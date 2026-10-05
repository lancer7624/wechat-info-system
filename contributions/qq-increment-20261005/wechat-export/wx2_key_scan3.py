# -*- coding: utf-8 -*-
"""微信二号 key 扫描 v3:全内存抓 64hex ASCII 串
假设:微信把 master key 以 hex 字符串形式存内存(64 字符)。
策略 A:盐 hex 串 ±2KB 邻域内的 64hex 串 → 优先验证
策略 B:全内存 64hex 串按频次排序 → 高频优先验证
用法: python wx2_key_scan3.py
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
from wx2_key_scan import verify_key, wx2_pids, iter_regions, read_region  # noqa: E402

WX2_DB = r"C:\Users\<你的用户名>\Documents\xwechat_files\<你的wxid二号>\db_storage"
SALT_DBS = [
    os.path.join(WX2_DB, "message", "message_0.db"),
    os.path.join(WX2_DB, "contact", "contact.db"),
    os.path.join(WX2_DB, "session", "session.db"),
]
KEY_OUT = os.path.join(HERE, "wx2_key.json")
HEX64 = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")
CTX = 2048


def salts():
    out = {}
    for p in SALT_DBS:
        try:
            with open(p, "rb") as f:
                out[os.path.basename(p)] = f.read(16).hex().encode()
        except OSError:
            pass
    return out


def main():
    pids = wx2_pids()
    if not pids:
        print("二号微信没在跑", flush=True)
        return
    salts_map = salts()
    print(f"PIDs: {pids}, 盐: {salts_map}", flush=True)

    prio = set()
    counter = Counter()
    for pid in pids:
        h = ctypes.WinDLL("kernel32", use_last_error=True)
        h.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        h.OpenProcess.restype = wt.HANDLE
        hp = h.OpenProcess(0x0010 | 0x0400, False, pid)
        if not hp:
            continue
        for base, region_size in iter_regions(hp):
            off = 0
            while off < region_size:
                chunk = min(0x800000, region_size - off)
                data = read_region(hp, base + off, chunk)
                if data is None:
                    break
                for m in HEX64.finditer(data):
                    counter[m.group(1)] += 1
                for name, s in salts_map.items():
                    idx = data.find(s)
                    while idx != -1:
                        a = max(0, idx - CTX)
                        b = min(len(data), idx + len(s) + CTX)
                        for m in HEX64.finditer(data[a:b]):
                            prio.add(m.group(1))
                        idx = data.find(s, idx + 1)
                off += chunk
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle.argtypes = [wt.HANDLE]
        ctypes.WinDLL("kernel32").CloseHandle(hp)

    print(f"盐邻域 64hex 串: {len(prio)},全内存 64hex 串种类: {len(counter)}", flush=True)

    tried = set()
    order = sorted(prio, key=lambda x: -counter[x])
    order += [k for k, _ in counter.most_common(3000) if k not in tried]

    n = 0
    for cand in order:
        if cand in tried:
            continue
        tried.add(cand)
        n += 1
        ok = verify_key(cand.decode())
        if ok:
            print(f"  ✓ 第 {n} 个验证通过({ok})! key={cand.decode()}", flush=True)
            json.dump({"key_hex": cand.decode(), "wxid": "<你的wxid二号>",
                       "validated_db": ok},
                      open(KEY_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"  已存 {KEY_OUT}", flush=True)
            return
        if n % 100 == 0:
            print(f"  已验证 {n}/{len(order)}...", flush=True)
    print("全灭。key 不是 hex 字符串形态,或不在内存。", flush=True)


if __name__ == "__main__":
    main()
