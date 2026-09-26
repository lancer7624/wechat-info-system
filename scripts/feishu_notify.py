# -*- coding: utf-8 -*-
"""飞书群机器人推送：POST webhook，飞书手机端收通知（线一活动提醒专用）
webhook 存 config.json 的 飞书webhook 字段。密钥值绝不进日志、绝不打印。
用法:
    python feishu_notify.py "标题" "正文"
退出码 0=成功，1=失败（失败原因只打 stderr，不含 webhook 本体）
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_JSON = os.path.join(ROOT, "config.json")


def _load_webhook():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        h = (cfg.get("飞书webhook") or "").strip()
        return h or None
    except Exception:
        return None


def send_text(title, body, hook=None):
    """发文本消息。返回 (ok, msg)。hook 不打印、不进返回值。"""
    hook = hook or _load_webhook()
    if not hook:
        return False, "config.json 未配置 飞书webhook"
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n{body}"},
    }
    req = urllib.request.Request(
        hook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("code") == 0:
            return True, "ok"
        return False, f"飞书返回 code={r.get('code')} msg={r.get('msg')}"
    except Exception as e:
        return False, f"请求失败: {type(e).__name__}"


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "测试"
    body = sys.argv[2] if len(sys.argv) > 2 else "微信信息管理系统推送测试"
    ok, msg = send_text(title, body)
    print("OK" if ok else f"FAIL: {msg}", file=sys.stderr if not ok else sys.stdout)
    sys.exit(0 if ok else 1)
