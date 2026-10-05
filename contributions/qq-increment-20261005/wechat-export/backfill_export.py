# -*- coding: utf-8 -*-
"""把 <src_date> 全量导出里时间戳为 <target_date> 的块,补写到 <target_date> 目录同名文件(去重追加)
用法: python backfill_export.py <target_date> <src_date>
"""
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
EXPORT = os.path.join(HERE, "export")
TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]")


def split_blocks(path, target):
    """返回 (其他行, 目标日期的块)"""
    keep, grab, cur_ts, cur_lines = [], [], None, []
    for line in open(path, encoding="utf-8"):
        m = TS.match(line.strip())
        if m:
            if cur_ts is not None and cur_lines:
                if cur_ts == target:
                    grab.extend(cur_lines)
                else:
                    keep.extend(cur_lines)
            cur_ts = m.group(1)
            cur_lines = [line.rstrip("\n")]
        else:
            cur_lines.append(line.rstrip("\n"))
    if cur_ts is not None and cur_lines:
        if cur_ts == target:
            grab.extend(cur_lines)
        else:
            keep.extend(cur_lines)
    return keep, grab


def main():
    target, src = sys.argv[1], sys.argv[2]
    src_dir = os.path.join(EXPORT, src)
    dst_dir = os.path.join(EXPORT, target)
    os.makedirs(dst_dir, exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(src_dir, "*.md")):
        name = os.path.basename(f)
        _, grab = split_blocks(f, target)
        if not grab:
            continue
        dst = os.path.join(dst_dir, name)
        existing = ""
        if os.path.exists(dst):
            existing = open(dst, encoding="utf-8").read()
        add = []
        for line in grab:
            if line.strip() and line.strip() not in existing:
                add.append(line)
        if add:
            with open(dst, "a", encoding="utf-8") as fw:
                fw.write("\n" + "\n".join(add) + "\n")
            n += len(add)
            print(f"{name}: +{len(add)} 行", flush=True)
    print(f"完成: {n} 行补进 {target}", flush=True)


if __name__ == "__main__":
    main()
