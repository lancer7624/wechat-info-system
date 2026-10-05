# -*- coding: utf-8 -*-
"""微信疑似新置顶检测:白名单外的会话今天有消息 → 提示用户确认是否加入白名单。
微信置顶名单加密读不到,用活跃度反推:用户新置顶的群大概率会去看/有消息。
用法: python detect_new_pinned.py
"""
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(BASE, "wechat-export", "export")
CFG = os.path.join(BASE, "config.json")
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]")

WX_TARGETS = ["示例通知群", "示例班级群", "示例群A", "示例群B",
              "示例群C", "示例群D", "示例竞赛群", "示例线下群"]
# 用户已确认不是白名单的会话,永远不报
IGNORE = ["示例群E", "<你确认忽略的会话名>"]


def main():
    whitelist = []
    try:
        cfg = json.load(open(CFG, encoding="utf-8"))
        whitelist = [str(x) for x in cfg.get("群聊", [])]
    except Exception:
        pass

    import datetime
    today = datetime.date.today().isoformat()
    hits = []
    for d in (today,):
        for f in glob.glob(os.path.join(EXPORT, d, "*.md")):
            name = os.path.basename(f).split("_")[0]
            if not name:
                continue
            if any(t in name for t in IGNORE):
                continue
            matched = any(t in name for t in WX_TARGETS) or \
                      any(t in name for t in whitelist)
            if matched:
                continue
            n = 0
            for line in open(f, encoding="utf-8"):
                if line.startswith("[" + d):
                    n += 1
            if n >= 1:
                hits.append((name, n))
    hits.sort(key=lambda x: -x[1])
    if not hits:
        print("白名单外无活跃会话(今天)", flush=True)
        return
    print("白名单外今天有消息的会话(疑似新置顶,需用户确认):", flush=True)
    for name, n in hits[:12]:
        print("  %d 条 | %s" % (n, name), flush=True)
    print("", flush=True)
    print("把名单报给用户:新增置顶的回复群名,老子加白名单;没有就忽略", flush=True)


if __name__ == "__main__":
    main()
