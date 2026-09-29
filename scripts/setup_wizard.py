# -*- coding: utf-8 -*-
"""一键部署向导 —— 微信信息管理系统（脱敏版）

双击根目录 一键部署.bat，或在命令行运行：
    python scripts\\setup_wizard.py [选项]

自动完成：选解释器 -> 探测微信 -> 装依赖 -> 铺配置 + 替换占位符 ->
抓数据库密钥 -> 验证 + 首次导出 -> 起看板 -> 桌面快捷方式 -> 计划任务。

设计约束：
- 纯标准库（pip 装依赖之前就要能跑），兼容 Python 3.7+
- 子进程一律注入 PYTHONIOENCODING=utf-8（cp936 管道下特殊符号会崩）
- PowerShell 一律 -EncodedCommand（UTF-16LE base64），避免中文/引号转义问题
- 幂等：重复运行按文件现状跳过已完成的事；--dry-run 只打印不执行
"""
import argparse
import base64
import ctypes
import glob
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "logs")
STATE_FILE = os.path.join(ROOT, ".setup_state.json")

PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
if not os.path.isfile(PS):
    PS = "powershell"

FOLDERID_Documents = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"
FOLDERID_Desktop = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

# 桌面快捷方式图标；空 = 系统默认。imageres.dll,-1024 是图表类图标
ICON = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32", "imageres.dll") + ",-1024"

DRY = False
YES = False
NO_OPEN = False
SKIP_DEPS = False
SKIP_REPLACE = False
SKIP_KEYS = False
ROUTE = None
DESKTOP_DIR = None
NO_TASKS = False


# ---------------------------------------------------------------- 控制台输出

class C:
    _lf = None

    @classmethod
    def emit(cls, s=""):
        try:
            print(s)
        except Exception:
            pass
        if cls._lf is not None:
            try:
                cls._lf.write(s + "\n")
                cls._lf.flush()
            except Exception:
                pass

    @classmethod
    def banner(cls, s):
        cls.emit("")
        cls.emit("=" * 64)
        cls.emit("  " + s)
        cls.emit("=" * 64)

    @classmethod
    def step(cls, n, s):
        cls.emit("")
        cls.emit("--- [%s] %s %s" % (n, s, "-" * max(2, 56 - len(s))))

    @classmethod
    def ok(cls, s):
        cls.emit("   [OK] " + s)

    @classmethod
    def warn(cls, s):
        cls.emit("   [!!] " + s)

    @classmethod
    def err(cls, s):
        cls.emit("   [XX] " + s)

    @classmethod
    def info(cls, s):
        cls.emit("        " + s)

    @classmethod
    def cmd(cls, argv, cwd=None):
        line = " ".join(a if (" " not in a and '"' not in a) else '"%s"' % a for a in argv)
        if cwd:
            line += "   (cwd: %s)" % cwd
        cls.info("> " + line)

    @classmethod
    def ask(cls, prompt, default=""):
        if DRY or YES:
            cls.emit("    ?   %s => %s" % (prompt, default if default else "(跳过)"))
            return default
        try:
            s = input("    ?   %s " % prompt).strip()
        except EOFError:
            return default
        return s or default

    @classmethod
    def confirm(cls, prompt, default=True):
        if DRY or YES:
            cls.emit("    ?   %s => %s" % (prompt, "Y" if default else "N"))
            return default
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            try:
                s = input("    ?   %s %s " % (prompt, hint)).strip().lower()
            except EOFError:
                return default
            if not s:
                return default
            if s in ("y", "yes", "是", "1"):
                return True
            if s in ("n", "no", "否", "0"):
                return False

    @classmethod
    def choose(cls, prompt, options, default=0):
        for i, o in enumerate(options):
            cls.emit("        %d) %s" % (i + 1, o))
        if DRY or YES:
            cls.emit("    ?   %s => %d" % (prompt, default + 1))
            return default
        while True:
            try:
                s = input("    ?   %s [1-%d，回车=%d] " % (prompt, len(options), default + 1)).strip()
            except EOFError:
                return default
            if not s:
                return default
            if s.isdigit() and 1 <= int(s) <= len(options):
                return int(s) - 1


