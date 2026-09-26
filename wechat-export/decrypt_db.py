# -*- coding: utf-8 -*-
"""微信 4.x 数据库解密工具（离线版，支持 WAL 合并）
方案（已通过内存页对 + 文件 MAC 双重验证）：
- master key（32B）来自 Frida 登录时捕获；或 enc_keys 模式（wcdb-key-tool 扫得的每库现成密钥），两种都存 db_key.json
- enc_key = PBKDF2-HMAC-SHA512(master, 页1[0:16], 32, 256000)
- mac_key = PBKDF2-HMAC-SHA512(enc_key, 页1salt^0x3a, 32, 2)
- 页面布局（页大小 ps，reserve=80，usable=ps-80）：
    页1:  salt(16) | ct[16:usable] | IV(16) | MAC(64)   → pt[0:16]=SQLite魔数
    页≥2: ct[0:usable] | IV(16) | MAC(64)               → pt[0:usable]
- 密码：AES-256-CBC，key=enc_key，IV=页内[usable:usable+16]
- MAC：HMAC-SHA512(mac_key, ct区+IV+LE32(页号))[:64] == 页内 MAC
- WAL 帧与主库页同格式同密钥，解析后按页号覆盖
输出：解密后的标准 SQLite 文件（reserve=80 保留原样，sqlite3 可直接读）
"""
import argparse
import json
import os
import sqlite3
import struct
import sys

from Crypto.Hash import HMAC, SHA512
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))


class EncKeyRing:
    """enc_keys 模式：每库现成的最终密钥（如 wcdb-key-tool 的 Config.Cipher 扫描所得）。
    这类 key 等于 PBKDF2(master, salt, 256000) 的结果，无法逆推回 master，
    因此按页1 salt 直接取用，跳过 256000 轮推导。"""

    def __init__(self, enc_map):
        self.enc_map = enc_map  # {salt_hex(小写): bytes(32)}


def load_master(key_path):
    with open(key_path, encoding="utf-8") as f:
        d = json.load(f)
    if "key_hex" in d:
        return bytes.fromhex(d["key_hex"])
    if "enc_keys" in d:
        enc_map = {}
        for salt_hex, enc_hex in d["enc_keys"].items():
            s = str(salt_hex).strip().lower()
            e = str(enc_hex).strip().lower()
            if len(s) != 32 or len(e) != 64:
                raise RuntimeError(
                    f"enc_keys 条目长度不对: salt={len(s)} hex, key={len(e)} hex"
                    "（应分别为 32 / 64）")
            enc_map[s] = bytes.fromhex(e)
        if not enc_map:
            raise RuntimeError("db_key.json 的 enc_keys 是空的")
        return EncKeyRing(enc_map)
    raise RuntimeError("db_key.json 里既没有 key_hex 也没有 enc_keys")


def derive_keys(master, salt):
    if isinstance(master, EncKeyRing):
        enc = master.enc_map.get(salt.hex())
        if enc is None:
            raise RuntimeError(
                f"db_key.json 里没有该库的密钥（salt={salt.hex()}）——"
                "抓取时可能漏扫了这个库，重跑一次密钥抓取")
    else:
        enc = PBKDF2(master, salt, 32, count=256000, hmac_hash_module=SHA512)
    mac = PBKDF2(enc, bytes(b ^ 0x3A for b in salt), 32,
                 count=2, hmac_hash_module=SHA512)
    return enc, mac


def decrypt_page(page, pgno, enc, page_size, usable):
    """解密单页（页1含 salt 前缀特例）→ 明文页字节"""
    iv = page[usable:usable + 16]
    if pgno == 1:
        pt = bytearray(page_size)
        pt[0:16] = b"SQLite format 3\x00"
        pt[16:usable] = AES.new(enc, AES.MODE_CBC, iv).decrypt(
            page[16:usable])
        pt[usable:] = page[usable:]
    else:
        pt = bytearray(AES.new(enc, AES.MODE_CBC, iv).decrypt(
            page[:usable]))
        pt += page[usable:]
    return bytes(pt)


