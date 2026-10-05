# -*- coding: utf-8 -*-
"""纯只读内存扫描(Windows API,无注入无 hook):
OpenProcess(PROCESS_VM_READ) + VirtualQueryEx + ReadProcessMemory,
搜 34 个 chatroom username 的 UTF-16LE,定位会话对象,找置顶标志。
纪律:只读,不写进程内存,不注入。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "tmp_users.json"), encoding="utf-8") as f:
    USERS = json.load(f)

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
MEMORY_BASIC_INFORMATION_SIZE = 48  # x64

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
OpenProcess.restype = wt.HANDLE

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wt.BOOL

VirtualQueryEx = kernel32.VirtualQueryEx
VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.c_void_p, ctypes.c_size_t]
VirtualQueryEx.restype = ctypes.c_size_t

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wt.HANDLE]


def scan(pid):
    h = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        err = ctypes.get_last_error()
        print(f"OpenProcess 失败 (err={err})", flush=True)
        return None
    print(f"打开进程 {pid} OK", flush=True)

    enc = {u: u.encode("utf-16-le") for u in USERS}
    hits = []
    addr = 0
    mbi = (ctypes.c_ubyte * MEMORY_BASIC_INFORMATION_SIZE)()
    buf = (ctypes.c_ubyte * 0x400000)()  # 4MB 缓冲
    total_scanned = 0
    regions = 0
    max_addr = 0x7FFFFFFFFFFF

    while addr < max_addr:
        if VirtualQueryEx(h, addr, mbi, MEMORY_BASIC_INFORMATION_SIZE) == 0:
            break
        base = ctypes.cast(mbi, ctypes.POINTER(ctypes.c_ulonglong))[0]
        alloc_base = ctypes.cast(ctypes.byref(mbi, 8), ctypes.POINTER(ctypes.c_ulonglong))[0]
        # x64 MBI: Base(0,8) AllocBase(8,8) AllocProtect(16,4) pad(20,4) RegionSize(24,8)
        #          State(32,4) Protect(36,4) Type(40,4) pad(44,4) = 48B
        region_size = ctypes.cast(ctypes.byref(mbi, 24), ctypes.POINTER(ctypes.c_ulonglong))[0]
        state = ctypes.cast(ctypes.byref(mbi, 32), ctypes.POINTER(ctypes.c_uint32))[0]
        protect = ctypes.cast(ctypes.byref(mbi, 36), ctypes.POINTER(ctypes.c_uint32))[0]

        if region_size == 0:
            break
        if state == MEM_COMMIT and not (protect & PAGE_NOACCESS) and not (protect & PAGE_GUARD) \
                and region_size < 0x10000000:
            regions += 1
            off = 0
            while off < region_size:
                chunk = min(0x400000, region_size - off)
                read = ctypes.c_size_t()
                cur = base + off
                if ReadProcessMemory(h, cur, buf, chunk, ctypes.byref(read)):
                    data = bytes(buf[:read.value])
                    total_scanned += read.value
                    for u, pat in enc.items():
                        idx = data.find(pat)
                        while idx != -1:
                            start = max(0, idx - 48)
                            dump = data[start:idx + len(pat) + 96]
                            hits.append({
                                "user": u,
                                "addr": hex(cur + idx),
                                "hex": dump.hex(),
                            })
                            if len(hits) % 50 == 0:
                                print(f"  命中累计 {len(hits)}", flush=True)
                            idx = data.find(pat, idx + 1)
                else:
                    break  # 读失败,跳该区域
                off += chunk
        addr = base + region_size
        if addr <= base:
            break

    CloseHandle(h)
    print(f"扫完: {regions} 个区域, {total_scanned // 1024 // 1024} MB", flush=True)
    return hits


if __name__ == "__main__":
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not pid:
        # 自动找主 Weixin 进程(内存最大)
        import subprocess
        out = subprocess.check_output(
            "powershell -Command \"Get-Process Weixin -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending | Select-Object -First 1 Id\"",
            shell=True).decode("gbk", "replace").strip()
        pid = int(out.split()[-1])
        print(f"自动选中 PID={pid}", flush=True)
    hits = scan(pid)
    if hits:
        out = os.path.join(HERE, "mem_hits.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(hits, f, ensure_ascii=False, indent=1)
        print(f"完成: {len(hits)} 个命中 → {out}", flush=True)
    else:
        print("无命中或失败", flush=True)
