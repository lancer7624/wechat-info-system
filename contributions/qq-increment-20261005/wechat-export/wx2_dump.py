# -*- coding: utf-8 -*-
"""二号微信全内存 dump:标准 MiniDumpWriteDump,不注入不 attach,零风险
用法: python wx2_dump.py [pid]
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "wx2_full.dmp")

PROCESS_ALL_ACCESS = 0x001F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]

dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
MiniDumpWriteDump = dbghelp.MiniDumpWriteDump
MiniDumpWriteDump.argtypes = [
    wt.HANDLE, wt.DWORD, wt.HANDLE, wt.DWORD,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
MiniDumpWriteDump.restype = wt.BOOL

MiniDumpWithFullMemory = 0x00000002
MiniDumpNormal = 0x00000000

GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
CREATE_ALWAYS = 0x2
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 30272
    hp = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hp:
        print(f"OpenProcess 失败 err={ctypes.get_last_error()}", flush=True)
        return
    hf = k32.CreateFileW(OUT, GENERIC_WRITE, FILE_SHARE_READ, None,
                         CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if hf == INVALID_HANDLE_VALUE:
        print("CreateFile 失败", flush=True)
        return
    print(f"开始 dump PID {pid} → {OUT} (全内存,预计 1GB+,一两分钟)", flush=True)
    ok = MiniDumpWriteDump(hp, pid, hf, MiniDumpWithFullMemory, None, None, None)
    k32.CloseHandle(hf)
    k32.CloseHandle(hp)
    if ok:
        print(f"完成: {os.path.getsize(OUT) // 1024 // 1024} MB", flush=True)
    else:
        print(f"失败 err={ctypes.get_last_error()}", flush=True)


if __name__ == "__main__":
    main()
