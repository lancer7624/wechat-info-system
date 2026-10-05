# -*- coding: utf-8 -*-
"""微信二号 key 扫描 v4:盐邻域熵窗口 + 全局频次统计
v2 教训:候选 set 去重丢了频次。真 key 32B 每个打开的库都要参与
PBKDF2/解密,内存里同一份 key 会出现多次。取高频 top 验证。
用法: python wx2_key_scan4.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wx2_key_scan import verify_key, wx2_pids, iter_regions, read_region, entropy_windows  # noqa: E402

WX2_DB = r"C:\Users\<你的用户名>\Documents\xwechat_files\<你的wxid二号>\db_storage"
SALT_DBS = [
    os.path.join(WX2_DB, "message", "message_0.db"),
    os.path.join(WX2_DB, "contact", "contact.db"),
    os.path.join(WX2_DB, "session", "session.db"),
]
KEY_OUT = os.path.join(HERE, "wx2_key.json")
CTX = 2048


def main():
    pids = wx2_pids()
    if not pids:
        print("二号微信没在跑", flush=True)
        return
    salts = {}
    for p in SALT_DBS:
        try:
            with open(p, "rb") as f:
                salts[os.path.basename(p)] = f.read(16)
        except OSError:
            pass
    print(f"PIDs: {pids}", flush=True)

    counter = Counter()
    for pid in pids:
        hp = ctypes.WinDLL("kernel32", use_last_error=True)
        hp.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        hp.OpenProcess.restype = wt.HANDLE
        h = hp.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            continue
        for base, region_size in iter_regions(h):
            off = 0
            while off < region_size:
                chunk = min(0x800000, region_size - off)
                data = read_region(h, base + off, chunk)
                if data is None:
                    break
                for salt in salts.values():
                    idx = data.find(salt)
                    while idx != -1:
                        a = max(0, idx - CTX)
                        b = min(len(data), idx + len(salt) + CTX)
                        for w in entropy_windows(data[a:b]):
                            counter[w] += 1
                        idx = data.find(salt, idx + 1)
                off += chunk
        ctypes.WinDLL("kernel32").CloseHandle(h)

    order = [w for w, _ in counter.most_common(800)]
    print(f"熵窗口种类: {len(counter)},验证高频 top {len(order)}", flush=True)
    for i, w in enumerate(order, 1):
        ok = verify_key(w.hex())
        if ok:
            print(f"  ✓ 第 {i} 个(freq={counter[w]})验证通过({ok})! key={w.hex()}", flush=True)
            json.dump({"key_hex": w.hex(), "wxid": "<你的wxid二号>",
                       "validated_db": ok},
                      open(KEY_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"  已存 {KEY_OUT}", flush=True)
            return
        if i % 100 == 0:
            print(f"  已验证 {i}/{len(order)}...", flush=True)
    print("高频全灭。", flush=True)


if __name__ == "__main__":
    main()
