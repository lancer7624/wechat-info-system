# -*- coding: utf-8 -*-
"""微信公众号文章导出（独立工具，2026-09-29）

biz_message_0.db + contact.db（取名字）→ 白名单过滤
  → export/<日期>/biz_articles.json + 公众号_<名>.md

⚠️ 公众号中文名必须走 contact 库的 name_map（remark / nick_name），
   不要去解析 message_content 里 XML 的 nickname——微信 4.x 那些中文字段
   是 GBK 字节，按 UTF-8 解会出乱码扩展字符（х/涛/犭/懋…）；而且多数条目
   根本没那些 tag，硬提只会 fallback 成 gh_xxx，白名单（中文名归一化）
   一条都匹配不上。contact 库里的名字是干净的。

用法（在 wechat-export 目录里跑）：
  python export_biz.py                  # 只用 config.json 白名单
  python export_biz.py --all            # 不过滤，全量导出
  python export_biz.py --out DIR        # 自定义输出目录
  python export_biz.py --db-root DIR    # 手动指定微信 db_storage
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from decrypt_db import decrypt_db, load_master            # noqa: E402
from export_chat import (load_name_map, export_biz_decrypted,  # noqa: E402
                         norm_name)

SNAPSHOT = [
    os.path.join("contact", "contact.db"),
    os.path.join("message", "biz_message_0.db"),
]


def find_config(cli=None):
    if cli:
        return cli
    # 源系统形态：daily_export.py 里 CONFIG_JSON 是写死的字面路径
    p = os.path.join(HERE, "daily_export.py")
    try:
        with open(p, encoding="utf-8") as f:
            m = re.search(r'^CONFIG_JSON\s*=\s*r"([^"]+)"', f.read(), re.M)
        if m and os.path.isfile(m.group(1)):
            return m.group(1)
    except OSError:
        pass
    # 分享包形态：config.json 在 wechat-export 的上级目录
    for c in (os.path.join(os.path.dirname(HERE), "config.json"),
              os.path.join(HERE, "config.json")):
        if os.path.isfile(c):
            return c
    return None


def load_biz_whitelist(cfg_path):
    """→ ({归一化名: 类别}, [原始名...])；没配置 → (None, [])"""
    if not cfg_path:
        return None, []
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    out, raw = {}, []
    for cat, names in (cfg.get("公众号") or {}).items():
        for n in names or []:
            n = str(n).strip()
            if n:
                out[norm_name(n)] = cat
                raw.append(n)
    return (out or None), raw


def find_db_root(cli=None):
    if cli:
        return cli
    p = os.path.join(HERE, "daily_export.py")
    try:
        with open(p, encoding="utf-8") as f:
            m = re.search(r'^DB_ROOT\s*=\s*r"([^"]+)"', f.read(), re.M)
        if m and "YOURNAME" not in m.group(1) and os.path.isdir(m.group(1)):
            return m.group(1)
    except OSError:
        pass
    base = os.path.join(os.path.expanduser("~"), "Documents",
                        "xwechat_files")
    hits = sorted(glob.glob(os.path.join(
        base, "*", "db_storage", "message", "biz_message_0.db")))
    if hits:
        return os.path.dirname(os.path.dirname(hits[0]))
    raise SystemExit(
        "找不到微信 db_storage（默认在 文档\\xwechat_files\\<wxid>\\db_storage）。"
        "用 --db-root 手动指定。")


def snapshot(db_root, tmp):
    got = []
    for rel in SNAPSHOT:
        src = os.path.join(db_root, rel)
        if not os.path.isfile(src):
            print(f"[警告] 库里没有这个文件，跳过: {src}")
            continue
        dst = os.path.join(tmp, os.path.basename(rel))
        shutil.copy2(src, dst)
        for suf in ("-wal", "-shm"):
            if os.path.isfile(src + suf):
                shutil.copy2(src + suf, dst + suf)
        got.append(os.path.basename(rel))
    if "biz_message_0.db" not in got:
        raise SystemExit("没有 biz_message_0.db，无法导出")
    return got


def report(arts, name_map, whitelist, raw_wl, out_dir):
    per = {}
    for a in arts:
        k = a.get("name") or "?"
        per[k] = per.get(k, 0) + 1
    print(f"\n导出 {len(per)} 个公众号 / {len(arts)} 条推送 → {out_dir}")
    for k in sorted(per, key=lambda x: -per[x]):
        print(f"  {k}  ({per[k]} 条)")

    bad = [k for k in per if k.startswith(("gh_", "wxid_")) or k == "?"]
    if bad:
        print(f"\n[警告] {len(bad)} 个号名字没取到（显示成 ID）：{'、'.join(bad)}")
        print("       说明 contact 库里没有对应记录 → 白名单（中文名）匹配不上。")

    if whitelist:
        vals = {norm_name(v) for v in name_map.values()}
        missing = [n for n in raw_wl if norm_name(n) not in vals]
        if missing:
            print(f"\n[提示] 白名单里这些号在 contact 库中找不到（未关注/名字变过）："
                  f"{'、'.join(missing)}")
        zero = [n for n in raw_wl if norm_name(n) not in
                {norm_name(k) for k in per}]
        if zero:
            print(f"[提示] 本次 0 条推送的白名单号（可能只是没新消息）："
                  f"{'、'.join(zero)}")


def main():
    ap = argparse.ArgumentParser(description="公众号文章导出（白名单过滤）")
    ap.add_argument("--all", action="store_true", help="不过滤，全量导出")
    ap.add_argument("--out", help="输出目录（默认 export/<今天>）")
    ap.add_argument("--db-root", help="微信 db_storage 目录")
    ap.add_argument("--config", help="config.json 路径（默认自动定位）")
    args = ap.parse_args()

    db_root = find_db_root(args.db_root)
    key_path = os.path.join(HERE, "db_key.json")
    if not os.path.isfile(key_path):
        raise SystemExit(f"没有 {key_path}，先用向导/import_dbkey.py 生成")
    master = load_master(key_path)
    print(f"微信库: {db_root}")

    out_dir = args.out or os.path.join(HERE, "export",
                                       time.strftime("%Y-%m-%d"))
    cfg = find_config(args.config)
    if args.all:
        whitelist, raw_wl = None, []
        print("模式: 全量导出（忽略白名单）")
    else:
        whitelist, raw_wl = load_biz_whitelist(cfg)
        if whitelist is None:
            print("[警告] config.json 里没配「公众号」名单 → 退化成全量导出")
        else:
            print(f"白名单: {len(raw_wl)} 个（来自 {cfg}）")

    tmp = tempfile.mkdtemp(prefix="biz_export_")
    try:
        print("快照中…", flush=True)
        snapshot(db_root, tmp)

        name_map = {}
        try:
            cdata = decrypt_db(os.path.join(tmp, "contact.db"), master,
                               verify_mac=False, use_wal=False)
            name_map = load_name_map(cdata)
            print(f"contact 库: {len(name_map)} 个名字映射")
        except Exception as e:
            print(f"[警告] contact 库解密失败: {e}")
            print("       没有名字映射 → 名字会显示成 gh_xxx，白名单匹配失效")
        if not name_map:
            print("[警告] name_map 为空！公众号名将全部 fallback 成 ID，"
                  "白名单（中文名）必然 0 命中")

        print("解密 biz_message_0.db（30 秒左右）…", flush=True)
        bdata = decrypt_db(os.path.join(tmp, "biz_message_0.db"), master,
                           verify_mac=False, use_wal=False)

        acc, cnt = export_biz_decrypted(bdata, out_dir, name_map=name_map,
                                        whitelist=whitelist)
        json_p = os.path.join(out_dir, "biz_articles.json")
        with open(json_p, encoding="utf-8") as f:
            arts = json.load(f)
        report(arts, name_map, whitelist, raw_wl, out_dir)
        print(f"\n完成：{acc} 个公众号 {cnt} 条 → {json_p}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
