# -*- coding: utf-8 -*-
"""微信新消息活动检测 + 桌面通知（纯本地，零 AI 成本）
在 daily_export.py 导出完成后调用：
1. 对比 activity_state.json（按天维度，{date, counts:{会话hash:消息数}, biz:N}）
2. 有新增消息 → Windows toast 通知（谁找你、几条）
3. 无新增 → 静默退出；跨天自动重建基线
可选 --ai：有新消息时调本机命令行智能体（自动探测，规则同 scripts/agent_runner.py）读正文生成一句话总结（默认关，隐私权衡）
用法: python activity_check.py [--ai] [--dry]（--dry 只打印不弹通知）
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "activity_state.json")
EXPORT_ROOT = os.path.join(HERE, "export")


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def latest_export():
    import re
    dirs = sorted([d for d in os.listdir(EXPORT_ROOT)
                   if os.path.isdir(os.path.join(EXPORT_ROOT, d))
                   and re.match(r"^\d{4}-\d{2}-\d{2}$", d)], reverse=True)
    return os.path.join(EXPORT_ROOT, dirs[0]) if dirs else None


def parse_md(path):
    """读一个会话 md，返回 (会话名, hash8, 消息数)"""
    with open(path, encoding="utf-8") as f:
        first = f.readline().strip()
        for _ in range(2):
            line = f.readline().strip()
            if "消息数:" in line:
                try:
                    cnt = int(line.split("消息数:")[1].split("（")[0].strip())
                except (IndexError, ValueError):
                    cnt = 0
                break
        else:
            cnt = 0
    return first, cnt


def detect_activity():
    """对比本次导出与状态（按天维度，跨天自动重建基线）
    返回 (新增总数, [(会话名, hash8, 新增数)], 新状态)"""
    out = latest_export()
    if not out:
        return 0, [], {}
    today = time.strftime("%Y-%m-%d")
    st = load_state()
    prev = st.get("counts", {}) if st.get("date") == today else {}
    new_st = {"date": today, "counts": {}, "biz": 0}
    total_new = 0
    changed = []
    for fn in sorted(os.listdir(out)):
        if not fn.endswith(".md") or fn.startswith("公众号_"):
            continue
        h = fn.rsplit("_", 1)[-1].split(".")[0]
        title, cnt = parse_md(os.path.join(out, fn))
        name = title.replace("# 会话:", "").split(" (")[0].strip()
        old = prev.get(h, 0)
        new_st["counts"][h] = cnt
        if cnt > old:
            changed.append((name, h, cnt - old))
            total_new += cnt - old
    # 订阅号推送计数（biz_articles.json 条目数）
    biz_path = os.path.join(out, "biz_articles.json")
    if os.path.exists(biz_path):
        try:
            with open(biz_path, encoding="utf-8") as f:
                biz_n = len(json.load(f))
            old_biz = st.get("biz", 0) if st.get("date") == today else 0
            new_st["biz"] = biz_n
            if biz_n > old_biz:
                changed.append(("订阅号推送", "biz", biz_n - old_biz))
                total_new += biz_n - old_biz
        except Exception:
            pass
    return total_new, changed, new_st


def build_message(total, changed):
    if not changed:
        return None, None
    top = changed[:3]
    head = f"微信新消息 +{total} 条"
    body_lines = [f"{n}（+{c}）" for n, _, c in top]
    if len(changed) > 3:
        body_lines.append(f"…等 {len(changed)} 个会话")
    return head, "；".join(body_lines)


def notify(title, body, dry=False):
    print(f"[通知] {title}\n  {body}", flush=True)
    if dry:
        return
    try:
        from win11toast import toast
        toast(title, body, duration="short")
    except Exception as e:
        print(f"toast 失败（不影响导出）: {e}", flush=True)


def pick_agent():
    """挑本机命令行智能体（复用 scripts/agent_runner.py 的探测与配置）。

    返回 (基础命令行, 参数模板, 名称)；不可用时返回 None（回退裸 claude -p）。
    """
    try:
        scripts = os.path.join(os.path.dirname(HERE), "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import agent_runner as AR
        agent, _src = AR.load_agent()
        if not agent:
            return None
        exe = agent["exe"]
        base = ["cmd.exe", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]
        return base, list(agent["参数"]), agent["名称"]
    except Exception:
        return None


def ai_summary(total, changed, dry=False):
    """调本机命令行智能体（自动探测）读新增消息正文生成一句话总结（可选，默认关）"""
    if dry:
        print("[AI] dry 模式跳过", flush=True)
        return None
    out = latest_export()
    if not out:
        return None
    # 只取有新增的前 3 个会话正文片段
    snippets = []
    for name, h, c in changed[:3]:
        p = os.path.join(out, f"{name}_{h}.md")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            snippets.append(f"== {name} ==\n" + "".join(lines[-20:]))
    if not snippets:
        return None
    prompt = ("以下是微信聊天记录导出文件的最新片段，"
              "请用一句中文总结有什么需要用户关注的事（谁、什么事），"
              "不超过 40 字，没有值得关注的就说'无'。\n\n" +
              "\n".join(snippets)[:4000])
    tmp_path = None
    try:
        picked = pick_agent()
        if picked:
            base, params, name = picked
            use_arg = any("{prompt}" in p for p in params)
            use_file = any("{prompt_file}" in p for p in params)
            if use_file:
                tf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                                 encoding="utf-8", newline="")
                tf.write(prompt)
                tf.close()
                tmp_path = tf.name
            cmd = list(base)
            for p in params:
                if "{prompt_file}" in p:
                    p = p.replace("{prompt_file}", tmp_path)
                if "{prompt}" in p:
                    p = p.replace("{prompt}", prompt)
                cmd.append(p)
            print(f"[AI] 智能体: {name}", flush=True)
        else:
            cmd, use_arg, use_file = ["claude", "-p", prompt], True, False
        kw = {"stdin": subprocess.DEVNULL} if (use_arg or use_file) else {"input": prompt}
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace", **kw)
        s = r.stdout.strip()
        return s if s and s != "无" else None
    except Exception as e:
        print(f"AI 总结失败: {e}", flush=True)
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def main():
    dry = "--dry" in sys.argv
    use_ai = "--ai" in sys.argv
    today = time.strftime("%Y-%m-%d")
    first_run = (load_state().get("date") != today)
    total, changed, new_st = detect_activity()
    if first_run:
        save_state(new_st)
        print(f"当天首次运行：已建立基线（{len(new_st.get('counts', {}))} 个会话），"
              f"不通知", flush=True)
        return
    if total == 0 or not changed:
        print("无新增消息，静默", flush=True)
        return
    save_state(new_st)

    title, body = build_message(total, changed)
    if use_ai:
        s = ai_summary(total, changed, dry)
        if s:
            body = (body + "\n" + s) if body else s
    notify(title, body, dry)


if __name__ == "__main__":
    main()
