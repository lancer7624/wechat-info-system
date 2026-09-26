# -*- coding: utf-8 -*-
"""飞书推送命令行封装（headless 班次用）
用法: python feishu_push.py "标题" "内容"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: feishu_push.py <title> <body>")
        sys.exit(1)
    ok, msg = feishu_notify.send_text(sys.argv[1], sys.argv[2])
    print(("OK " if ok else "FAIL ") + msg)
    sys.exit(0 if ok else 1)
