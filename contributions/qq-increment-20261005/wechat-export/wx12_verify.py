# -*- coding: utf-8 -*-
"""并行验证 dump 熵窗口候选:一号+二号库都验,multiprocessing 8 核
用法: python wx12_verify.py [topN]
"""
import json
import multiprocessing as mp
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from decrypt_db import derive_keys, AES  # noqa: E402

DUMP = os.path.join(HERE, "wx12_full.dmp")
HITS = os.path.join(HERE, "wx12_scan_hits.json")

ACCOUNTS = {
    "1号": r"C:\Users\<你的用户名>\Documents\xwechat_files\<你的wxid一号>\db_storage",
    "2号": r"C:\Users\<你的用户名>\Documents\xwechat_files\<你的wxid二号>\db_storage",
}
DBS = ["message/message_0.db", "contact/contact.db", "session/session.db"]

_page1_cache = {}


def page1_of(path):
    if path not in _page1_cache:
        with open(path, "rb") as f:
            _page1_cache[path] = f.read(4096)
    return _page1_cache[path]


def try_key(key_hex):
    """返回 (账号, 库名) 或 None"""
    master = bytes.fromhex(key_hex)
    for acc, root in ACCOUNTS.items():
        for rel in DBS:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                continue
            try:
                page1 = page1_of(p)
                salt = page1[0:16]
                enc, _ = derive_keys(master, salt)
                iv1 = page1[4016:4032]
                pt1 = AES.new(enc, AES.MODE_CBC, iv1).decrypt(page1[16:4016])
                if bytes(pt1[:16]) == b"SQLite format 3\x00":
                    return acc, rel.split("/")[0]
            except Exception:
                continue
    return None


def worker(args):
    w_hex, i, total = args
    hit = try_key(w_hex)
    return i, w_hex, hit


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    hits = json.load(open(HITS, encoding="utf-8"))
    hits.sort(key=lambda x: -x["c"])
    jobs = []
    with open(DUMP, "rb") as f:
        for i, h in enumerate(hits[:top_n]):
            f.seek(h["o"])
            w = f.read(32)
            if len(w) == 32:
                jobs.append((w.hex(), i, h["c"]))
    print(f"候选 {len(jobs)} 个,{mp.cpu_count()} 核并行验证...", flush=True)

    found = {}
    with mp.Pool(min(8, mp.cpu_count())) as pool:
        for n, (i, w_hex, hit) in enumerate(
                pool.imap_unordered(worker, jobs, chunksize=4), 1):
            if hit:
                acc, db = hit
                print(f"★ {acc} {db} key 找到! key={w_hex}", flush=True)
                found[acc] = w_hex
                if acc == "1号":
                    json.dump({"key_hex": w_hex, "wxid": "<你的wxid一号>",
                               "validated_db": db, "note": "重登录轮换后的新 key"},
                              open(os.path.join(HERE, "db_key.json"), "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                    print("  一号新 key 已更新 db_key.json", flush=True)
                if acc == "2号":
                    json.dump({"key_hex": w_hex, "wxid": "<你的wxid二号>",
                               "validated_db": db},
                              open(os.path.join(HERE, "wx2_key.json"), "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                    print("  二号 key 已存 wx2_key.json", flush=True)
                if len(found) == 2:
                    print("两个账号 key 全到手,收工", flush=True)
                    pool.terminate()
                    break
            if n % 500 == 0:
                print(f"  已验证 {n}/{len(jobs)}...", flush=True)
    if not found:
        print("top N 全灭。", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
