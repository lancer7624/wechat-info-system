# -*- coding: utf-8 -*-
"""通用版:微信 key 盐邻域扫描对照实验
扫 <wxid_dir> 各库盐邻域熵窗口 + 频次,和 <known_key> 比对:
- 已知 key 命中 → 方法有效
- 已知 key 没进候选 → 该方法对微信无效(一号当年是 frida 登录时抓的)
用法: python wx_key_probe.py <wxid_dir> <known_key_hex 或 none>
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wx2_key_scan import iter_regions, read_region, entropy_windows, wx2_pids  # noqa: E402

SALT_DBS = ["message/message_0.db", "contact/contact.db", "session/session.db"]
CTX = 2048


def main():
    wxid_dir = sys.argv[1]
    known = sys.argv[2] if len(sys.argv) > 2 else None
    pids = wx2_pids() if "<你的wxid二号>" in wxid_dir else None
    if pids is None:
        out = subprocess.check_output(
            "powershell -NoProfile -Command \"Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object Id,Path\"",
            shell=True).decode("utf-8", "replace")
        pids = [int(m) for m in re.findall(r"^(\d+)\s", out, re.M)]
    print(f"PIDs: {pids}", flush=True)

    salts = {}
    for rel in SALT_DBS:
        p = os.path.join(wxid_dir, "db_storage", rel)
        try:
            with open(p, "rb") as f:
                salts[rel.split("/")[0]] = f.read(16)
        except OSError:
            pass
    print(f"盐: {dict((k, v.hex()[:16]) for k, v in salts.items())}", flush=True)

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

    print(f"熵窗口种类: {len(counter)}", flush=True)
    if known:
        kb = bytes.fromhex(known)
        if kb in counter:
            rank = [w for w, _ in counter.most_common()].index(kb) + 1
            print(f"★ 已知 key 在候选里! 频次={counter[kb]} 排名={rank}/{len(counter)}", flush=True)
            print("→ 方法有效,二号是 key 不在内存(需活性触发)", flush=True)
        else:
            print("✗ 已知 key 不在盐邻域候选里", flush=True)
            print("→ 方法对微信无效,key 不在盐邻域(别恋战,换思路)", flush=True)
    else:
        for w, c in counter.most_common(20):
            print(f"  freq={c:>4}  {w.hex()}", flush=True)


if __name__ == "__main__":
    main()