def open_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, "setup_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
        C._lf = open(p, "w", encoding="utf-8")
    except Exception:
        C._lf = None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    if DRY:
        return
    d["root"] = ROOT
    d["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------------------------------------------- 子进程工具

def child_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_capture(argv, cwd=None, timeout=120, enc="utf-8"):
    """跑命令收集输出。返回 (rc, text)。124=超时，127=命令不存在"""
    argv = [str(a) for a in argv]
    if DRY:
        C.cmd(argv, cwd)
        return 0, ""
    try:
        p = subprocess.run(argv, cwd=cwd, env=child_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode(enc, "replace")
    except subprocess.TimeoutExpired:
        return 124, "[timeout %ss]" % timeout
    except FileNotFoundError:
        return 127, "[not found] " + argv[0]
    except Exception as e:
        return 126, str(e)


def run_stream(argv, cwd=None, timeout=1800):
    """跑命令并逐行实时转发输出（用于长任务）。返回 rc"""
    argv = [str(a) for a in argv]
    if DRY:
        C.cmd(argv, cwd)
        return 0
    C.cmd(argv, cwd)
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=child_env(),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        C.err("命令不存在: " + argv[0])
        return 127
    q = queue.Queue()

    def _rd():
        try:
            for ln in iter(p.stdout.readline, b""):
                q.put(ln)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=_rd, daemon=True).start()
    t0 = time.time()
    while True:
        try:
            ln = q.get(timeout=1.0)
        except queue.Empty:
            if timeout and time.time() - t0 > timeout:
                C.err("超时（%d 秒），已终止子进程" % timeout)
                try:
                    p.kill()
                except Exception:
                    pass
                return 124
            continue
        if ln is None:
            break
        C.emit("        | " + ln.rstrip(b"\r\n").decode("utf-8", "replace"))
    return p.wait()


def psq(s):
    return "'" + str(s).replace("'", "''") + "'"


def ps_b64(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def run_ps(script, timeout=120, enc="gbk"):
    return run_capture([PS, "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", ps_b64(script)], timeout=timeout, enc=enc)


def _read_text_any(path):
    for enc in ("utf-16", "utf-8"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except (OSError, UnicodeError):
            continue
    return ""


def _tail(path, seen):
    try:
        text = _read_text_any(path)
        lines = text.splitlines()
    except Exception:
        return seen
    for ln in lines[seen:]:
        C.emit("        | " + ln)
    return len(lines)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(ps_lines, cwd, timeout=1800, tag="elevated"):
    """提权运行一段 PowerShell（UAC 弹窗）。
    返回 None=UAC 被拒/无法启动；-1=超时；其它=退出码。
    输出写入 logs\\<tag>_<时间>.log，退出码写入同名 .rc"""
    if DRY:
        C.cmd([PS + " (runas)"], cwd)
        C.emit("        [dry] " + " ; ".join(ps_lines))
        return 0
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    ts = time.strftime("%Y%m%d_%H%M%S")
    logf = os.path.join(LOG_DIR, "%s_%s.log" % (tag, ts))
    rcf = os.path.join(LOG_DIR, "%s_%s.rc" % (tag, ts))
    inner = ["$ErrorActionPreference = 'Continue'",
             "Set-Location -LiteralPath " + psq(cwd),
             "Start-Transcript -LiteralPath " + psq(logf) + " -Force | Out-Null"]
    inner += ps_lines
    inner += ["$rc = $LASTEXITCODE",
              "Stop-Transcript | Out-Null",
              "Set-Content -LiteralPath " + psq(rcf) + " -Value $rc -Encoding ascii"]
    script = "\r\n".join(inner) + "\r\n"
    try:
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", PS,
            "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + ps_b64(script),
            cwd, 1)
    except Exception:
        return None
    if r <= 32:
        return None
    C.info("已提权运行，实时日志: %s" % logf)
    seen = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isfile(rcf):
            time.sleep(1.0)
            _tail(logf, seen)
            try:
                with open(rcf, encoding="ascii", errors="replace") as f:
                    txt = f.read().strip()
                return int(txt)
            except Exception:
                return -1
        if time.time() - t0 > 3:
            seen = _tail(logf, seen)
        time.sleep(2)
    C.warn("提权任务超时（%d 秒；也可能是 UAC 弹窗没点是）。完整日志: %s" % (timeout, logf))
    return -1


# ---------------------------------------------------------------- 系统探测

def known_folder(guid_str):
    try:
        import uuid
        g = uuid.UUID(guid_str)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        guid = GUID(g.time_low, g.time_mid, g.time_hi_version,
                    (ctypes.c_ubyte * 8)(*g.bytes[8:]))
        ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(ptr))
        if hr == 0 and ptr.value:
            path = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path
    except Exception:
        pass
    return None


def probe_py(exe):
    try:
        p = subprocess.run(
            [exe, "-c",
             "import sys;print('%d.%d.%d %d'%(sys.version_info[0],sys.version_info[1],"
             "sys.version_info[2],64 if sys.maxsize>2**32 else 32))"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=25)
        s = p.stdout.decode("utf-8", "replace").strip().split()
        if p.returncode == 0 and len(s) == 2:
            v = tuple(int(x) for x in s[0].split("."))
            return v, int(s[1])
    except Exception:
        pass
    return None


def list_py_candidates():
    cands = {}

    def add(path):
        path = str(path).strip().strip('"')
        if not path or not path.lower().endswith(".exe") or not os.path.isfile(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in cands:
            return
        info = probe_py(path)
        if info:
            cands[key] = (os.path.abspath(path), info[0], info[1])

    add(sys.executable)
    for cmd in (["py", "-0p"], ["where", "python"], ["where", "python3"]):
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=20)
            for line in p.stdout.decode("gbk", "replace").splitlines():
                m = re.search(r"([A-Za-z]:\\.+?\.exe)\s*$", line.strip())
                if m:
                    add(m.group(1))
        except Exception:
            pass
    return list(cands.values())


def _valid_install(d):
    return bool(d) and os.path.isdir(d) and os.path.isfile(os.path.join(d, "Weixin.exe"))


def parse_ver(s):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", s or "")
    if m:
        return tuple(int(x) for x in m.groups())
    return None


def _ver_dirs(root):
    out = []
    try:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if (os.path.isdir(full) and re.fullmatch(r"\d+(\.\d+){2,3}", name)
                    and os.path.isfile(os.path.join(full, "Weixin.dll"))):
                out.append((tuple(int(x) for x in name.split(".")), name, full))
    except OSError:
        pass
    out.sort()
    return out


def detect_wechat_root():
    cands = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
                          (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\Weixin"),
                          (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Tencent\Weixin")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    for v in ("InstallPath", "InstallDir", "Path", "WeixinPath"):
                        try:
                            val = winreg.QueryValueEx(k, v)[0]
                            if isinstance(val, str) and val:
                                cands.append(val)
                        except OSError:
                            pass
            except OSError:
                pass
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                def q(v2):
                                    try:
                                        return winreg.QueryValueEx(sk, v2)[0]
                                    except OSError:
                                        return ""

                                disp = str(q("DisplayName"))
                                low = disp.lower()
                                if "微信" in disp or "weixin" in low or "wechat" in low:
                                    for v2 in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                        cands.append(str(q(v2)))
                        except OSError:
                            continue
            except OSError:
                pass
    except Exception:
        pass
    rc, out = run_capture(
        ["powershell", "-NoProfile", "-Command",
         "$p=Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object -First 1;"
         "if($p){$p.Path}"], timeout=25, enc="gbk")
    if out.strip():
        cands.append(out.strip().splitlines()[-1])
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for b in (pf, pf86):
        cands.append(os.path.join(b, "Tencent", "Weixin"))
        cands.append(os.path.join(b, "Tencent", "WeChat"))
    cands.append(r"C:\Tencent\Weixin")
    cands.append(r"D:\Tencent\Weixin")
    norm = []
    for c in cands:
        c = c.strip().strip('"')
        if not c:
            continue
        if c.lower().endswith(".exe"):
            c = os.path.dirname(c)
        for t in (c, os.path.join(c, "Weixin")):
            if _valid_install(t):
                norm.append(t)
                break
    seen = set()
    roots = []
    for n in norm:
        k = os.path.normcase(os.path.abspath(n))
        if k not in seen:
            seen.add(k)
            roots.append(os.path.abspath(n))
    return roots


def detect_wechat():
    info = {"root": None, "version": None, "ver_tuple": None, "exe": None, "dll": None}
    for r in detect_wechat_root():
        vs = _ver_dirs(r)
        if vs:
            vt, vs_str, vdir = vs[-1]
            info.update(root=r, version=vs_str, ver_tuple=vt,
                        exe=os.path.join(r, "Weixin.exe"),
                        dll=os.path.join(vdir, "Weixin.dll"))
            break
    if not info["root"]:
        roots = detect_wechat_root()
        if roots:
            r = roots[0]
            info.update(root=r, exe=os.path.join(r, "Weixin.exe"))
            rc, out = run_ps("(Get-Item " + psq(info["exe"]) + ").VersionInfo.FileVersion",
                             timeout=40)
            m = re.search(r"\d+\.\d+\.\d+(\.\d+)?", out or "")
            if m:
                info["version"] = m.group(0)
    if info["root"] and not info["dll"]:
        flat = os.path.join(info["root"], "Weixin.dll")
        if os.path.isfile(flat):
            info["dll"] = flat
        elif info["version"]:
            vd = os.path.join(info["root"], info["version"])
            if os.path.isfile(os.path.join(vd, "Weixin.dll")):
                info["dll"] = os.path.join(vd, "Weixin.dll")
    if info["version"] and not info["ver_tuple"]:
        info["ver_tuple"] = parse_ver(info["version"])
    return info


def detect_accounts():
    bases = []
    docs = known_folder(FOLDERID_Documents)
    if docs:
        bases.append(os.path.join(docs, "xwechat_files"))
    home = os.path.expanduser("~")
    bases.append(os.path.join(home, "xwechat_files"))
    bases.append(os.path.join(home, "Documents", "xwechat_files"))
    seen = set()
    accts = []
    for b in bases:
        if not os.path.isdir(b):
            continue
        try:
            names = sorted(os.listdir(b))
        except OSError:
            continue
        for n in names:
            full = os.path.join(b, n)
            if os.path.isdir(os.path.join(full, "db_storage")):
                k = os.path.normcase(full)
                if k not in seen:
                    seen.add(k)
                    accts.append((n, full))
    return accts


def wechat_running():
    rc, out = run_capture(["tasklist", "/FI", "IMAGENAME eq Weixin.exe",
                           "/FO", "CSV", "/NH"], timeout=25, enc="gbk")
    return "Weixin.exe" in out


# ---------------------------------------------------------------- 步骤 S0-S2

def detect_agents():
    """探测本机可用的命令行智能体（复用 agent_runner 的预设与顺序）。
    返回 [(key, 名称, exe路径), ...]；找不到返回 []。"""
    try:
        import agent_runner as AR
    except Exception:
        return []
    out = []
    for key in AR.AGENT_ORDER:
        p = AR.PRESETS[key]
        exe = AR.resolve_command(p["命令"])
        if exe:
            out.append((key, p["名称"], exe))
    return out


def s0_selfcheck():
    C.step("S0", "环境自检")
    if os.name != "nt":
        C.err("本向导只支持 Windows。")
        return False
    v = sys.version_info
    C.info("向导解释器 : Python %d.%d.%d (%s)" % (
        v[0], v[1], v[2], "64位" if sys.maxsize > 2**32 else "32位"))
    C.info("安装目录   : " + ROOT)
    if v < (3, 7):
        C.err("Python 版本过低，请先安装 3.10+：https://www.python.org/downloads/windows/")
        return False
    if v < (3, 10):
        C.warn("向导能跑，但系统推荐 Python 3.10+（密钥工具与部分依赖要求）。")
    for f in ("README.md", "prompts", "scripts", "kanban", "wechat-export"):
        if not os.path.exists(os.path.join(ROOT, f)):
            C.err("当前目录不像分享包根目录（缺 %s）。请对整个包运行向导。" % f)
            return False
    if "onedrive" in ROOT.lower():
        C.warn("安装目录在 OneDrive 同步范围里，可能被占用/路径漂移，建议挪到如 D:\\wechat-assistant。")
    try:
        t = os.path.join(ROOT, ".setup_write_test")
        if not DRY:
            with open(t, "w") as f:
                f.write("x")
            os.remove(t)
    except Exception:
        C.err("当前目录不可写（权限不足），请把包放到有写权限的目录再跑。")
        return False
    agents = detect_agents()
    if agents:
        C.ok("智能体: " + " / ".join("%s (%s)" % (a[1], a[2]) for a in agents))
    else:
        C.warn("没检测到任何命令行智能体（Claude Code / Gemini CLI / Codex 等）——分析班次会失败。")
        C.info("装一个并登录，或在 config.json「智能体」节指定；见 README 常见问题。")
    C.ok("自检通过")
    return True


def s1_python(state):
    C.step("S1", "选择 Python 解释器")
    cands = list_py_candidates()
    if not cands:
        C.err("没找到可用的 Python。请安装 3.10+（勾选 Add python.exe to PATH）：")
        C.info("https://www.python.org/downloads/windows/")
        return None
    cands.sort(key=lambda c: c[1], reverse=True)
    asc = sorted(range(len(cands)), key=lambda i: cands[i][1])
    rec = None
    for floor in ((3, 12), (3, 10)):
        for i in asc:
            if cands[i][1] >= floor:
                rec = i
                break
        if rec is not None:
            break
    if rec is None:
        rec = 0
    labels = []
    for i, (exe, ver, bits) in enumerate(cands):
        labels.append("%d.%d.%d (%d位)  %s%s" % (
            ver[0], ver[1], ver[2], bits, exe, "   <== 推荐" if i == rec else ""))
    sel = C.choose("用哪个解释器跑本系统（依赖会装进它；推荐 3.12）", labels, rec)
    py = cands[sel][0]
    ver = cands[sel][1]
    if ver < (3, 10):
        C.warn("所选解释器低于 3.10，密钥工具/部分依赖可能装不上（之后可 --skip-keys 或另装新版）。")
    C.ok("已选: " + py)
    state["py"] = py
    return py


def s2_wechat(ctx_state):
    C.step("S2", "探测微信与账号目录")
    info = detect_wechat()
    if info["root"]:
        C.ok("微信安装目录: " + info["root"])
        C.info("版本: " + (info["version"] or "未识别"))
        if info["dll"]:
            C.info("DLL : " + info["dll"])
    else:
        C.warn("没探测到微信安装目录（微信 4.x 装过但没有？）。")
        man = C.ask("可粘贴微信安装目录（如 C:\\Program Files\\Tencent\\Weixin），回车=跳过", "")
        if man:
            man = man.strip().strip('"')
            if _valid_install(man):
                info["root"] = man
                info["exe"] = os.path.join(man, "Weixin.exe")
                vs = _ver_dirs(man)
                if vs:
                    info.update(version=vs[-1][1], ver_tuple=vs[-1][0],
                                dll=os.path.join(vs[-1][2], "Weixin.dll"))
                C.ok("已使用: " + man)
            else:
                C.warn("该目录里没找到 Weixin.exe，按未探测到处理。")
    if info["root"] and not info["version"]:
        v = C.ask("没能识别微信版本号（微信 设置→关于微信 里看），可粘贴如 4.1.12.55，回车=留未知", "")
        if re.fullmatch(r"\d+(\.\d+){2,3}", v or ""):
            info["version"] = v
            info["ver_tuple"] = parse_ver(v)
            vd = os.path.join(info["root"], v)
            if os.path.isfile(os.path.join(vd, "Weixin.dll")):
                info["dll"] = os.path.join(vd, "Weixin.dll")
    accts = detect_accounts()
    acct = None
    if len(accts) == 1:
        acct = accts[0]
        C.ok("账号目录: " + acct[1])
        if not C.confirm("用这个账号目录？", True):
            acct = None
    elif len(accts) > 1:
        i = C.choose("检测到多个账号目录，选消息数据所在的那个：",
                     [a[1] for a in accts], 0)
        acct = accts[i]
        C.ok("账号目录: " + acct[1])
    if acct is None and not accts:
        C.warn("没找到 xwechat_files 账号目录（微信 4.x 在本机登录过才会生成）。")
    if acct is None:
        man = C.ask("可粘贴账号目录（含 db_storage 的那层，如 ...\\xwechat_files\\wxid_xxx），回车=跳过", "")
        if man:
            man = man.strip().strip('"')
            if os.path.isdir(os.path.join(man, "db_storage")):
                acct = (os.path.basename(man.rstrip("\\/")), man)
                C.ok("账号目录: " + acct[1])
            else:
                C.warn("该目录下没有 db_storage，按跳过处理。")
    ctx_state["wechat"] = info
    ctx_state["acct"] = list(acct) if acct else None
    if not info["root"] and not acct:
        C.warn("微信相关信息全跳过：密钥抓取与路径替换会在后续步骤自动略过。")
    return info, acct


# ---------------------------------------------------------------- S3 依赖

def s3_deps(py):
    C.step("S3", "安装依赖（pip）")
    if SKIP_DEPS:
        C.warn("--skip-deps 已跳过")
        return True
    req = os.path.join(ROOT, "requirements.txt")
    if not os.path.isfile(req):
        C.warn("没找到 requirements.txt，跳过。")
        return True
    C.info("首次安装需要几分钟（下载 frida/numpy 等）…")
    rc = run_stream([py, "-m", "pip", "install", "-r", req,
                     "--disable-pip-version-check"], timeout=3600)
    if rc != 0:
        C.warn("直连 PyPI 失败（rc=%s），换清华镜像重试…" % rc)
        rc = run_stream([py, "-m", "pip", "install", "-r", req,
                         "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                         "--disable-pip-version-check"], timeout=3600)
    if rc != 0:
        C.err("依赖安装失败。可稍后手动重跑本步，或检查网络/代理。")
        return False
    rc, out = run_capture([py, "-c",
                           "import frida, Crypto, win11toast, requests, keyboard, "
                           "sounddevice, numpy, zstandard; print('core deps ok')"],
                          timeout=180)
    if rc != 0:
        C.err("核心依赖自检失败：\n" + out.strip())
        return False
    C.ok("核心依赖 OK")
    rc, out = run_capture([py, "-c", "import faster_whisper; print('whisper ok')"],
                          timeout=300)
    if rc != 0:
        C.warn("faster-whisper 导入失败：录音转写不可用（看板与分析不受影响）。")
    else:
        C.ok("faster-whisper OK")
    return True


# ---------------------------------------------------------------- S4 配置+替换

def build_replacements(wechat, acct):
    reps = []
    if acct:
        name, full = acct
        reps.append((r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID",
                     full, "wechat-export/*.py", "账号目录"))
        reps.append(("wxid_YOURWXID", name, "wechat-export/*.py", "账号目录名(残留)"))
        reps.append((r"C:\Users\YOURNAME", os.path.expanduser("~"),
                     "wechat-export/*.py", "用户目录(残留)"))
    if wechat and wechat.get("root"):
        if wechat.get("dll"):
            reps.append((r"C:\Program Files\Tencent\Weixin\4.1.12.55\Weixin.dll",
                         wechat["dll"], "wechat-export/*.py", "phase2d DLL"))
            if wechat.get("version") == "4.1.13.61":
                reps.append((r"C:\Program Files\Tencent\Weixin\4.1.13.61\Weixin.dll",
                             wechat["dll"], "wechat-export/*.py", "hook_image_key3 DLL"))
        if wechat.get("exe"):
            reps.append((r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                         wechat["exe"], "wechat-export/*.py", "微信 EXE"))
    reps.append(("<SKILL_DIR>", ROOT, "prompts/*.md", "技能目录"))
    reps.append(("<SKILL_DIR>", ROOT, "CLAUDE.md", "会话守则"))
    return reps


def apply_replacements(reps, state):
    by_pattern = {}
    for old, new, pattern, note in reps:
        if old == new:
            continue
        by_pattern.setdefault(pattern, []).append((old, new, note))
    made = []
    scanned = 0
    for pattern, pairs in by_pattern.items():
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            base = os.path.basename(path)
            scanned += 1
            try:
                with open(path, encoding="utf-8", newline="") as f:
                    text = f.read()
            except Exception as e:
                C.warn("读不了 %s: %s" % (base, e))
                continue
            orig = text
            hits = []
            for old, new, note in pairs:
                n = text.count(old)
                if n:
                    text = text.replace(old, new)
                    hits.append("%s x%d" % (note, n))
            if text != orig:
                if DRY:
                    C.ok("[dry] " + base + " <- " + "、".join(hits))
                else:
                    with open(path, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
                    C.ok(base + " <- " + "、".join(hits))
                made.append(path)
            elif any(new in text for old, new, _ in pairs):
                C.info("[已有] " + base + "（此前已配置）")
    C.info("占位符扫描：共 %d 个文件，%d 个有改动" % (scanned, len(made)))
    if made:
        state.setdefault("replaces", [])
        for path in made:
            rel = os.path.relpath(path, ROOT)
            if rel not in state["replaces"]:
                state["replaces"].append(rel)
    return made


def s4_scaffold(wechat, acct, state):
    C.step("S4", "铺配置 + 替换占位符")
    copies = [("config.example.json", "config.json"),
              ("分类规则模板.md", "分类规则.md"),
              ("行程表模板.md", "行程表.md"),
              (os.path.join("kanban", "data.example.json"), os.path.join("kanban", "data.json"))]
    for src, dst in copies:
        s = os.path.join(ROOT, src)
        d = os.path.join(ROOT, dst)
        if os.path.exists(d):
            C.info("[已有] " + dst + "（不覆盖）")
        elif DRY:
            C.cmd(["copy", s, d])
        else:
            try:
                shutil.copyfile(s, d)
                C.ok("已生成 " + dst)
            except Exception as e:
                C.warn("复制失败 %s: %s" % (dst, e))
    for sub in ("待审", "待入库"):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            C.info("[已有] " + sub + "\\")
        elif DRY:
            C.cmd(["mkdir", d])
        else:
            try:
                os.makedirs(d, exist_ok=True)
                C.ok("已建目录 " + sub + "\\")
            except Exception as e:
                C.warn("建目录失败 %s: %s" % (sub, e))
    cfg = os.path.join(ROOT, "config.json")
    if os.path.isfile(cfg) and not DRY:
        with open(cfg, encoding="utf-8", newline="") as f:
            ctext = f.read()
        changed = False
        if "REPLACE_WITH_YOUR_WEBHOOK" in ctext:
            hook = C.ask("粘贴你的飞书机器人 webhook 地址（留空=稍后在 config.json 手填）", "")
            if hook:
                if "http" not in hook:
                    C.warn("这不像一个 URL，也已按原样写入。")
                ctext = ctext.replace("REPLACE_WITH_YOUR_WEBHOOK", hook)
                changed = True
                C.ok("webhook 已写入 config.json")
        m = re.search(r'"审核期结束日":\s*"([^"]*)"', ctext)
        if m and m.group(1).startswith("YYYY-MM-DD"):
            d3 = (date.today() + timedelta(days=3)).isoformat()
            d2 = C.ask("审核期结束日（回车=今天+3天）", d3)
            ctext = ctext.replace(m.group(0), '"审核期结束日": "%s"' % d2)
            changed = True
            C.ok("审核期结束日 = " + d2)
        if changed:
            with open(cfg, "w", encoding="utf-8", newline="") as f:
                f.write(ctext)
    elif DRY and os.path.isfile(cfg):
        C.info("[dry] 跳过 config.json 交互填写")
    if SKIP_REPLACE:
        C.warn("--skip-replace 已跳过占位符替换")
    else:
        reps = build_replacements(wechat, acct)
        apply_replacements(reps, state)
    if DRY:
        C.info("[dry] 跳过替换后残留自检（干跑不落盘）")
    else:
        tokens = ["<SKILL_DIR>", "YOURWXID", "YOURNAME"]
        bad = []
        for path in (glob.glob(os.path.join(ROOT, "prompts", "*.md"))
                     + glob.glob(os.path.join(ROOT, "CLAUDE.md"))
                     + glob.glob(os.path.join(ROOT, "wechat-export", "*.py"))):
            try:
                with open(path, encoding="utf-8") as f:
                    t = f.read()
            except Exception:
                continue
            for tok in tokens:
                if tok in t:
                    bad.append((os.path.relpath(path, ROOT), tok))
        if bad:
            C.warn("还有占位符没替换干净（可手动处理或重跑本步）：")
            for rel, tok in bad:
                C.info("  %s 里残留 %s" % (rel, tok))
        else:
            C.ok("占位符自检通过（无残留）")


# ---------------------------------------------------------------- S5 密钥

def keys_wcdb(py, acct):
    wex = os.path.join(ROOT, "wechat-export")
    tool = os.path.join(wex, "tools", "wcdb_key_tool_windows.py")
    if not os.path.isfile(tool):
        C.err("内置工具缺失: wechat-export\\tools\\wcdb_key_tool_windows.py")
        return "fail"
    if not DRY:
        if YES and not wechat_running():
            C.warn("--yes 模式下微信未运行，自动跳过 wcdb 路线（需要微信登录状态）。")
            return "skipped"
        tries = 0
        while not wechat_running() and tries < 3:
            tries += 1
            C.warn("微信没在运行。wcdb 路线需要微信登录状态（密钥在运行时内存里）。")
            if not C.confirm("现在去启动微信并登录，登录好之后选 Y 继续（选 N 跳过本步）", True):
                return "skipped"
            t0 = time.time()
            while not wechat_running() and time.time() - t0 < 180:
                time.sleep(5)
        if not wechat_running():
            C.err("仍未检测到 Weixin.exe 进程，跳过密钥步骤。")
            return "skipped"
    out_json = os.path.join(wex, "all_keys.json")
    argv = [py, tool, "extract", "--output", out_json]
    if acct:
        argv += ["--db-dir", os.path.join(acct[1], "db_storage")]
    if is_admin() or DRY:
        C.info("当前已是管理员，直接运行内置工具（只读扫描，不注入）…")
        rc = run_stream(argv, cwd=wex, timeout=1800)
    else:
        C.info("需要管理员权限（马上会弹 UAC，请选“是”）。工具只读扫描，不注入不改微信。")
        inner = "& " + psq(py) + " " + " ".join(psq(a) for a in argv[1:])
        rc = run_elevated([inner], cwd=wex, timeout=1800, tag="wcdb_extract")
        if rc is None:
            C.err("UAC 被拒绝或提权失败，密钥没抓到。降级方案：")
            C.info("  1) 以管理员身份开个终端，cd 到 wechat-export 手动跑:")
            C.info("     " + " ".join(argv))
            C.info("  2) 或改用 Frida 路线（微信 <= 4.1.13）")
            C.info("  3) 或先跳过，之后补（README 第 4 节）")
            return "fail"
    if DRY:
        return "dry"
    if rc == -1:
        C.err("提权运行超时（可能是 UAC 弹窗没点是，或工具卡住）。看 logs 里的提权日志。")
        return "fail"
    if rc != 0:
        C.err("wcdb-key-tool 退出码 %s（常见原因：微信未登录 / 弹窗没选“是” / 版本不符）" % rc)
        return "fail"
    if not os.path.isfile(out_json):
        C.err("没生成 all_keys.json，请看 logs 目录下的提权日志。")
        return "fail"
    rc2, out = run_capture([py, "import_dbkey.py", out_json], cwd=wex, timeout=120)
    C.emit((out or "").rstrip())
    if rc2 != 0:
        C.err("导入 db_key.json 失败（rc=%s）" % rc2)
        return "fail"
    C.ok("db_key.json 生成完毕")
    return "ok"


def keys_frida(py, acct):
    wex = os.path.join(ROOT, "wechat-export")
    if not os.path.isfile(os.path.join(wex, "anchors_focus.json")):
        C.err("缺 anchors_focus.json（Frida 路线必需），包不完整？")
        return "fail"
    tries = 0
    while wechat_running() and not DRY:
        tries += 1
        if tries > 3:
            C.warn("多次检测微信仍在运行，跳过 Frida 路线。")
            return "skipped"
        C.warn("Frida 路线要求微信完全退出（托盘右键→退出；多开账号都要退）。")
        i = C.choose("怎么处理？",
                     ["我已退干净了，重新检测",
                      "帮我强制结束微信（taskkill /IM Weixin.exe /F）",
                      "跳过 Frida 路线"], 2 if YES else 0)
        if i == 0:
            time.sleep(1)
            continue
        if i == 1:
            C.warn("正在强制结束微信…（未保存的输入会丢）")
            run_capture(["taskkill", "/IM", "Weixin.exe", "/F"], timeout=60, enc="gbk")
            time.sleep(2)
            continue
        return "skipped"
    if DRY:
        C.cmd([py, "phase2e_login.py"], wex)
        C.cmd([py, "phase3_verify3.py"], wex)
        return "dry"
    C.info("开始 phase2e_login.py：它会自动拉起微信 -> 请用手机扫码登录 ->")
    C.info("登录后脚本继续观察 120 秒（等登录最长 15 分钟），全程只读。")
    rc = run_stream([py, "phase2e_login.py"], cwd=wex, timeout=1500)
    if rc != 0:
        C.warn("phase2e_login 退出码 %s" % rc)
    if not os.path.isfile(os.path.join(wex, "candidates.json")):
        C.err("没产出 candidates.json。常见原因：frida 没装好 / 微信版本 > 4.1.13（要走 wcdb 路线）/ 没完成扫码。")
        return "fail"
    C.ok("候选密钥已捕获，开始离线验证（phase3_verify3.py）…")
    rc = run_stream([py, "phase3_verify3.py"], cwd=wex, timeout=900)
    if rc != 0 or not os.path.isfile(os.path.join(wex, "db_key.json")):
        C.err("验证没通过（rc=%s）。可重试本步；仍不行建议改走 wcdb 路线。" % rc)
        return "fail"
    C.ok("db_key.json 生成完毕")
    return "ok"


def s5_keys(py, wechat, acct):
    C.step("S5", "抓取数据库密钥")
    wex = os.path.join(ROOT, "wechat-export")
    if SKIP_KEYS:
        C.warn("--skip-keys 已跳过")
        return "skipped"
    if os.path.isfile(os.path.join(wex, "db_key.json")):
        C.ok("已存在 db_key.json")
        if not C.confirm("要重新抓取吗？", False):
            return "exists"
    if not acct and not (wechat and wechat.get("root")):
        C.err("微信信息没探测到，无法抓密钥。确认微信已登录后重跑向导。")
        return "fail"
    ver = (wechat or {}).get("version") or ""
    vt = (wechat or {}).get("ver_tuple")
    if ROUTE:
        route = ROUTE
        C.info("--route=%s 指定路线" % route)
    else:
        rec = 0 if (vt and vt >= (4, 1, 14)) else 1
        C.info("检测到微信版本 %s，推荐 %s 路线" % (ver or "未知",
              "wcdb-key-tool" if rec == 0 else "Frida"))
        route = ["wcdb", "frida", "none"][C.choose("选密钥提取路线",
            ["wcdb-key-tool：微信保持登录，管理员一次性扫描（推荐 4.1.14+）",
             "Frida：微信完全退出后扫码登录，只读 hook（适用 <=4.1.13）",
             "先跳过（之后按 README 第 4 节手动补）"], rec)]
    if route == "none":
        return "skipped"
    if route == "wcdb":
        return keys_wcdb(py, acct)
    return keys_frida(py, acct)


# ---------------------------------------------------------------- S6-S10

def s6_verify(py):
    C.step("S6", "验证解密 + 首次导出")
    wex = os.path.join(ROOT, "wechat-export")
    if not os.path.isfile(os.path.join(wex, "db_key.json")):
        C.warn("没有 db_key.json（密钥步骤跳过或失败），先跳过验证/首跑。")
        C.info("之后补好密钥手动跑：cd wechat-export && python decrypt_db.py --count && python daily_export.py")
        return "skipped"
    if SKIP_KEYS:
        C.warn("--skip-keys 已跳过")
        return "skipped"
    C.info("解密计数验证（decrypt_db.py --count，跑几分钟）…")
    rc, out = run_capture([py, "decrypt_db.py", "--count"], cwd=wex, timeout=2400)
    C.emit((out or "").rstrip())
    if rc != 0:
        C.err("解密验证失败（rc=%s）——密钥可能不对，回 S5 换路线重抓。" % rc)
        return "fail"
    C.ok("解密验证通过")
    if DRY:
        C.cmd([py, "daily_export.py"], wex)
        return "dry"
    if C.confirm("现在跑首次全量导出 daily_export.py？（首次可能要十几分钟）", True):
        rc = run_stream([py, "daily_export.py"], cwd=wex, timeout=5400)
        if rc != 0:
            C.warn("daily_export 退出码 %s（可稍后手动重跑，不影响看板先跑起来）" % rc)
        else:
            C.ok("首次导出完成")
    return "ok"


def s7_image_key(py, wechat):
    C.step("S7", "图片密钥（可选）")
    wex = os.path.join(ROOT, "wechat-export")
    ik = os.path.join(wex, "image_key.json")
    if os.path.isfile(ik):
        C.ok("已存在 image_key.json")
        if not C.confirm("重新抓取？", False):
            return "exists"
    C.info("用途：图片归档（media_archive）需要；不抓不影响消息导出与看板。")
    if not C.confirm("现在抓图片密钥？", False):
        return "skipped"
    ver = (wechat or {}).get("version") or ""
    if ver == "4.1.13.61":
        C.info("微信保持运行，先随便点开 2-3 张聊天图片，脚本会自动捕获（只读）…")
        rc = run_stream([py, "hook_image_key3.py"], cwd=wex, timeout=900)
    else:
        C.info("版本不是 4.1.13.61，改用通用内存扫描 find_image_key.py（需管理员）。")
        C.info("微信保持运行，先随便点开 2-3 张聊天图片。")
        script = os.path.join(wex, "find_image_key.py")
        if is_admin() or DRY:
            rc = run_stream([py, script], cwd=wex, timeout=1800)
        else:
            rc = run_elevated(["& " + psq(py) + " " + psq(script)],
                              cwd=wex, timeout=1800, tag="image_key")
            if rc is None:
                C.warn("UAC 被拒，跳过图片密钥（可稍后手动跑）。")
                return "skipped"
    if DRY:
        return "dry"
    if rc == 0 and os.path.isfile(ik):
        C.ok("image_key.json 已生成")
        return "ok"
    C.warn("没抓到图片密钥（可稍后点开图片再重试）。")
    return "fail"


def s8_kanban(py):
    C.step("S8", "启动看板")
    if not os.path.isfile(os.path.join(ROOT, "kanban", "data.json")):
        C.warn("kanban\\data.json 不存在（S4 应已生成），看板可能空白。")
    if NO_OPEN:
        C.info("--no-open：跳过自动打开。之后双击桌面「微信看板」快捷方式（或 scripts\\打开看板.bat）。")
        return "skipped"
    if DRY:
        C.cmd([py, os.path.join(ROOT, "scripts", "open_kanban.py")])
        return "dry"
    C.info("启动本地服务并打开浏览器（http://127.0.0.1:8710）…")
    rc, out = run_capture([py, os.path.join(ROOT, "scripts", "open_kanban.py")],
                          timeout=120)
    if out.strip():
        C.emit("        | " + out.strip().replace("\n", "\n        | "))
    ok = False
    for _ in range(24):
        try:
            s = socket.create_connection(("127.0.0.1", 8710), 0.5)
            s.close()
            ok = True
            break
        except OSError:
            time.sleep(0.5)
    if ok:
        C.ok("看板就绪：http://127.0.0.1:8710（浏览器应已打开）")
        return "ok"
    C.warn("8710 端口没探到服务，可能被占用或 recorder 起慢了。")
    C.info("手动排查：python scripts\\recorder.py；或重开 scripts\\打开看板.bat")
    return "warn"


def s9_shortcut(py):
    C.step("S9", "桌面快捷方式")
    target = os.path.join(ROOT, "scripts", "open_kanban.py")
    if not os.path.isfile(target):
        C.err("缺 scripts\\open_kanban.py，跳过。")
        return "fail"
    runner = os.path.join(os.path.dirname(py), "pythonw.exe")
    if not os.path.isfile(runner):
        runner = py
    desk = DESKTOP_DIR or known_folder(FOLDERID_Desktop) \
        or os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desk):
        if DRY:
            C.info("[dry] 将创建目录 " + desk)
        else:
            try:
                os.makedirs(desk, exist_ok=True)
                C.ok("已创建桌面目录 " + desk)
            except OSError as e:
                C.err("桌面目录创建失败: " + desk + "  " + str(e))
                return "fail"
    lnk = os.path.join(desk, "微信看板.lnk")
    ps = ("$ProgressPreference = 'SilentlyContinue'\n"
          "$ws = New-Object -ComObject WScript.Shell\n"
          "$s = $ws.CreateShortcut(" + psq(lnk) + ")\n"
          "$s.TargetPath = " + psq(runner) + "\n"
          "$s.Arguments = " + psq('"' + target + '"') + "\n"
          "$s.WorkingDirectory = " + psq(os.path.dirname(target)) + "\n"
          "$s.Description = " + psq("打开微信信息看板") + "\n"
          + ("$s.IconLocation = " + psq(ICON) + "\n" if ICON else "")
          + "$s.Save()\n"
          "$chk = $ws.CreateShortcut(" + psq(lnk) + ")\n"
          "if ($chk.TargetPath -eq " + psq(runner) + ") { Write-Output 'VERIFY_OK' } "
          "else { Write-Output 'VERIFY_FAIL' }\n")
    if DRY:
        C.cmd([PS, "CreateShortcut", lnk, "->", runner, target])
        return "dry"
    rc, out = run_ps(ps, timeout=90)
    if rc != 0 or "VERIFY_OK" not in out:
        C.err("快捷方式创建/校验失败：" + (out or "").strip()[:400])
        return "fail"
    C.ok("已创建: " + lnk)
    C.info("指向: " + runner)
    return "ok"


def build_tasks_ps(py):
    lines = ["$ErrorActionPreference = 'Stop'",
             "$ProgressPreference = 'SilentlyContinue'",
             "$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
             "$root = " + psq(ROOT),
             "$py = " + psq(py),
             "$st = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
             "-DontStopIfGoingOnBatteries -StartWhenAvailable",
             "$p = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive "
             "-RunLevel Limited"]
    for s in ("1200", "1800", "2200"):
        h = s[:2] + ":" + s[2:]
        name = "WeChatExport_" + s
        lines.append("$a = New-ScheduledTaskAction -Execute $py -Argument '-u daily_export.py' "
                     "-WorkingDirectory ($root + '\\wechat-export')")
        lines.append("$t = New-ScheduledTaskTrigger -Daily -At " + psq(h))
        lines.append("Register-ScheduledTask -TaskName " + psq(name)
                     + " -Action $a -Trigger $t -Principal $p -Settings $st -Force | Out-Null")
        lines.append("Write-Output " + psq("OK " + name))
    for s, ph in (("1210", "noon"), ("1810", "evening"), ("2100", "trip"), ("2210", "daily")):
        h = s[:2] + ":" + s[2:]
        name = "WeChatAnalysis_" + s
        lines.append("$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "
                     "('/c \"' + $root + '\\scripts\\run_analysis.bat\" " + ph + "')")
        lines.append("$t = New-ScheduledTaskTrigger -Daily -At " + psq(h))
        lines.append("Register-ScheduledTask -TaskName " + psq(name)
                     + " -Action $a -Trigger $t -Principal $p -Settings $st -Force | Out-Null")
        lines.append("Write-Output " + psq("OK " + name))
    return "\n".join(lines) + "\n"


def s10_tasks(py):
    C.step("S10", "Windows 计划任务")
    if NO_TASKS:
        C.warn("--no-tasks 已跳过")
        return "skipped"
    if not C.confirm("注册 7 个计划任务（3 个导出 + 4 个分析，自动跑批）？", True):
        return "skipped"
    ps = build_tasks_ps(py)
    if DRY:
        C.emit("        [dry] 将执行的 PowerShell：")
        for ln in ps.splitlines():
            C.emit("        | " + ln)
        return "dry"
    rc, out = run_ps(ps, timeout=300)
    C.emit((out or "").rstrip())
    okn = (out or "").count("OK WeChat")
    if rc != 0 or okn < 7:
        C.warn("注册不完整（成功 %d/7）。若提示拒绝访问，用管理员身份重跑本步。" % okn)
        return "warn"
    C.ok("7 个计划任务已注册（12:00/18:00/22:00 导出；12:10/18:10/21:00/22:10 分析）")
    return "ok"


def s11_summary(py, wechat, acct, results):
    C.step("S11", "完成")
    C.emit("  解释器   : " + (py or "-"))
    if wechat and wechat.get("root"):
        C.emit("  微信     : %s  版本 %s" % (wechat["root"], wechat.get("version") or "未知"))
    if acct:
        C.emit("  账号目录 : " + acct[1])
    C.emit("  密钥     : " + results.get("keys", "-"))
    C.emit("  验证导出 : " + results.get("verify", "-"))
    C.emit("  图片密钥 : " + results.get("image_key", "-"))
    C.emit("  看板     : " + results.get("kanban", "-"))
    C.emit("  快捷方式 : " + results.get("shortcut", "-"))
    C.emit("  计划任务 : " + results.get("tasks", "-"))
    agents = detect_agents()
    if agents:
        C.emit("  智能体   : %s (%s)" % (agents[0][1], agents[0][2]))
        if len(agents) > 1:
            C.emit("             （还检测到：%s；默认用第一个，想固定用哪个在 config.json「智能体」节指定）"
                   % "、".join(a[1] for a in agents[1:]))
    else:
        C.emit("  智能体   : 未检测到（见下方待办）")
    C.emit("")
    C.emit("  待办清单：")
    if not agents:
        C.emit("  · 没装命令行智能体 -> 分析班次会失败；装一个并登录（Claude Code / Gemini CLI / Codex 等），")
        C.emit("    或改 config.json「智能体」节指定，见 README 常见问题")
    if results.get("keys") in ("skipped", "fail", None, "exists") and results.get("verify") != "ok":
        C.emit("  · 密钥没配好 -> 见 README 第 4 节；配好后跑:")
        C.emit("    cd wechat-export && python decrypt_db.py --count && python daily_export.py")
    C.emit("  · 编辑 config.json：公众号名单 / 群聊名单 / 重点群聊（现在是示例占位）")
    C.emit("  · 编辑 分类规则.md：所有 ⚙️ 标注处改成你的场景")
    C.emit("  · config.json 的 vault目录 / whisper模型目录（用知识库/录音转写才需要）")
    C.emit("")
    C.emit("  Claude Cron（会话级，仅 Claude Code 有此机制；用其它智能体靠计划任务已足够）：")
    C.emit("  在该目录打开 Claude Code 的新会话会按 CLAUDE.md 自动创建；")
    C.emit("  也可把下面这段直接发给它手动建（等效）：")
    C.emit("  " + "-" * 60)
    txt = ("请建 4 个会话级 Cron：\n"
           "10 12 * * * -> 读 " + ROOT + "\\prompts\\prompt_noon.md 并严格按其流程执行\n"
           "10 18 * * * -> 读 " + ROOT + "\\prompts\\prompt_evening.md 并严格按其流程执行\n"
           "0 21 * * * -> 读 " + ROOT + "\\prompts\\prompt_trip.md 并严格按其流程执行\n"
           "10 22 * * * -> 读 " + ROOT + "\\prompts\\prompt_daily.md 并严格按其流程执行\n"
           "并且每次新会话开始时检查这 4 个 Cron 是否还在，缺失就重建。")
    for ln in txt.splitlines():
        C.emit("  " + ln)
    C.emit("  " + "-" * 60)
    C.emit("")
    C.emit("  日常入口：桌面「微信看板」快捷方式（或 scripts\\打开看板.bat）")


# ---------------------------------------------------------------- 主流程

def heal(state):
    old_root = state.get("root")
    if not old_root or old_root == ROOT:
        return
    C.info("检测到安装目录变化: %s -> %s，自愈已替换的绝对路径…" % (old_root, ROOT))
    for path in (glob.glob(os.path.join(ROOT, "prompts", "*.md"))
                 + glob.glob(os.path.join(ROOT, "CLAUDE.md"))):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                t = f.read()
        except Exception:
            continue
        if old_root in t:
            if DRY:
                C.ok("[dry] 自愈 " + os.path.basename(path))
            else:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(t.replace(old_root, ROOT))
                C.ok("自愈 " + os.path.basename(path))
    state["root"] = ROOT


def main():
    global DRY, YES, NO_OPEN, SKIP_DEPS, SKIP_REPLACE, SKIP_KEYS
    global ROUTE, DESKTOP_DIR, NO_TASKS
    ap = argparse.ArgumentParser(prog="setup_wizard.py",
                                 description="微信信息管理系统 · 一键部署向导")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不写不改")
    ap.add_argument("--yes", action="store_true", help="全程用默认值（少交互）")
    ap.add_argument("--skip-deps", action="store_true", help="跳过依赖安装")
    ap.add_argument("--skip-replace", action="store_true", help="跳过占位符替换")
    ap.add_argument("--skip-keys", action="store_true", help="跳过密钥提取与验证")
    ap.add_argument("--route", choices=["wcdb", "frida", "none"], help="指定密钥路线")
    ap.add_argument("--desktop-dir", help="桌面快捷方式落点（测试用）")
    ap.add_argument("--no-tasks", action="store_true", help="不注册计划任务")
    ap.add_argument("--no-open", action="store_true", help="不打开看板")
    args = ap.parse_args()
    DRY = args.dry_run
    YES = args.yes
    SKIP_DEPS = args.skip_deps
    SKIP_REPLACE = args.skip_replace
    SKIP_KEYS = args.skip_keys
    ROUTE = args.route
    DESKTOP_DIR = args.desktop_dir
    NO_TASKS = args.no_tasks
    NO_OPEN = args.no_open

    open_log()
    C.banner("微信信息管理系统 · 一键部署向导")
    C.info("安装目录: " + ROOT)
    if DRY:
        C.warn("干跑模式：只打印，不执行任何写操作/子进程")
    state = load_state()
    heal(state)
    if not s0_selfcheck():
        return 1
    py = s1_python(state)
    if not py:
        return 1
    wechat, acct = s2_wechat(state)
    save_state(state)
    results = {}
    results["deps"] = "ok" if s3_deps(py) else "fail"
    save_state(state)
    s4_scaffold(wechat, acct, state)
    save_state(state)
    results["keys"] = s5_keys(py, wechat, acct)
    save_state(state)
    results["verify"] = s6_verify(py)
    results["image_key"] = s7_image_key(py, wechat)
    results["kanban"] = s8_kanban(py)
    results["shortcut"] = s9_shortcut(py)
    results["tasks"] = s10_tasks(py)
    save_state(state)
    s11_summary(py, wechat, acct, results)
    if results.get("deps") == "fail":
        C.warn("注意：依赖安装失败，部分功能（密钥/录音）可能不可用，看板不受影响。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
