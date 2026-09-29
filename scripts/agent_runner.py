# -*- coding: utf-8 -*-
"""分析班次的智能体调用器。

由 run_analysis.bat 调用：python agent_runner.py <班次> <提示词文件> <输出日志> [--append]
用哪个智能体读 config.json 的「智能体」节（可选）：
  {"名称": "Gemini CLI", "命令": "gemini", "参数": ["--yolo", "-p", "{prompt}"]}
没写这节就按 AGENT_ORDER 顺序自动探测本机可用的命令行智能体。
prompt 注入方式看「参数」：含 {prompt} 占位 = 提示词内容作为参数；不含 = 从标准输入喂。
退出码原样传回（找不到智能体/配置损坏 = 127），bat 靠它做重试与飞书告警。
"""
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 常见命令行智能体预设。各家 CLI 参数（尤其非 Claude）随版本可能变化：
# 跑不通就改 config.json「智能体」节。prompt 注入三选一：
#   参数含 {prompt}      = 提示词内容作为参数（.cmd/.bat 外壳下不能多行！）
#   参数含 {prompt_file} = 提示词文件绝对路径作为参数（适合支持读文件的 CLI，如 aider）
#   两者都不含           = 从标准输入喂（对 npm 的 .cmd 外壳最稳）
PRESETS = {
    "claude": {"名称": "Claude Code", "命令": "claude",
               "参数": ["-p", "--allowedTools", "Bash,PowerShell,Read,Write,Edit,Glob,Grep",
                        "--max-turns", "100", "--output-format", "text"]},
    "gemini": {"名称": "Gemini CLI", "命令": "gemini", "参数": ["--yolo"]},
    "codex": {"名称": "Codex CLI", "命令": "codex", "参数": ["exec"]},
    "cursor-agent": {"名称": "Cursor Agent", "命令": "cursor-agent", "参数": ["-p", "{prompt}"]},
    "aider": {"名称": "Aider", "命令": "aider", "参数": ["--yes", "--message-file", "{prompt_file}"]},
    "opencode": {"名称": "opencode", "命令": "opencode", "参数": ["run"]},
}
AGENT_ORDER = ["claude", "gemini", "codex", "cursor-agent", "aider", "opencode"]


def find_claude():
    """claude.exe：VSCode 扩展优先（目录名随扩展更新变，每次现找），其次 PATH。"""
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    hits = glob.glob(os.path.join(up, ".vscode", "extensions",
                                  "anthropic.claude-code-*",
                                  "resources", "native-binary", "claude.exe"))
    if hits:
        return sorted(hits)[-1]
    return shutil.which("claude")


def resolve_command(name):
    """把「命令」解析成可执行路径；claude 特判（VSCode 扩展动态定位）。找不到返回 None。"""
    if not name or not isinstance(name, str):
        return None
    if name == "claude":
        return find_claude()
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.exists(name) else None
    return shutil.which(name)


def load_agent():
    """返回 (智能体 dict 或 None, 来源/失败说明)。

    dict: {"名称": str, "exe": str, "参数": [str]}
    config.json 的「智能体」节有效则用它；没有/不完整则按 AGENT_ORDER 自动探测。
    """
    cfg_path = os.path.join(ROOT, "config.json")
    cfg = None
    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        cfg = None
    a = cfg.get("智能体") if isinstance(cfg, dict) else None
    if isinstance(a, dict) and isinstance(a.get("命令"), str) and a["命令"].strip():
        cmd = a["命令"].strip()
        params = a.get("参数")
        if not (isinstance(params, list) and all(isinstance(x, str) for x in params)):
            params = PRESETS.get(cmd, {}).get("参数", [])
        exe = resolve_command(cmd)
        name = a.get("名称") or PRESETS.get(cmd, {}).get("名称") or cmd
        if not exe:
            return None, "config.json「智能体」里的命令找不到: " + cmd
        return {"名称": name, "exe": exe, "参数": list(params)}, "config.json「智能体」"
    for key in AGENT_ORDER:
        p = PRESETS[key]
        exe = resolve_command(p["命令"])
        if exe:
            return {"名称": p["名称"], "exe": exe, "参数": list(p["参数"])}, "自动探测"
    return None, "没探测到任何命令行智能体（" + "/".join(AGENT_ORDER) + "）"


def main(argv):
    batch = argv[0] if len(argv) > 0 else "noon"
    prompt = argv[1] if len(argv) > 1 else os.path.join(ROOT, "prompts", "prompt_%s.md" % batch)
    out_log = argv[2] if len(argv) > 2 else os.path.join(ROOT, "run_tmp_%s.log" % batch)
    append = "--append" in argv[3:]

    try:
        lf = open(out_log, "ab" if append else "wb")
    except Exception:
        return 127

    def log(msg):
        lf.write((msg + "\r\n").encode("utf-8"))
        lf.flush()

    agent, source = load_agent()
    if agent is None:
        log("[agent] error: " + source)
        log("[agent] 装一个命令行智能体（Claude Code / Gemini CLI / Codex 等），"
            "或在 config.json 加「智能体」节指定；见 README 常见问题。")
        lf.close()
        return 127

    use_arg = any("{prompt}" in p for p in agent["参数"])
    use_file = any("{prompt_file}" in p for p in agent["参数"])
    # npm 装的 CLI 在 Windows 上是 .cmd/.bat 外壳，CreateProcess 不能直接执行，需经 cmd /c
    exe = agent["exe"]
    shim = exe.lower().endswith((".cmd", ".bat"))
    cmdline = (["cmd.exe", "/c", exe] if shim else [exe])
    text = None
    if use_arg:
        try:
            with open(prompt, encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            log("[agent] error: 读不到提示词 %s: %s" % (prompt, e))
            lf.close()
            return 127
        if shim and "\n" in text:
            log("[agent] error: %s 是 .cmd/.bat 外壳，Windows 命令行传不了多行提示词。" % exe)
            log("[agent] 改 config.json「智能体」参数：删掉 {prompt} 改为 stdin 方式，"
                "或改用 {prompt_file} 传提示词文件路径（需该 CLI 支持读文件参数），"
                "或换非 npm 方式安装该 CLI。")
            lf.close()
            return 127
    for p in agent["参数"]:
        if "{prompt_file}" in p:
            p = p.replace("{prompt_file}", os.path.abspath(prompt))
        if "{prompt}" in p:
            p = p.replace("{prompt}", text)
        cmdline.append(p)
    mode = "参数" if use_arg else ("文件" if use_file else "stdin")
    log("[agent] %s (%s) | 来源: %s | prompt 方式: %s"
        % (agent["名称"], agent["exe"], source, mode))
    log("[agent] batch=" + batch + " prompt=" + os.path.basename(prompt))

    try:
        if use_arg or use_file:
            p = subprocess.run(cmdline, stdin=subprocess.DEVNULL, stdout=lf,
                               stderr=subprocess.STDOUT, cwd=ROOT)
        else:
            with open(prompt, "rb") as pin:
                p = subprocess.run(cmdline, stdin=pin, stdout=lf,
                                   stderr=subprocess.STDOUT, cwd=ROOT)
    except OSError as e:
        log("[agent] error: 启动失败 %s: %s" % (agent["exe"], e))
        lf.close()
        return 127
    lf.close()
    return p.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
