# -*- coding: utf-8 -*-
"""wx_serve.py — 微信机器人双向服务

挂着长轮询收你在微信发给机器人的消息（文字 / 图片 / 文件），交给本机命令行智能体处理，
把回复发回微信。回复正文里单独一行写「[发图] 绝对路径」/「[发文件] 绝对路径」，
机器人就会把文件当附件发给你。

用法:
    python scripts/wx_serve.py             # 前台常驻（能看日志，Ctrl+C 退出）
    python scripts/wx_serve.py --once      # 只跑一轮（调试用）

配置（config.json 顶层「微信机器人」节里的「双向」子节）：
    "双向": {
        "启用": true,
        "白名单": [],
        "提示词": "prompts/prompt_wxchat.md",
        "历史轮数": 4,
        "超时秒": 600
    }

白名单留空 = 只服务配对时扫码的那个微信号。
"""
import os
import sys
import json
import time
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wx_bot  # noqa: E402

DEFAULT_PROMPT = os.path.join(ROOT, "prompts", "prompt_wxchat.md")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class WxServe:
    def __init__(self, conf, log=print):
        self.conf = conf or {}
        self.log = log
        cfg = self.conf.get("微信机器人") or {}
        chat = cfg.get("双向") or {}
        self.chat = chat
        self.enabled = bool(chat.get("启用", True))
        self.allow = [str(x) for x in (chat.get("白名单") or []) if x]
        self.prompt_file = str(chat.get("提示词") or "").strip() or DEFAULT_PROMPT
        self.history_turns = int(chat.get("历史轮数") or 4)
        self.timeout = int(chat.get("超时秒") or 600)
        self.state_dir = str(cfg.get("账号目录") or "").strip() or os.path.join(ROOT, "bot")
        self.hist_file = os.path.join(self.state_dir, "chat_history.json")
        self.bot = wx_bot.WxBot(conf, log=log, poll=True, on_message=self.on_message)
        self._busy = False

    # ---- 白名单 ----
    def allowed(self, peer):
        if self.allow:
            return peer in self.allow
        return peer == self.bot.peer

    # ---- 历史 ----
    def _hist(self):
        try:
            with open(self.hist_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_hist(self, peer, user_text, ai_text):
        h = self._hist()
        turns = h.get(peer) or []
        turns.append({"t": _now(), "user": user_text[:2000], "ai": (ai_text or "")[:2000]})
        h[peer] = turns[-max(self.history_turns * 2, 4):]
        try:
            os.makedirs(os.path.dirname(self.hist_file), exist_ok=True)
            with open(self.hist_file, "w", encoding="utf-8") as f:
                json.dump(h, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- 组装提示词 ----
    def build_prompt(self, peer, text, attachments):
        try:
            with open(self.prompt_file, encoding="utf-8") as f:
                base = f.read()
        except Exception as e:
            base = ("你是「微信信息管理系统」的微信通道助手。用简体中文简短回答，"
                    "别超过 200 字。（读不到提示词文件 %s：%s）" % (self.prompt_file, e))
        base = base.replace("<SKILL_DIR>", ROOT)
        hist = (self._hist().get(peer) or [])[-self.history_turns:]
        lines = [base, "", "---", "## 最近的对话"]
        if hist:
            for turn in hist:
                lines.append("用户：%s" % turn.get("user", "").replace("\n", " "))
                lines.append("你：%s" % (turn.get("ai", "") or "(没回)").replace("\n", " "))
        else:
            lines.append("（无）")
        lines += ["", "## 用户刚发来的消息", text or "（没有文字，只有附件）"]
        if attachments:
            lines += ["", "## 附件（已存在本机，可直接读）"]
            for a in attachments:
                if a.get("path"):
                    name = ("（原名：%s）" % a["name"]) if a.get("name") else ""
                    lines.append("[%s] %s%s" % (a.get("kind") or "附件", a["path"], name))
                else:
                    lines.append("[%s] 没取到文件（%s）" % (a.get("kind") or "附件",
                                                          a.get("note") or ""))
        return "\n".join(lines)

    # ---- 调命令行智能体 ----
    def run_agent(self, prompt_text):
        import agent_runner as AR
        agent, source = AR.load_agent()
        if not agent:
            return False, "（没探测到命令行智能体：%s）" % source
        exe, params = agent["exe"], list(agent["参数"])
        shim = exe.lower().endswith((".cmd", ".bat"))
        cmd = (["cmd.exe", "/c", exe] if shim else [exe])
        use_arg = any("{prompt}" in p for p in params)
        use_file = any("{prompt_file}" in p for p in params)
        tmp_path = None
        for p in params:
            if "{prompt_file}" in p:
                tf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                                 encoding="utf-8", newline="")
                tf.write(prompt_text)
                tf.close()
                tmp_path = tf.name
                p = p.replace("{prompt_file}", tmp_path)
            if "{prompt}" in p:
                p = p.replace("{prompt}", prompt_text)
            cmd.append(p)
        try:
            if use_arg or use_file:
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                   cwd=ROOT, timeout=self.timeout)
            else:
                r = subprocess.run(cmd, input=prompt_text.encode("utf-8"),
                                   capture_output=True, cwd=ROOT, timeout=self.timeout)
            out = (r.stdout or b"").decode("utf-8", "replace").strip()
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            if r.returncode != 0 and not out:
                return False, "（智能体退出码 %d）%s" % (r.returncode, err[-300:])
            return True, out
        except subprocess.TimeoutExpired:
            return False, "（智能体超时 %d 秒）" % self.timeout
        except Exception as e:
            return False, "（调用智能体失败 %s: %s）" % (type(e).__name__, e)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---- 收到消息 ----
    def on_message(self, bot, msg):
        peer = msg.get("peer") or ""
        if not self.enabled:
            return
        if not self.allowed(peer):
            self.log("[serve] 忽略非白名单 %s：%s" % (wx_bot._mask(peer),
                                                    (msg.get("text") or "")[:40]))
            return
        if self._busy:
            self.log("[serve] 上一条还在处理，本轮跳过（可调「超时秒」）")
            return
        self._busy = True
        t0 = time.time()
        try:
            text = msg.get("text") or ""
            atts = msg.get("attachments") or []
            low = text.strip().lower()
            # 快捷指令先兜掉，省得动智能体
            if low in ("状态", "status", "ping"):
                reply = "通道正常 ✅\n%s" % bot.status_line()
                bot.send_text("", reply)
                return
            if low in ("帮助", "help", "?"):
                bot.send_text("", "直接说事就行。可以问我：今天的通知/待审清单/行程。\n"
                                  "回复「全过」「删3号」可以过审。")
                return
            prompt_text = self.build_prompt(peer, text, atts)
            self.log("[serve] 交给智能体处理 %d 字…" % len(prompt_text))
            ok, out = self.run_agent(prompt_text)
            if not ok:
                bot.send_text("", out)
                return
            body, sends = wx_bot.parse_send_directives(out or "", base_dir=ROOT)
            if body.strip():
                bot.send_text("", body)
            for s in sends:
                if not os.path.isfile(s["path"]):
                    bot.send_text("", "（想发的%s没找到：%s）" % (s["kind"], s["raw"]))
                    continue
                ok2, m2 = bot.send_media(s["path"], s["caption"] or None)
                if not ok2:
                    bot.send_text("", "（附件发送失败：%s）" % m2)
            if not body.strip() and not sends:
                bot.send_text("", "（处理完了，但没有产出回复）")
            self._save_hist(peer, text or "[附件]", body)
            self.log("[serve] 完成，用时 %.1fs" % (time.time() - t0))
        except Exception as e:
            self.log("[serve] 处理异常：%s: %s" % (type(e).__name__, e))
            try:
                bot.send_text("", "（处理出错了：%s）" % type(e).__name__)
            except Exception:
                pass
        finally:
            self._busy = False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="微信机器人双向服务")
    ap.add_argument("--conf", default=None, help="config.json 路径")
    ap.add_argument("--once", action="store_true", help="只验证配置后退出（调试）")
    a = ap.parse_args()

    conf = wx_bot.load_conf(a.conf)
    srv = WxServe(conf)
    print("=== 微信机器人双向服务 ===")
    print("  配置：", conf.get("_path"))
    print("  通道：", srv.bot.describe())
    print("  提示词：", srv.prompt_file, "(有)" if os.path.exists(srv.prompt_file) else "(缺)")
    print("  白名单：", ("、".join(srv.allow) if srv.allow else "只服务配对号"))
    if not srv.bot.ready():
        print("[!] 通道未就绪：" + srv.bot.describe())
        print("    先跑 python scripts/wx_bot.py --login 配对，并在 config.json 里开启「微信机器人」")
        sys.exit(1)
    import agent_runner as AR
    agent, src = AR.load_agent()
    print("  智能体：", ("%s（%s）" % (agent["名称"], src)) if agent else ("没探测到（%s）" % src))
    if a.once:
        print("[*] --once：配置检查完毕，不启动长轮询")
        return
    print("[*] 长轮询启动，给你自己发条消息试试（Ctrl+C 退出）")
    srv.bot.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        srv.bot.stop()


if __name__ == "__main__":
    main()
