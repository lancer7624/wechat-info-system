# -*- coding: utf-8 -*-
"""把抓到的密钥导入为 db_key.json（两种来源自动识别）

用法（在本目录下跑）：
    python import_dbkey.py <wcdb-key-tool 输出的 all_keys.json>   # 4.1.14+ 新机型
    python import_dbkey.py <64位hex密钥>                          # 登录期抓到的 master key
    python import_dbkey.py <输入> --out <自定义输出路径>

导入后建议验证：
    python decrypt_db.py --count
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")


def import_all_keys(path, out):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    enc_map = {}
    for rel, info in d.items():
        if not isinstance(info, dict):
            continue
        enc = str(info.get("enc_key", "")).strip().lower()
        salt = str(info.get("salt", "")).strip().lower()
        if HEX64.fullmatch(enc) and HEX32.fullmatch(salt):
            enc_map[salt] = enc
    if not enc_map:
        sys.exit("[!] 这个 json 里没解析出任何有效条目"
                 "（要的是 wcdb-key-tool 的 all_keys.json）")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"enc_keys": enc_map}, f, indent=2)
    print(f"[OK] 已写入 {out}")
    print(f"     enc_keys 模式：{len(enc_map)} 个库的密钥（来源：{os.path.basename(path)}）")


def import_hex(hex_str, out):
    h = hex_str.strip().strip('"').lower()
    if not HEX64.fullmatch(h):
        sys.exit(f"[!] 不是 64 位十六进制密钥（32 字节）：{hex_str[:24]}...")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"key_hex": h}, f, indent=2)
    print(f"[OK] 已写入 {out}")
    print("     key_hex 模式：登录期抓到的 master key")


def main():
    ap = argparse.ArgumentParser(description="把抓到的密钥导入为 db_key.json")
    ap.add_argument("input", help="all_keys.json 路径，或 64 位十六进制密钥")
    ap.add_argument("--out", default=os.path.join(HERE, "db_key.json"))
    args = ap.parse_args()
    if os.path.isfile(args.input):
        import_all_keys(args.input, args.out)
    else:
        import_hex(args.input, args.out)
    print("下一步：python decrypt_db.py --count   （通过 = 密钥有效）")
    print("[!] db_key.json 是敏感文件，不要外传、不要入 git")


if __name__ == "__main__":
    main()