def check_mac(page, pgno, mac_key, page_size, usable):
    # 页1 的 MAC 区为 ct[16:usable]+IV（不含 salt）；页≥2 为 ct[0:usable]+IV
    region = page[16:usable + 16] if pgno == 1 else page[:usable + 16]
    h = HMAC.new(mac_key, region + struct.pack("<I", pgno),
                 SHA512).digest()
    return h == page[usable + 16:usable + 80]


def decrypt_wal(wal_path, enc, mac_key, page_size, usable):
    """解析 WAL 文件，返回 ({pgno: 明文页}, 跳过的页数)
    SQLite WAL 帧头 24B: pgno(4) nTruncate(4) salt(8) cksum(8)
    - 提交帧: pgno=0 且 nTruncate≠0 → 本事务页面生效
    - 数据帧: pgno≠0 → 缓冲，待提交帧确认
    - 每页先 MAC 验证再解密（防错位垃圾页覆盖）
    ⚠️ 已知限制：微信 WAL 为环形复用（journal_size_limit=4MB），帧在文件中
    不按帧号顺序排列，有效帧区需 -shm wal-index 定位；本函数仅适用于
    顺序帧布局（调试用），生产路径默认不合并 WAL。"""
    out = {}
    skipped = 0
    if not wal_path or not os.path.exists(wal_path):
        return out, skipped
    with open(wal_path, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            return out, skipped
        magic = int.from_bytes(head[0:4], "big")
        if magic not in (0x377F0682, 0x377F0683):
            return out, skipped
        wal_ps = int.from_bytes(head[8:12], "big")
        if wal_ps != page_size:
            raise RuntimeError(f"WAL 页大小 {wal_ps} 与主库 {page_size} 不一致")
        pending = {}
        while True:
            fh = f.read(24)
            if len(fh) < 24:
                break
            pgno = int.from_bytes(fh[0:4], "big")
            ntrunc = int.from_bytes(fh[4:8], "big")
            if pgno == 0:
                if ntrunc:
                    # 提交帧：本事务的页面正式生效
                    out.update(pending)
                pending = {}
                continue
            page = f.read(page_size)
            if len(page) < page_size:
                break
            if check_mac(page, pgno, mac_key, page_size, usable):
                pending[pgno] = decrypt_page(page, pgno, enc,
                                             page_size, usable)
            else:
                skipped += 1
        # EOF 时 pending 中是无提交帧保护的帧（回滚/陈旧），丢弃
    return out, skipped


def decrypt_db(db_path, master, out_path=None, verify_mac=True,
               progress=False, wal_path=None, use_wal=False):
    """解密一个 WeChat 4.x SQLCipher 数据库
    use_wal: 是否合并 WAL。⚠️ 微信 WAL 为环形复用（4MB journal_size_limit），
    帧顺序非文件顺序，需 -shm wal-index 定位（运行中 mmap 读取会撕裂），
    故默认 False = 只用主库快照（一致但滞后几页）。"""
    if wal_path is None and use_wal:
        w = db_path + "-wal"
        wal_path = w if os.path.exists(w) else None

    with open(db_path, "rb") as f:
        head = f.read(16)

    # 先按 4096 假设解第 1 页拿真实页大小
    with open(db_path, "rb") as f:
        page1 = f.read(4096)
    salt = page1[0:16]
    enc, mac = derive_keys(master, salt)

    iv1 = page1[4016:4032]
    pt1 = bytearray(4096)
    pt1[0:16] = b"SQLite format 3\x00"
    pt1[16:4016] = AES.new(enc, AES.MODE_CBC, iv1).decrypt(page1[16:4016])
    pt1[4016:4096] = page1[4016:4096]
    if bytes(pt1[0:16]) != b"SQLite format 3\x00":
        raise RuntimeError("页1魔数不对：密钥可能错误")
    page_size = int.from_bytes(bytes(pt1[16:18]), "big")
    reserve = pt1[20]
    usable = page_size - reserve

    fsize = os.path.getsize(db_path)
    n_pages = fsize // page_size
    if progress:
        print(f"页大小={page_size} reserve={reserve} usable={usable} "
              f"文件页数={n_pages}", flush=True)

    # 重读第 1 页（若页大小 > 4096）
    with open(db_path, "rb") as f:
        page1 = f.read(page_size)
    bad_mac = 0
    out = bytearray(n_pages * page_size)
    out[0:page_size] = decrypt_page(page1, 1, enc, page_size, usable)
    if verify_mac:
        if not check_mac(page1, 1, mac, page_size, usable):
            bad_mac += 1
            print("警告：页1 MAC 不匹配", flush=True)

    with open(db_path, "rb") as f:
        f.seek(page_size)
        for i in range(2, n_pages + 1):
            page = f.read(page_size)
            if len(page) < page_size:
                break
            out[(i - 1) * page_size:i * page_size] = decrypt_page(
                page, i, enc, page_size, usable)
            if verify_mac and not check_mac(page, i, mac, page_size, usable):
                bad_mac += 1
            if progress and i % 2000 == 0:
                print(f"  已解密 {i}/{n_pages} 页", flush=True)

    # WAL 合并（仅 use_wal=True，调试用；生产默认关闭）
    if use_wal and wal_path:
        n_wal = 0
        wal_pages, wal_skipped = decrypt_wal(wal_path, enc, mac,
                                             page_size, usable)
        for pgno, pt in wal_pages.items():
            off = (pgno - 1) * page_size
            if off + page_size > len(out):
                out.extend(b"\x00" * (off + page_size - len(out)))
            out[off:off + page_size] = pt
            n_wal += 1
        if n_wal:
            msg = f"WAL 合并：{n_wal} 页"
            if wal_skipped:
                msg += f"（跳过 {wal_skipped} 页 MAC 失败）"
            print(msg, flush=True)
        # 合并后文件变大：同步页1头部声明的页数（与 checkpoint 行为一致）
        total_pages = len(out) // page_size
        if total_pages != int.from_bytes(out[28:32], "big"):
            out[28:32] = total_pages.to_bytes(4, "big")

    if verify_mac:
        print(f"MAC 校验：{n_pages - bad_mac}/{n_pages} 页通过", flush=True)

    if out_path:
        with open(out_path, "wb") as f:
            f.write(out)
        print(f"已写出：{out_path}", flush=True)
    return bytes(out)


def dump_tables(db_bytes, label):
    """用 sqlite3 打开解密后的字节流，列出表名+行数"""
    path = os.path.join(HERE, f"_tmp_{label}.db")
    with open(path, "wb") as f:
        f.write(db_bytes)
    try:
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute("SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table','index') ORDER BY type")
        print(f"\n== {label} ==")
        tables = []
        for name, typ in cur:
            if typ == "table" and not name.startswith("sqlite_"):
                tables.append(name)
        for t in tables:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                cnt = cur.fetchone()[0]
                print(f"  {t}: {cnt} 行")
            except sqlite3.Error as e:
                print(f"  {t}: 查询失败 ({e})")
        con.close()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", help="数据库路径；缺省=全部 5 个库")
    ap.add_argument("--key", default=os.path.join(HERE, "db_key.json"))
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--count", action="store_true", help="列出表行数")
    args = ap.parse_args()

    master = load_master(args.key)

    DB_DIR = r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage"
    DBS = [
        (os.path.join(DB_DIR, "message", "message_0.db"), "message_0"),
        (os.path.join(DB_DIR, "contact", "contact.db"), "contact"),
        (os.path.join(DB_DIR, "session", "session.db"), "session"),
        (os.path.join(DB_DIR, "sns", "sns.db"), "sns"),
        (os.path.join(DB_DIR, "favorite", "favorite.db"), "favorite"),
    ]
    if args.db:
        DBS = [(args.db, os.path.splitext(os.path.basename(args.db))[0])]

    for path, label in DBS:
        if not os.path.exists(path):
            print(f"跳过（不存在）：{path}", flush=True)
            continue
        print(f"\n===== 解密 {label} ({os.path.getsize(path)} 字节) =====",
              flush=True)
        try:
            out_path = None
            if args.out:
                os.makedirs(args.out, exist_ok=True)
                out_path = os.path.join(args.out, f"{label}.decrypted.db")
            data = decrypt_db(path, master, out_path=out_path,
                              verify_mac=not args.no_verify, progress=True)
            if args.count:
                dump_tables(data, label)
        except Exception as e:
            print(f"解密失败：{e}", flush=True)
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
