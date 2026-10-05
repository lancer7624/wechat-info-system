# -*- coding: utf-8 -*-
"""微信机器人推送命令行封装（headless 班次用）

用法:
    python wx_push.py "标题" "内容"
    python wx_push.py "标题" "内容" --image C:\\a.png
    python wx_push.py "标题" "内容" --file C:\\a.pdf
    python wx_push.py "标题" "内容" --image a.png --image b.png   # 多张

在代码里用:
    import wx_push
    ok, msg = wx_push.send_text("标题", "正文")
    ok, msg = wx_push.send_text("标题", "正文", images=["C:\\a.png"], files=["C:\\b.pdf"])
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx_bot  # noqa: E402


def _bot(conf_path=None, log=None):
    conf = wx_bot.load_conf(conf_path)
    return wx_bot.WxBot(conf, log=log, poll=False)


def send_text(title, body, images=None, files=None, conf_path=None, log=None):
    """发文本 + 可选附件。返回 (ok, msg)"""
    bot = _bot(conf_path, log)
    if not bot.enabled:
        return False, "微信机器人通道已关闭（config.json「微信机器人」→「启用」= false）"
    if not bot.account:
        return False, "微信机器人没配对：python scripts/wx_bot.py --login"
    texts_ok, msgs = True, []
    if (title or body or "").strip():
        texts_ok, m = bot.send_text(title, body)
        msgs.append(m)
    atts = list(images or []) + list(files or [])
    if atts:
        a_ok, m = bot.send_attachments(atts)
        msgs.append(m)
        texts_ok = texts_ok and a_ok
    return texts_ok, "；".join([m for m in msgs if m]) or "没内容可发"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="微信机器人推送")
    ap.add_argument("title", nargs="?", default="", help="标题（可空）")
    ap.add_argument("body", nargs="?", default="", help="正文")
    ap.add_argument("--image", action="append", default=[], help="附带的图片（可多次）")
    ap.add_argument("--file", action="append", default=[], help="附带的文件（可多次）")
    ap.add_argument("--conf", default=None, help="config.json 路径")
    a = ap.parse_args()
    if not a.title and not a.body and not a.image and not a.file:
        ap.print_help()
        sys.exit(2)
    ok, msg = send_text(a.title, a.body, a.image, a.file, conf_path=a.conf)
    print(("OK " if ok else "FAIL ") + msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
