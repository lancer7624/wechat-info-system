# -*- coding: utf-8 -*-
"""班次失败告警（run_analysis.bat 调用；claude 自动重试后仍失败时推飞书）
用法: python notify_fail.py <班次> <退出码>
      python notify_fail.py <班次> <退出码> --dry   # 只打印，不推送
"""
import os
import sys

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NAMES = {"noon": "午班", "evening": "晚班", "trip": "行程班", "daily": "日报班"}


def main():
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry"]
    batch = args[0] if len(args) > 0 else "?"
    rc = args[1] if len(args) > 1 else "?"
    name = NAMES.get(batch, batch)
    title = "⚠ 微信班次失败"
    body = ("%s（%s）自动重试后仍失败，退出码 %s。\n"
            "排查：analysis.log 尾部。" % (name, batch, rc))
    if dry:
        print("[dry] 将推送: %s | %s" % (title, body.replace("\n", " ")))
        return
    try:
        import feishu_notify
        ok, msg = feishu_notify.send_text(title, body)
        print("push " + ("ok" if ok else "fail: %s" % msg))
    except Exception as e:
        print("push 异常（不影响班次）: %s" % e)


if __name__ == "__main__":
    main()
