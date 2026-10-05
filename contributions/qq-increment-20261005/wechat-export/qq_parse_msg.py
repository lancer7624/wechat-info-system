# -*- coding: utf-8 -*-
"""QQ NT group_msg_table 消息体 protobuf 解析:提取文本元素"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
import sqlcipher3

KEY_HEX = "1d2a8f4f1c1237d89684b2c2c4725bd30391f0c6dcdb3cb4ce6ab9a679cd74a5"


def varint(data, i):
    v, shift = 0, 0
    while i < len(data):
        b = data[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return v, i


def walk_pb(data):
    out = []
    i = 0
    n = len(data)
    while i < n:
        tag, i = varint(data, i)
        field, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = varint(data, i)
            val = data[i:i + ln]
            i += ln
            try:
                s = val.decode("utf-8")
                has_cjk = any("一" <= ch <= "鿿" for ch in s)
                if has_cjk or (len(s) > 1 and all(32 <= ord(ch) < 127 for ch in s)):
                    out.append((field, "str", s))
            except Exception:
                out.append((field, "bin", val[:16].hex()))
        elif wt == 0:
            v, i = varint(data, i)
            out.append((field, "varint", v))
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            break
    return out


def main():
    con = sqlcipher3.connect("qq_nt_msg_stripped.db")
    cur = con.cursor()
    cur.execute("PRAGMA key = \"x'%s'\"" % KEY_HEX)
    cur.execute("PRAGMA kdf_iter = 4000")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")

    cur.execute("SELECT 40021, 40050, 40090, 40800 FROM group_msg_table "
                "WHERE 40800 IS NOT NULL LIMIT 10")
    for nick, ts, gname, blob in cur.fetchall():
        if not isinstance(blob, bytes):
            continue
        fields = walk_pb(blob)
        strs = [f[2] for f in fields if f[1] == "str"]
        print("ts=%s 群=%s 发=%s" % (ts, gname, nick))
        print("  文本:", strs[:5])
        if not strs:
            print("  原始字段:", fields[:10])


if __name__ == "__main__":
    main()
