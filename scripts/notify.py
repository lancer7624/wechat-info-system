# -*- coding: utf-8 -*-
"""notify.py — 统一推送入口（班次/脚本都走这里，换通道不用改调用方）

按 config.json 顶层的「手机推送渠道」决定发去哪：
    飞书                 → scripts/feishu_notify.py（群机器人 webhook）
    微信                 → scripts/wx_bot.py（腾讯 iLink 微信机器人）
    双通道 / 都发 / 两个  → 两边都发（任一成功即算成功）

附件（--image / --file）只有微信通道支持；飞书 webhook 是纯文本通道。

用法:
    python notify.py "标题" "内容"
    python notify.py "标题" "内容" --image a.png --file b.pdf
    python notify.py "标题" "内容" --channel 微信      # 临时指定，不改配置
    python notify.py --status                         # 看各通道就绪情况

在代码里:
    import notify
    ok, msg = notify.send("标题", "正文", images=["C:\\\\a.png"])
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

WX_NAMES = ("微信", "微信机器人", "wxbot", "wx_bot", "ilink")
FS_NAMES = ("飞书", "feishu", "lark")
BOTH_NAMES = ("双通道", "都发", "两个", "both", "全发")


def load_conf(path=None):
    import json
    cands = [path, os.environ.get("WX_NOTIFY_CONF"), os.path.join(ROOT, "config.json")]
    for c in cands:
        if c and os.path.exists(c):
            with open(c, encoding="utf-8-sig") as f:
                conf = json.load(f)
            conf["_path"] = os.path.abspath(c)
            return conf
    return {}


def channels_of(conf, override=None):
    """把配置里的渠道描述解析成 [通道名, ...]"""
    raw = override if override is not None else conf.get("手机推送渠道")
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        text = str(raw or "").strip()
        items = [x for x in re.split(r"[+,，、/ ]+", text) if x]
    if not items:
        return ["飞书"]                      # 缺省保持原行为
    out = []
    for it in items:
        low = it.lower()
        if low in [n.lower() for n in BOTH_NAMES]:
            for n in ("飞书", "微信"):
                if n not in out:
                    out.append(n)
            continue
        if low in [n.lower() for n in WX_NAMES]:
            if "微信" not in out:
                out.append("微信")
            continue
        if low in [n.lower() for n in FS_NAMES]:
            if "飞书" not in out:
                out.append("飞书")
            continue
        # 认不出的写法：原样留着，交给下面报错
        out.append(it)
    return out


def _send_feishu(title, body, log=None):
    try:
        import feishu_notify
    except Exception as e:
        return False, "飞书通道不可用（%s）" % type(e).__name__
    try:
        return feishu_notify.send_text(title, body)
    except Exception as e:
        return False, "飞书发送异常：%s: %s" % (type(e).__name__, e)


def _send_wx(title, body, images=None, files=None, conf_path=None, log=None):
    try:
        sys.path.insert(0, HERE)
        import wx_push
    except Exception as e:
        return False, "微信通道不可用（%s）" % type(e).__name__
    try:
        return wx_push.send_text(title, body, images=images, files=files,
                                 conf_path=conf_path, log=log)
    except Exception as e:
        return False, "微信发送异常：%s: %s" % (type(e).__name__, e)


def send(title, body, images=None, files=None, channel=None, conf_path=None, log=None):
    """按配置把消息推出去。返回 (ok, msg)"""
    conf = load_conf(conf_path)
    chans = channels_of(conf, channel)
    results, ok_any = [], False
    for ch in chans:
        if ch == "飞书":
            if images or files:
                results.append("飞书: 跳过附件（webhook 只发文本）")
            ok, m = _send_feishu(title, body, log)
        elif ch == "微信":
            ok, m = _send_wx(title, body, images, files, conf_path, log)
        else:
            ok, m = False, "认不出的推送渠道：%s" % ch
        results.append("%s: %s" % (ch, m))
        ok_any = ok_any or ok
    return ok_any, "；".join(results)


def status(conf_path=None):
    conf = load_conf(conf_path)
    print("=== 推送通道状态 ===")
    print("  配置：", conf.get("_path") or "(没找到 config.json)")
    print("  配置渠道：", conf.get("手机推送渠道") or "(未设置，缺省飞书)")
    print("  实际解析：", " + ".join(channels_of(conf)))
    wh = str(conf.get("飞书webhook") or "")
    print("  飞书 webhook：", ("已填 " + wh[:28] + "…") if wh and "REPLACE" not in wh else "未填/占位")
    try:
        import wx_bot
        bot = wx_bot.WxBot(conf, poll=False)
        print("  微信机器人：", bot.describe())
        print("  微信媒体：", "cryptography 已装" if wx_bot._has_crypto() else "缺 cryptography")
    except Exception as e:
        print("  微信机器人：加载失败 %s: %s" % (type(e).__name__, e))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="统一推送入口（飞书 / 微信机器人）")
    ap.add_argument("title", nargs="?", default="", help="标题（可空）")
    ap.add_argument("body", nargs="?", default="", help="正文")
    ap.add_argument("--image", action="append", default=[], help="附带的图片（仅微信通道）")
    ap.add_argument("--file", action="append", default=[], help="附带的文件（仅微信通道）")
    ap.add_argument("--channel", default=None, help="临时指定通道：飞书 / 微信 / 双通道")
    ap.add_argument("--conf", default=None, help="config.json 路径")
    ap.add_argument("--status", action="store_true", help="看各通道就绪情况")
    a = ap.parse_args()
    if a.status:
        status(a.conf)
        return
    if not a.title and not a.body and not a.image and not a.file:
        ap.print_help()
        sys.exit(2)
    ok, msg = send(a.title, a.body, a.image, a.file, channel=a.channel, conf_path=a.conf)
    print(("OK " if ok else "FAIL ") + msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
