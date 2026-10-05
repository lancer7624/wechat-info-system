# -*- coding: utf-8 -*-
"""微信二号 key 扫描 v2:纯 ReadProcessMemory
微信 4.x 不跑 SQL,key 在内存里是裸 32B,不是 QQ 的 x'...' 字面量。
策略:定位盐(裸 16B + hex 32B 两种形态),dump 盐 ±512B 邻域,
提取所有 32B 熵窗口做候选,逐个页1魔数验证。
用法: python wx2_key_scan.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from decrypt_db import derive_keys, AES  # noqa: E402

WX2_DB = r"C:\Users\<你的用户名>\Documents\xwechat_files\<你的wxid二号>\db_storage"
SALT_DBS = [
    os.path.join(WX2_DB, "message", "message_0.db"),
    os.path.join(WX2_DB, "contact", "contact.db"),
    os.path.join(WX2_DB, "session", "session.db"),
]
KEY_OUT = os.path.join(HERE, "wx2_key.json")
CTX = 512  # 盐邻域 dump 半径

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.c_void_p, ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.CloseHandle.argtypes = [wt.HANDLE]


def wx2_pids():
    out = subprocess.check_output(
        "powershell -NoProfile -Command \"Get-CimInstance Win32_Process -Filter \\\"Name='Weixin.exe'\\\" | Where-Object {$_.ExecutablePath -like '*Weixin2*'} | Select-Object ProcessId,WorkingSetSize\"",
        shell=True).decode("utf-8", "replace")
    pids = []
    for line in out.splitlines():
        m = re.search(r"(\d+)\s+(\d+)", line)
        if m:
            pids.append((int(m.group(2)), int(m.group(1))))
    pids.sort(reverse=True)
    return [p for _, p in pids]


def read_region(h, base, size):
    buf = (ctypes.c_ubyte * size)()
    read = ctypes.c_size_t()
    if k32.ReadProcessMemory(h, base, buf, size, ctypes.byref(read)):
        return bytes(buf[:read.value])
    return None


def iter_regions(h):
    addr = 0
    mbi = (ctypes.c_ubyte * 48)()
    while addr < 0x7FFFFFFFFFFF:
        if k32.VirtualQueryEx(h, addr, mbi, 48) == 0:
            break
        base = ctypes.cast(mbi, ctypes.POINTER(ctypes.c_ulonglong))[0]
        region_size = ctypes.cast(ctypes.byref(mbi, 24),
                                  ctypes.POINTER(ctypes.c_ulonglong))[0]
        state = ctypes.cast(ctypes.byref(mbi, 32), ctypes.POINTER(ctypes.c_uint32))[0]
        protect = ctypes.cast(ctypes.byref(mbi, 36), ctypes.POINTER(ctypes.c_uint32))[0]
        if region_size == 0:
            break
        if state == 0x1000 and not (protect & 0x101):
            yield base, region_size
        addr = base + region_size
        if addr <= base:
            break


def scan_salt_context(h, salt):
    """找盐(裸+hex 两种形态)出现位置,返回盐邻域 bytes 列表"""
    sraw = bytes.fromhex(salt)
    shex = salt.encode()
    out = []
    for base, region_size in iter_regions(h):
        off = 0
        while off < region_size:
            chunk = min(0x800000, region_size - off)
            data = read_region(h, base + off, chunk)
            if data is None:
                break
            for pat in (sraw, shex):
                idx = data.find(pat)
                while idx != -1:
                    a = max(0, idx - CTX)
                    b = min(len(data), idx + len(pat) + CTX)
                    out.append((hex(base + off + idx), data[a:b]))
                    idx = data.find(pat, idx + 1)
                    if len(out) > 200:
                        return out
            off += chunk
    return out


def entropy_windows(ctx_bytes, wsize=32):
    """提取所有 32B 熵窗口:≥16 种不同字节、零字节≤9、可打印≤21"""
    cands = set()
    for i in range(0, len(ctx_bytes) - wsize + 1, 4):
        w = ctx_bytes[i:i + wsize]
        if len(set(w)) < 16:
            continue
        if w.count(0) > 9:
            continue
        if sum(1 for b in w if 32 <= b < 127) > 21:
            continue
        cands.add(w)
    return cands


def verify_key(key_hex):
    master = bytes.fromhex(key_hex)
    for p in SALT_DBS:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                page1 = f.read(4096)
            salt = page1[0:16]
            enc, _ = derive_keys(master, salt)
            iv1 = page1[4016:4032]
            pt1 = AES.new(enc, AES.MODE_CBC, iv1).decrypt(page1[16:4016])
            if bytes(pt1[:16]) == b"SQLite format 3\x00":
                return os.path.basename(p)
        except Exception:
            continue
    return None


def main():
    pids = wx2_pids()
    if not pids:
        print("二号微信没在跑,先打开二号窗口", flush=True)
        return
    print(f"二号进程 PIDs: {pids}", flush=True)
    salts = {}
    for p in SALT_DBS:
        try:
            with open(p, "rb") as f:
                salts[os.path.basename(p)] = f.read(16).hex()
        except OSError:
            pass
    print(f"盐: {salts}", flush=True)

    all_cands = set()
    for pid in pids:
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print(f"PID {pid}: OpenProcess 失败", flush=True)
            continue
        for name, salt in salts.items():
            ctxs = scan_salt_context(h, salt)
            n_cand = 0
            for addr, ctx in ctxs:
                for w in entropy_windows(ctx):
                    if w not in all_cands:
                        all_cands.add(w)
                        n_cand += 1
            print(f"  PID {pid} {name}: 盐邻域 {len(ctxs)} 处,新增候选 {n_cand}", flush=True)
        k32.CloseHandle(h)

    # 64hex ASCII 串兜底(和盐 hex 串共现过的优先,这里全抓但限量)
    print(f"\n候选总数: {len(all_cands)},开始逐个验证(每个 ~0.2s)...", flush=True)
    n = 0
    for w in all_cands:
        n += 1
        ok = verify_key(w.hex())
        if ok:
            print(f"  ✓ 第 {n} 个候选验证通过({ok})! key={w.hex()}", flush=True)
            json.dump({"key_hex": w.hex(), "wxid": "<你的wxid二号>",
                       "validated_db": ok},
                      open(KEY_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"  已存 {KEY_OUT}", flush=True)
            return
        if n % 50 == 0:
            print(f"  已验证 {n}/{len(all_cands)}...", flush=True)
    print("全灭。key 不在盐邻域(可能登录时临时存在后被清,或结构不相邻)。", flush=True)


if __name__ == "__main__":
    main()
