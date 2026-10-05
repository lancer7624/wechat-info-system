# -*- coding: utf-8 -*-
"""每周漏消息审查:扫最近 N 天微信+QQ 白名单群的所有 @所有人/@全体成员 通知,
和已推送记录(待审文件+看板 data.json 的 quotes/text)比对,输出漏网候选。

用法: python audit_weekly.py [天数(默认7)]
输出: 审计报告\<日期>_每周审查报告.txt + stdout
"""
import datetime
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WX_ROOT = os.path.join(BASE, "wechat-export", "export")
QQ_ROOT = os.path.join(BASE, "wechat-export", "qq_export")
PENDING = os.path.join(BASE, "待审")
KANBAN = os.path.join(BASE, "kanban", "data.json")
REPORT_DIR = os.path.join(BASE, "审计报告")

WX_TARGETS = ["示例通知群", "示例班级群", "示例群A", "示例群B",
              "示例群C", "示例群D", "示例竞赛群", "示例线下群"]
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]")
AT_RE = re.compile(r"@所有人|@全体成员")


def norm(s):
    return re.sub(r"\s+", "", s or "")


def load_pushed():
    """已推送记录:待审文件全部 quotes/text + 看板全部 quotes/text"""
    pushed = []
    for f in glob.glob(os.path.join(PENDING, "*待审.json")):
        try:
            doc = json.load(open(f, encoding="utf-8"))
            for key in ("notify", "brief_chat", "brief_mp", "activities_new"):
                for e in doc.get(key, []):
                    pushed.append(e.get("text", ""))
                    for q in e.get("quotes", []):
                        pushed.append(q)
        except Exception:
            pass
    try:
        d = json.load(open(KANBAN, encoding="utf-8"))
        for section in [d.get("today", {})] + [h.get("data", {}) for h in d.get("history", [])]:
            for key in ("notify", "brief_chat", "brief_mp", "activities_new"):
                for e in section.get(key, []):
                    pushed.append(e.get("text", ""))
                    for q in e.get("quotes", []):
                        pushed.append(q)
            for sched in section.get("schedule", []):
                for e in sched.get("items", []):
                    pushed.append(e.get("title", ""))
                    for q in e.get("quotes", []):
                        pushed.append(q)
    except Exception:
        pass
    return [norm(p) for p in pushed if p]


def load_watermarks():
    """读双平台水位,返回 {日期: HH:MM},只审水位之前的消息"""
    wm = {}
    for p, key in ((os.path.join(BASE, "analysis_watermark.json"), "wx"),
                   (os.path.join(BASE, "qq_analysis_watermark.json"), "qq")):
        try:
            d = json.load(open(p, encoding="utf-8"))
            if d.get("date"):
                wm[key] = (d["date"], d.get("last_ts", "00:00"))
        except Exception:
            pass
    return wm


def scan_notices(days):
    """扫最近 days 天,输出 (日期, 时间, 来源, 通知正文) 列表。
    只取水位之前的消息(水位之后=班次还没跑,不算漏)"""
    notices = []
    wm = load_watermarks()
    today = datetime.date.today()
    for root, targets in ((WX_ROOT, WX_TARGETS), (QQ_ROOT, None)):
        wkey = "wx" if targets else "qq"
        for i in range(days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            cutoff = None  # 该日水位时间,之前才审
            if wm.get(wkey) and wm[wkey][0] == d:
                cutoff = wm[wkey][1]
            for f in glob.glob(os.path.join(root, d, "*.md")):
                name = os.path.basename(f).split(".")[0]
                if targets and not any(t in name.split("_")[0] for t in targets):
                    continue
                lines = open(f, encoding="utf-8").read().splitlines()
                blocks, cur = [], None
                for line in lines:
                    m = TS_RE.match(line.strip())
                    if m:
                        cur = [line]; blocks.append(cur)
                    elif cur is not None and line.strip():
                        cur.append(line)
                for b in blocks:
                    m2 = TS_RE.match(b[0].strip())
                    if not m2 or m2.group(1) != d:
                        continue
                    ts = m2.group(2)
                    if cutoff and ts >= cutoff:
                        continue  # 水位之后,班次会处理
                    text = " ".join(b[1:])
                    if not AT_RE.search(text):
                        continue
                    body = AT_RE.sub("", text).strip(" ,，:：~～")
                    if len(body) < 6:
                        continue
                    src = "微信:" + name if targets else "QQ:" + name
                    notices.append((d, ts, src, body))
    return notices


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    pushed = load_pushed()
    notices = scan_notices(days)
    # 通知正文被任何已推送 quote/text 覆盖(归一化子串)就不算漏
    missed = []
    for d, ts, src, body in notices:
        n = norm(body)
        if any(n in p or p in n for p in pushed if len(p) >= 6):
            continue
        missed.append((d, ts, src, body))
    # 去重
    seen, uniq = set(), []
    for m in missed:
        k = m[3][:40]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(m)

    os.makedirs(REPORT_DIR, exist_ok=True)
    report = os.path.join(REPORT_DIR, "%s_每周审查报告.txt" % datetime.date.today())
    lines = ["每周漏消息审查报告", "日期: %s | 审查范围: 最近 %d 天 | 通知总数: %d | 漏网候选: %d"
             % (datetime.date.today(), days, len(notices), len(uniq)), ""]
    for d, ts, src, body in uniq:
        lines.append("[%s %s] %s" % (d, ts, src))
        lines.append("  " + body[:100])
        lines.append("")
    if not uniq:
        lines.append("✓ 无漏网候选:所有 @通知都已被推送记录覆盖")
    open(report, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    main()
