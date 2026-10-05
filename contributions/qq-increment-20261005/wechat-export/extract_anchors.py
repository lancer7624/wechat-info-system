# -*- coding: utf-8 -*-
"""锚点提取：从 Weixin.dll 静态定位 SQLCipher 相关函数，生成 anchors_focus.json

思路：
1. 解析 PE64 节表 + .pdata 函数表（复用 phase2d_wide 的逻辑）
2. 在镜像里搜 SQLCipher / multiple ciphers 特征字符串（ASCII + UTF-16LE）
3. 在 .text 里找 rip-relative LEA 引用这些字符串的指令 → 反推所属函数 RVA
4. 产出 anchors_focus.json（rva / name / end 三字段）
"""
import struct
import json
import os
import bisect

DLL = r"D:\zizhuang\Weixin\4.1.13.65\Weixin.dll"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "anchors_focus.json")

# 特征字符串（SQLCipher 体系 + 作者 docstring 线索）
PATTERNS = [
    b"sqlcipher",
    b"cipher_migrate",
    b"ATTACH DATABASE",
    b"SQLite Encryption Extension",
    b"PRAGMA cipher",
    b"cipher_version",
    b"cipher_ctx",
    b"sqlite3_key",
    b"sqlite3_rekey",
    b"sqlcipher_export",
    b"migrate KEY",
    b"codec",
    b"x'%q'",
    b"key '%q'",
    b"pragma",
]

# crypto IV 常量（little-endian）——PBKDF2/HMAC 核心的指纹。
# 谁引用这些常量，谁就是 sha/hmac 实现，key 派生必经过。
IV_PATTERNS = {
    "sha1_iv": bytes.fromhex("67452301EFCDAB8998BADCFE10325476C3D2E1F0"),
    "sha256_iv": bytes.fromhex(
        "6A09E667BB67AE853C6EF372A54FF53A510E527F9B05688C1F83D9AB5BE0CD19"),
    "sha512_iv": bytes.fromhex(
        "6A09E667F3BCC908BB67AE8584CAA73B3C6EF372FE94F82B"
        "A54FF53A5F1D36F1510E527FADE682D19B05688C2B3E6C1F"
        "1F83D9ABFB41BD6B5BE0CD19137E2179"),
    "hmac_ipad36": bytes.fromhex("36363636363636363636363636363636"),
    "hmac_opad5c": bytes.fromhex("5C5C5C5C5C5C5C5C5C5C5C5C5C5C5C5C"),
}


def find_all(buf, pat):
    out = []
    st = 0
    while True:
        i = buf.find(pat, st)
        if i == -1:
            break
        out.append(i)
        st = i + 1
    return out


