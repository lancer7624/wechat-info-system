# -*- coding: utf-8 -*-
"""微信 4.x 图片 .dat 解密（纯离线）
支持三种格式（据公开研究 ZedeX/weixin-decrypte-script 验证）：
- 旧版单字节 XOR：密钥从图片魔数反推（JPEG FF D8 FF / PNG / GIF / BMP / WebP）
- V1（07 08 56 31 08 07）：AES-128-ECB（固定密钥）+ 尾部 XOR(0x88)
- V2（07 08 56 32 08 07）：AES-128-ECB（密钥在微信进程内存，一次性提取）
                          + 尾部 XOR(0xED，本机从缩略图 FF D9 尾反推验证)
文件结构（V1/V2，15 字节头）：
  magic(6B) | aes_size(4B LE) | xor_size(4B LE) | 占位(1B)
  body = AES段(aligned 16B) + 原始段 + XOR段
"""
import os
import struct

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

V1_AES_KEY = bytes.fromhex("cfcd208495d565ef")  # md5("0")[:16]，固定值
V1_XOR_KEY = 0x88
V2_XOR_KEY = 0xED  # 本机实测：3 个会话缩略图尾部 FF D9 反推一致

MAGIC_EXT = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG", ".png"),
    (b"RIFF", ".webp"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
]


def detect_ext(data):
    """按魔数判断图片扩展名，未知返回 .bin"""
    for m, e in MAGIC_EXT:
        if data[:len(m)] == m:
            return e
    return ".bin"


def detect_xor_key(data, default=V2_XOR_KEY):
    """从文件尾部反推 XOR 密钥（JPEG 尾 FF D9）。
    仅对缩略图可靠；原图（WxAM 压缩）尾部无特征码，用全局 key。"""
    if len(data) >= 2:
        k = data[-1] ^ 0xD9
        if data[-2] ^ k == 0xFF:
            return k
    return default


def decrypt_dat(data, aes_key=None, xor_key=None):
    """解密单个 .dat，返回 (明文字节, 格式名)
    aes_key: V2 必需（16B，从微信内存提取）；V1/旧版不需要
    xor_key: 覆盖自动检测（默认 None=自动）"""
    if len(data) < 15:
        raise ValueError("文件太小，不是微信 dat 格式")

    magic = data[:6]
    if magic in (b"\x07\x08V2\x08\x07", b"\x07\x08V1\x08\x07"):
        fmt = "V2" if magic[3:4] == b"2" else "V1"
        if fmt == "V2" and not aes_key:
            raise ValueError("V2 需要 AES 密钥（内存提取，见 find_image_key.py）")
        key = aes_key if fmt == "V2" else V1_AES_KEY
        _, aes_size, xor_size = struct.unpack("<6sLLx", data[:15])
        # 头里 aes_size 是明文大小；密文 = 明文+PKCS7 填充（整块时多占一块）
        aligned = aes_size + (16 - aes_size % 16)
        body = data[15:]
        if aligned + xor_size > len(body):
            raise ValueError("头字段与文件大小不符")
        aes_plain = unpad(AES.new(key, AES.MODE_ECB).decrypt(
            body[:aligned]), 16)
        raw = body[aligned:len(body) - xor_size]
        xk = xor_key if xor_key is not None else (
            V2_XOR_KEY if fmt == "V2" else V1_XOR_KEY)
        xor_plain = bytes(b ^ xk for b in body[-xor_size:])
        return aes_plain + raw + xor_plain, fmt

    # 旧版单字节 XOR：用各图片魔数首字节反推密钥并校验第 2 字节
    if xor_key is None:
        for cand, second in ((0xFF, 0xD8), (0x89, 0x50), (0x47, 0x49),
                             (0x42, 0x4D), (0x52, 0x49)):
            k = data[0] ^ cand
            if data[1] ^ k == second:
                xor_key = k
                break
        else:
            raise ValueError("无法从文件头反推 XOR 密钥（未知格式）")
    return bytes(b ^ xor_key for b in data), "XOR"


def decrypt_file(src, dst, aes_key=None, xor_key=None):
    """解密文件到 dst（自动加扩展名）。返回实际输出路径"""
    with open(src, "rb") as f:
        data = f.read()
    plain, fmt = decrypt_dat(data, aes_key=aes_key, xor_key=xor_key)
    ext = detect_ext(plain)
    out = dst + ext
    with open(out, "wb") as f:
        f.write(plain)
    return out


if __name__ == "__main__":
    # 冒烟测试：解密一个缩略图（XOR 部分，无 AES 密钥时中间段为空）
    import sys
    p = sys.argv[1]
    with open(p, "rb") as f:
        data = f.read()
    try:
        plain, fmt = decrypt_dat(data)
        print(f"{os.path.basename(p)}: 格式={fmt} 大小={len(plain)} "
              f"扩展名={detect_ext(plain)}")
    except ValueError as e:
        print(f"{os.path.basename(p)}: 需要 AES 密钥 - {e}")
