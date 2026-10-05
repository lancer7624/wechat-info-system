# -*- coding: utf-8 -*-
"""QQ NT 解密 v4:passphrase 形态变体 + no_kdf + 库同款 HMAC 消息格式"""
import sys
import struct

sys.stdout.reconfigure(encoding="utf-8")
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import HMAC, SHA512, SHA1
from Crypto.Cipher import AES

FULL = bytes.fromhex("1d2a8f4f1c1237d89684b2c2c4725bd30391f0c6dcdb3cb4ce6ab9a679cd74a5")
DB_PATH = r"C:\Users\<你的用户名>\Documents\Tencent Files\<你的QQ号>\nt_qq\nt_db\nt_msg.db"


def try_keys():
    # passphrase 形态:32B 全 / 16B 前半 / 16B 后半 / 16B 前后半各试
    return [
        ("full32", FULL),
        ("first16", FULL[:16]),
        ("last16", FULL[16:]),
    ]


def main():
    raw = open(DB_PATH, "rb").read()
    body = raw[1024:]
    page = body[:4096]
    salt = page[:16]
    data = page[16:4048]
    iv = page[4048:4064]
    tag = page[4064:4084]

    for pname, passw in try_keys():
        for no_kdf in (False, True):
            if no_kdf:
                enc_key = passw[:32].ljust(32, b"\x00")
                mac_key_cands = [("raw", passw[:32].ljust(32, b"\x00"))]
            else:
                for kdf_iter in (4000,):
                    enc_key = PBKDF2(passw, salt, dkLen=32, count=kdf_iter, hmac_hash_module=SHA512)
                    salt_xor = bytes(b ^ 0x3A for b in salt)
                    mac_key_cands = [
                        ("mk-sha512", PBKDF2(enc_key, salt_xor, dkLen=32, count=2, hmac_hash_module=SHA512)),
                        ("mk-sha1", PBKDF2(enc_key, salt_xor, dkLen=32, count=2, hmac_hash_module=SHA1)),
                    ]
            for mkname, mac_key in mac_key_cands:
                msg = data + iv + struct.pack("<I", 1)
                h = HMAC.new(mac_key[:20], msg, SHA1).digest()
                if h == tag:
                    print("HIT! pass=%s no_kdf=%s mac=%s" % (pname, no_kdf, mkname))
                    pt = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(data)
                    print("pt:", "".join(chr(b) if 32 <= b < 127 else "." for b in pt[:96]))
                    return
    print("all failed")


if __name__ == "__main__":
    main()
