# -*- coding: utf-8 -*-
"""从 VSCode 用户 settings.json 读取 claude-code.environmentVariables。
输出 NAME=VALUE 行供 run_analysis.bat 设置环境变量。仅输出环境块，不输出任何日志。
"""
import json
import os
import sys


def strip_jsonc(s):
    """去掉 JSONC 注释（状态机版，字符串内的 // 不受影响）"""
    out = []
    i, n = 0, len(s)
    in_str = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i += 2
            while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def main():
    path = os.path.join(os.environ.get("APPDATA", ""), "Code", "User", "settings.json")
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    cfg = json.loads(strip_jsonc(raw))
    env = cfg.get("claude-code.environmentVariables")
    if env is None:
        env = (cfg.get("claude-code") or {}).get("environmentVariables")
    if not isinstance(env, dict):
        sys.exit(1)
    for k, v in env.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
