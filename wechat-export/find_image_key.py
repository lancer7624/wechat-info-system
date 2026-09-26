# -*- coding: utf-8 -*-
"""从微信进程内存提取图片 .dat 的 AES 密钥（V2 格式）
原理（公开研究）：微信查看图片时，V2 图片 AES 密钥会临时加载到进程内存。
扫描进程可读内存，用每个候选密钥试解样本密文第一块（AES-128-ECB），
明文以图片魔数开头即为命中。全程只读内存，不 hook 不改代码。

用法（需管理员权限，微信运行中，先随便点开 2-3 张聊天图片）：
    python find_image_key.py
密钥保存到 image_key.json（敏感，同 db_key.json 待遇：不入 git 不外传）
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import string
import sys

from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "image_key.json")

MEM_COMMIT = 0x1000
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

IMG_HEADS = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8", b"BM")


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def find_sample():
    """取 3 个 V2 缩略图的 AES 段首块作样本（缩略图解密后必为标准 JPEG/PNG 头）"""
    root = os.path.expanduser(
        r"~\Documents\xwechat_files")
    if not os.path.isdir(root):
        print("找不到 xwechat_files 目录", flush=True)
        sys.exit(1)
    samples = []
    for d1 in os.listdir(root):
        attach = os.path.join(root, d1, "msg", "attach")
        if not os.path.isdir(attach):
            continue
        for d2 in os.listdir(attach):
            for d3 in os.listdir(os.path.join(attach, d2)):
                imgdir = os.path.join(attach, d2, d3, "Img")
                if not os.path.isdir(imgdir):
                    continue
                for fn in os.listdir(imgdir):
                    if not fn.endswith("_t.dat"):
                        continue
                    p = os.path.join(imgdir, fn)
                    with open(p, "rb") as f:
                        data = f.read(31)
                    if data[:6] == b"\x07\x08V2\x08\x07":
                        samples.append((data[15:31], p))
                        if len(samples) >= 3:
                            return samples
    if not samples:
        print("未找到 V2 格式缩略图样本", flush=True)
        sys.exit(1)
    return samples


def check_key(candidate):
    """单块 AES-128-ECB 解密，3 个样本都必须命中强图片魔数（防短魔数假阳性）"""
    try:
        cipher = AES.new(candidate, AES.MODE_ECB)
        for ct, _ in sample_cts:
            plain = cipher.decrypt(ct)
            if plain[:3] == b"\xff\xd8\xff" and plain[3] in (0xE0, 0xE1, 0xDB, 0xC4, 0xFE):
                continue  # JPEG SOI + 常见标记
            if plain[:8] == b"\x89PNG\r\n\x1a\n":
                continue  # PNG 完整魔数
            return False
    except Exception:
        return False
    return True


def scan_strings(pm_handle, base, size):
    """区域内容 → 提取 16/32 字节可打印字符串候选（含标点）"""
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t()
    if not ctypes.windll.kernel32.ReadProcessMemory(
            pm_handle, ctypes.c_void_p(base), buf, size, ctypes.byref(read)):
        return
    data = buf.raw
    # 可打印 ASCII（32-126）的 16 或 32 字节串（key 形态）
    pat = re.compile(rb"[\x20-\x7e]{16}(?:[\x20-\x7e]{16})?")
    for m in pat.finditer(data):
        s = m.group()
        if len(s) == 16:
            yield s
        else:
            yield s[:16]
            yield s[16:32]


def main():
    global sample_cts
    samples = find_sample()
    sample_cts = [ct for ct, _ in samples]
    print(f"样本: {len(samples)} 个缩略图", flush=True)
    for ct, p in samples:
        print(f"  {p}", flush=True)

    # 找微信进程（4.x 为 Weixin.exe，兼容 WeChat.exe）
    PROCNAMES = ("Weixin.exe", "WeChat.exe")
    k32 = ctypes.windll.kernel32
    pid = None
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    if k32.Process32First(snap, ctypes.byref(entry)):
        while True:
            name = entry.szExeFile.decode("gbk", "ignore")
            if name.lower() in (p.lower() for p in PROCNAMES):
                pid = entry.th32ProcessID
                break
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
    k32.CloseHandle(snap)
    if not pid:
        print("微信未运行。请启动微信并点开 2-3 张聊天图片后重试", flush=True)
        sys.exit(1)
    print(f"微信进程 PID={pid}", flush=True)

    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        print("打开进程失败：请以管理员身份运行（权限不足）", flush=True)
        sys.exit(1)

    # 遍历可读内存区域，先跑字符串候选
    k32.VirtualQueryEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                                   ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    tried = 0
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                             ctypes.byref(mbi), ctypes.sizeof(mbi)):
        base = mbi.BaseAddress or 0  # 首区域 BaseAddress=0（NULL 显示为 None）
        addr = base + mbi.RegionSize
        if mbi.State != MEM_COMMIT:
            continue
        if mbi.Protect not in (PAGE_READWRITE, PAGE_WRITECOPY,
                               PAGE_EXECUTE_READWRITE):
            continue
        if mbi.RegionSize > 512 * 1024 * 1024:  # 跳过巨型映射区
            continue
        for cand in scan_strings(h, mbi.BaseAddress, mbi.RegionSize):
            tried += 1
            if tried % 100000 == 0:
                print(f"  已尝试 {tried} 个候选…", flush=True)
            if check_key(cand):
                hexkey = cand.hex()
                print(f"✅ 命中！AES 密钥: {hexkey}", flush=True)
                with open(KEY_PATH, "w", encoding="utf-8") as f:
                    json.dump({"key_hex": hexkey,
                               "sample": os.path.basename(sample_path)},
                              f, indent=2)
                print(f"已保存 → {KEY_PATH}", flush=True)
                k32.CloseHandle(h)
                return
    print(f"字符串扫描未命中（已试 {tried} 个候选）。", flush=True)
    print("请确认：1) 管理员运行 2) 微信里点开过几张图片再试", flush=True)
    k32.CloseHandle(h)
    sys.exit(1)


if __name__ == "__main__":
    main()
