# -*- coding: utf-8 -*-
"""从 dump 扫描命中里验证 key:按频次降序,读窗口内容,页1魔数验证
用法: python wx2_verify_hits.py [topN]
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wx2_key_scan import verify_key  # noqa: E402

DUMP = os.path.join(HERE, "wx2_full.dmp")
HITS = os.path.join(HERE, "wx2_scan_hits.json")
KEY_OUT = os.path.join(HERE, "wx2_key.json")


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    hits = json.load(open(HITS, encoding="utf-8"))
    hits.sort(key=lambda x: -x["c"])
    print(f"命中 {len(hits)} 种,按频次验证 top {top_n}", flush=True)

    with open(DUMP, "rb") as f:
        for i, h in enumerate(hits[:top_n], 1):
            f.seek(h["o"])
            w = f.read(32)
            if len(w) < 32:
                continue
            ok = verify_key(w.hex())
            if ok:
                print(f"✓ 第 {i} 个(freq={h['c']})验证通过({ok})!", flush=True)
                print(f"  key={w.hex()}", flush=True)
                json.dump({"key_hex": w.hex(), "wxid": "<你的wxid二号>",
                           "validated_db": ok, "freq": h["c"]},
                          open(KEY_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                print(f"  已存 {KEY_OUT}", flush=True)
                return
            if i % 500 == 0:
                print(f"  已验证 {i}/{top_n}...", flush=True)
    print("top N 全灭。", flush=True)


if __name__ == "__main__":
    main()