def main():
    data = open(DLL, "rb").read()

    # ---- PE 解析 ----
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    opt_off = e_lfanew + 24
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    magic = struct.unpack_from("<H", data, opt_off)[0]
    assert magic == 0x20B, "不是 PE64"
    num_sec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    sec_off = opt_off + opt_size

    secs = []
    for i in range(num_sec):
        o = sec_off + i * 40
        name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        vsize = struct.unpack_from("<I", data, o + 8)[0]
        va = struct.unpack_from("<I", data, o + 12)[0]
        raw_size = struct.unpack_from("<I", data, o + 16)[0]
        raw_off = struct.unpack_from("<I", data, o + 20)[0]
        secs.append((name, va, vsize, raw_off, raw_size))

    def rva_to_off(rva):
        for name, va, vsize, raw_off, raw_size in secs:
            if va <= rva < va + max(vsize, raw_size):
                return raw_off + (rva - va)
        return None

    def off_to_rva(off):
        for name, va, vsize, raw_off, raw_size in secs:
            if raw_off <= off < raw_off + raw_size:
                return va + (off - raw_off)
        return None

    # ---- .pdata 函数表 ----
    pdata = next(s for s in secs if s[0] == ".pdata")
    _, _, _, praw, prsize = pdata
    funcs = []
    for i in range(prsize // 12):
        begin, end, uw = struct.unpack_from("<III", data, praw + i * 12)
        if begin:
            funcs.append((begin, end))
    funcs.sort()
    begins = [b for b, e in funcs]

    def rva_to_func(rva):
        i = bisect.bisect_right(begins, rva) - 1
        if i >= 0 and funcs[i][0] <= rva < funcs[i][1]:
            return funcs[i]
        return None

    text = next(s for s in secs if s[0] == ".text")
    _, tva, tvsize, traw, trsize = text

    # ---- 字符串搜索（ASCII + UTF-16LE，覆盖 .rdata 及其他节）----
    str_offs = {}   # 文件偏移 -> 特征名
    pat_names = {}
    for pat in PATTERNS:
        hits = find_all(data, pat)
        pat_names[pat] = pat.decode("ascii", "replace")
        for h in hits:
            str_offs.setdefault(h, pat_names[pat])
        # UTF-16LE
        pat_u = pat.decode("ascii", "replace").encode("utf-16-le")
        for h in find_all(data, pat_u):
            str_offs.setdefault(h, pat_names[pat])
    # IV 常量（精确字节序列）
    for name, ivec in IV_PATTERNS.items():
        for h in find_all(data, ivec):
            str_offs.setdefault(h, name)
    print(f"字符串/IV 命中（去重）: {len(str_offs)} 处")
    # 偏移 -> rva 索引
    str_rvas = {}
    for off, nm in str_offs.items():
        r = off_to_rva(off)
        if r is not None:
            str_rvas[r] = nm
    str_rva_set = set(str_rvas)

    # ---- xref：.text 里 rip-relative LEA ----
    # REX.W LEA: 48/4C 8D, mod=00 rm=101, disp32
    anchors = {}
    i = 0
    td = data[traw:traw + trsize]
    while i < trsize - 7:
        if td[i] in (0x48, 0x4C) and td[i + 1] == 0x8D:
            modrm = td[i + 2]
            if (modrm & 0xC7) == 0x05:  # mod=00, rm=101
                disp = struct.unpack_from("<i", td, i + 3)[0]
                insn_rva = tva + i
                target = insn_rva + 7 + disp
                if target in str_rva_set:
                    f = rva_to_func(insn_rva)
                    if f:
                        b, e = f
                        if b not in anchors:
                            anchors[b] = {"rva": b, "name": f"lea:{str_rvas[target]}", "end": e}
        i += 1
    print(f"lea xref 命中函数: {len(anchors)} 个")

    # ---- xref：mov reg, [rip+disp] ----
    i = 0
    while i < trsize - 7:
        if td[i] in (0x48, 0x4C) and td[i + 1] == 0x8B:
            modrm = td[i + 2]
            if (modrm & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", td, i + 3)[0]
                insn_rva = tva + i
                target = insn_rva + 7 + disp
                if target in str_rva_set:
                    f = rva_to_func(insn_rva)
                    if f:
                        b, e = f
                        if b not in anchors:
                            anchors[b] = {"rva": b, "name": f"mov:{str_rvas[target]}", "end": e}
        i += 1
    print(f"mov xref 命中函数: {len(anchors)} 个")

    # ---- 通用 disp32 扫描：覆盖 SSE 加载（movdqu/movups 等）----
    # 逐字节把 disp32 当作 rip-relative 位移反推目标，命中的必是真引用
    # （随机 4 字节恰好落在 IV/字符串地址的概率极低）
    i = 0
    while i < trsize - 4:
        disp = struct.unpack_from("<i", td, i)[0]
        insn_rva = tva + i
        target = insn_rva + 4 + disp
        if target in str_rva_set:
            f = rva_to_func(insn_rva)
            if f:
                b, e = f
                if b not in anchors:
                    anchors[b] = {"rva": b, "name": f"ref:{str_rvas[target]}", "end": e}
        i += 1
    print(f"通用 disp32 xref 命中函数: {len(anchors)} 个")

    lst = sorted(anchors.values(), key=lambda a: a["rva"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"anchors": lst}, f, ensure_ascii=False, indent=1)
    print(f"写出 {len(lst)} 个锚点 → {OUT}")
    for a in lst[:20]:
        print(f"  0x{a['rva']:X} - 0x{a['end']:X}  {a['name']}")


if __name__ == "__main__":
    main()
