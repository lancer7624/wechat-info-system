# -*- coding: utf-8 -*-
"""Phase 3v3：按公开资料的 WeChat 4.1.12 SQLCipher 布局重新验证

公开资料（stargazer-2026/wechat-4.1.12-decrypt）给出的真实参数：
- 页 4096，reserve 80：page = [0:16]salt | [16:4016]密文 | [4016:4032]IV | [4032:4096]HMAC
- enc_key = PBKDF2-HMAC-SHA512(master32, salt16, dklen=32, count=256000)
- mac_key = PBKDF2-HMAC-SHA512(enc_key, salt^0x3a, dklen=32, count=2)
- HMAC 校验：HMAC-SHA512(mac_key, page[16:4032] + pack('<I',1)) == page[4032:4096]
- 解密：AES-256-CBC，key=enc_key，IV=页面存储的 page[4016:4032]

v2 失败原因复盘：mac 输入范围、mac_key 长度、IV 来源都与真实布局不符。
本版按上式验证，另附多组变体（含/不含页码、含 salt、dklen 64）。
"""
import json
import os
import sys
import struct

from Crypto.Hash import HMAC, SHA512, SHA1
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))
CAND_FILE = os.path.join(HERE, "candidates.json")
OUT = os.path.join(HERE, "db_key.json")

DBS = [
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\message\message_0.db",
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\contact\contact.db",
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\session\session.db",
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\sns\sns.db",
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\favorite\favorite.db",
]

PAGE_SIZES = [4096, 65536, 1024, 2048, 8192, 16384, 32768]


def read_page(db_path):
    try:
        with open(db_path, "rb") as f:
            return f.read(65536)
    except OSError as e:
        print(f"  ✗ 无法读取 {os.path.basename(db_path)}: {e}")
        return None


def try_derived(derived, page, src):
    """对派生键跑 WeChat 4.1.12 布局 + 变体校验"""
    for ps in PAGE_SIZES:
        if len(page) < ps:
            continue
        salt = page[:16]
        for reserved in (80, 64, 20):
            data_end = ps - reserved
            if data_end <= 16:
                continue
            hmac_stored = page[data_end:ps] if reserved in (64, 20) else None
            # reserve 80：IV 存 4016..4032，HMAC 存 4032..4096
            if reserved == 80:
                iv_stored = page[ps - 80:ps - 64]
                hmac_stored = page[ps - 64:ps]
                data = page[16:ps - 80]
            else:
                iv_stored = None
                data = page[16:data_end]
            for dklen_mac in (32, 64):
                mac_key = PBKDF2(derived, bytes(b ^ 0x3A for b in salt),
                                 dklen_mac, count=2, hmac_hash_module=SHA512)
                # 变体 A：page[16:data_end] + LE32(页码)（公开资料公式）
                h = HMAC.new(mac_key, data + struct.pack("<I", 1), SHA512).digest()
                if h == hmac_stored:
                    return ("hmac-A-res80-%d" % reserved, ps)
                # 变体 B：不含页码
                h = HMAC.new(mac_key, data, SHA512).digest()
                if h == hmac_stored:
                    return ("hmac-B-res80-%d" % reserved, ps)
                # 变体 C：含 salt（从 0 开始）+ 页码
                h = HMAC.new(mac_key, page[:data_end] + struct.pack("<I", 1),
                             SHA512).digest()
                if h == hmac_stored:
                    return ("hmac-C-res80-%d" % reserved, ps)
                # 变体 D：含 salt，不含页码
                h = HMAC.new(mac_key, page[:data_end], SHA512).digest()
                if h == hmac_stored:
                    return ("hmac-D-res80-%d" % reserved, ps)
            # 明文校验：stored-IV AES-CBC（reserve 80 专用）
            if iv_stored is not None and len(data) % 16 == 0:
                try:
                    pt = AES.new(derived, AES.MODE_CBC, iv_stored).decrypt(data)
                except ValueError:
                    pt = b""
                if pt[:16] == b"SQLite format 3\x00":
                    return ("plaintext-storedIV", ps)
            # 明文校验：派生 IV（SQLCipher 经典）
            pk = HMAC.new(derived, struct.pack("<I", 1), SHA512).digest()
            if len(data) % 16 == 0:
                try:
                    pt = AES.new(pk[:32], AES.MODE_CBC, pk[32:48]).decrypt(data)
                except ValueError:
                    pt = b""
                if pt[:16] == b"SQLite format 3\x00":
                    return ("plaintext-derivedIV", ps)
    return None


def load_candidates():
    with open(CAND_FILE, "r", encoding="utf-8") as f:
        raw_list = json.load(f)
    cands, seen = [], set()
    for c in raw_list:
        try:
            raw = bytes.fromhex(c["hex"])
        except (ValueError, KeyError):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        cands.append((raw, c))
        # hex 文本候选：把 ASCII hex 解码成二进制再试一遍
        if c.get("is_hex_str") or (len(raw) % 2 == 0 and 64 <= len(raw) <= 256
                                   and all(b in b"0123456789abcdefABCDEF" for b in raw)):
            try:
                dec = bytes.fromhex(raw.decode("ascii"))
            except ValueError:
                dec = b""
            if dec not in seen and len(dec) >= 32:
                seen.add(dec)
                cands.append((dec, dict(c, hex=dec.hex(), off=0,
                                        reg="hexdec")))
    return cands


def save_key(raw, meta, db_path, scheme, ps, mode):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "key_hex": raw.hex(),
            "mode": mode,
            "scheme": scheme,
            "page_size": ps,
            "source": {"anchor": meta.get("anchor"), "reg": meta.get("reg"),
                       "off": meta.get("off")},
            "validated_db": os.path.basename(db_path),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n密钥已保存 → {OUT}")
    print("⚠️ 密钥是敏感信息，db_key.json 不要外传、不要入 git")


def main():
    cands = load_candidates()
    print(f"候选: {len(cands)} 条（含 hex 解码）")

    # 按来源把 a0006（MMV1 引用函数）排最前
    cands.sort(key=lambda x: (x[1].get("anchor") != "a0006",
                              -(x[1].get("freq", 0))))

    for db in DBS:
        page = read_page(db)
        if page is None:
            continue
        print(f"\n== 验证 {os.path.basename(db)} ==")
        for ci, (raw, meta) in enumerate(cands):
            if len(raw) == 32:
                masters = [(raw, "raw32")]
            else:
                masters = [(raw[o:o + 32], f"win@{o}")
                           for o in range(0, len(raw) - 31, 4)]
            for master, tag in masters:
                derived = PBKDF2(master, page[:16], 32, count=256000,
                                 hmac_hash_module=SHA512)
                hit = try_derived(derived, page, meta)
                if hit:
                    scheme, ps = hit
                    print(f"\n  ✅✅ 命中！库={os.path.basename(db)} 候选#{ci} "
                          f"anchor={meta.get('anchor')} r{meta.get('reg')} "
                          f"方案={scheme} 页={ps}")
                    print(f"     master key: {master.hex()}")
                    save_key(master, meta, db, scheme, ps, "pbkdf2-256k")
                    return
            if ci % 50 == 0:
                print(f"  已验 {ci}/{len(cands)} ...", flush=True)

    print("\n!! 修正方案仍无命中。密钥可能不在候选集（下一步：实时内存扫描 / MMV1 函数 spawn hook）")
    sys.exit(2)


if __name__ == "__main__":
    main()
