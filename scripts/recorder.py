# -*- coding: utf-8 -*-
"""微信信息管理系统 · 录音模块（后台常驻）
控制方式（双通道）：
  1. 全局热键 Ctrl+Alt+R 切换录音/停止
  2. 看板按钮 → 本地 HTTP 127.0.0.1:8710：
     GET  /status → {"recording": bool, "last_file": ..., "last_transcript": ...}
     POST /toggle → 切换录音（CORS 全开，file:// 页面可调）
停止后自动：ffmpeg 转 m4a 存 kanban/录音/ → whisper 本地转文字 →
写看板 data.json（voice 条目含 transcript）+ vault 每日复盘追加
全程气泡提示（toast.ps1）+ 短录音防误触丢弃
依赖: keyboard, sounddevice, numpy, faster-whisper, ffmpeg（系统安装）
配置: config.json 的 vault目录（Obsidian 库）、whisper模型目录（faster-whisper 模型）
"""
import datetime
import http.server
import json
import os
import re
import subprocess
import threading
import wave

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import keyboard
import numpy as np
import sounddevice as sd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_JSON = os.path.join(BASE, "config.json")
KANBAN_DIR = os.path.join(BASE, "kanban")
VOICE_DIR = os.path.join(KANBAN_DIR, "录音")
DATA_JSON = os.path.join(KANBAN_DIR, "data.json")
TOAST_PS1 = os.path.join(BASE, "scripts", "toast.ps1")
HOTKEY = "ctrl+alt+r"
SR = 16000
PORT = 8710
MIN_SECONDS = 1.0  # 短于此的录音丢弃（防误触）

_state = {"recording": False, "frames": [], "stream": None,
          "last": {"file": None, "transcript": None}}
_lock = threading.Lock()
_model = None


def _cfg():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def vault_review_dir():
    """每日复盘目录：config.json vault目录\每日复盘；未配置则落到技能目录内"""
    v = (_cfg().get("vault目录") or "").strip()
    if v and os.path.isdir(v):
        return os.path.join(v, "每日复盘")
    return os.path.join(BASE, "每日复盘")


VAULT_REVIEW = vault_review_dir()


def toast(title, msg):
    """右下角气泡提示，不抢焦点，几秒自动消失"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", TOAST_PS1, title, msg],
            creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        model_dir = (_cfg().get("whisper模型目录") or "").strip()
        if not model_dir or not os.path.isdir(model_dir):
            raise RuntimeError("config.json 未配置 whisper模型目录（faster-whisper 模型路径）")
        # 本地模型目录（curl 从 hf-mirror 直下，不走 huggingface_hub 下载通道）
        _model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    return _model


def transcribe(path):
    """本地转录（中文），返回文本"""
    m = get_model()
    segments, info = m.transcribe(path, language="zh", beam_size=5)
    return "".join(s.text for s in segments).strip()


def _rollover(data):
    """跨天：旧 today 归档进 archive + history 头插（截 7），重建空 today（与班次日切逻辑一致）"""
    today = datetime.date.today().isoformat()
    old = data.get("today") or {}
    if old.get("date") == today:
        return data
    old_date = old.get("date")
    if old_date:
        data.setdefault("archive", {})[old_date] = old
        hist = [x for x in (data.get("history") or []) if x != old_date]
        hist.insert(0, old_date)
        data["history"] = hist[:7]
    data["today"] = {"date": today, "ts": "", "brief": {"群聊": [], "公众号": []},
                     "notify": [], "brief_chat": [], "brief_mp": [],
                     "activities_new": [], "schedule": [],
                     "review": {"user": "", "ai": ""}, "voice": []}
    return data


def update_data_json(fname_rel, t, transcript):
    """看板 data.json 的 voice 条目追加（含 transcript）"""
    with open(DATA_JSON, encoding="utf-8") as f:
        data = json.load(f)
    _rollover(data)
    data["today"].setdefault("voice", [])
    data["today"]["voice"].append(
        {"file": fname_rel, "time": t, "note": "", "transcript": transcript})
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_review(transcript, t):
    """转录文本入库 Obsidian 每日复盘"""
    today = datetime.date.today().isoformat()
    os.makedirs(VAULT_REVIEW, exist_ok=True)
    p = os.path.join(VAULT_REVIEW, f"{today}_复盘.md")
    block = f"\n## 语音口述 {t}\n\n{transcript}\n"
    if os.path.exists(p):
        with open(p, "a", encoding="utf-8") as f:
            f.write(block)
    else:
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# {today} 复盘\n\n---\n{block}")


def save_user_review(text):
    """看板手动编辑：写 data.json 的 today.review.user + 镜像 vault 每日复盘"""
    with open(DATA_JSON, encoding="utf-8") as f:
        data = json.load(f)
    _rollover(data)
    data["today"].setdefault("review", {})
    data["today"]["review"]["user"] = text
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _mirror_user_review(text)


def _mirror_user_review(text):
    """用户复盘镜像到 vault 每日复盘（替换已有「用户复盘」块，无则追加）"""
    today = datetime.date.today().isoformat()
    os.makedirs(VAULT_REVIEW, exist_ok=True)
    p = os.path.join(VAULT_REVIEW, f"{today}_复盘.md")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            content = f.read()
        content = re.sub(r"(?ms)^## 用户复盘.*?(?=^## |\Z)", "",
                         content).rstrip() + "\n"
    else:
        content = f"# {today} 复盘\n\n---\n"
    block = f"\n## 用户复盘\n\n{text}\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(content + block)


def _pick_input():
    """选麦克风输入设备：排除立体声混音/扬声器环回，优先 WASAPI + 名字含麦克风"""
    exclude = ("混音", "stereo", "loopback", "环回", "映射器", "mapper",
               "扬声器", "speaker", "headphone", "耳机")
    cands = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        n = d["name"].lower()
        if any(k in n for k in exclude):
            continue
        score = (1 if "wasapi" in n else 0,
                 1 if ("麦克风" in n or "mic" in n) else 0)
        cands.append((score, i, d["name"]))
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1] if cands else None


def _cb(indata, frames, t, status):
    _state["frames"].append(indata.copy())


def _finish(frames):
    """停止后：存 wav → ffmpeg 转 m4a → 转文字 → 写看板 + 入库"""
    if not frames:
        print("✗ 没录到声音，已忽略", flush=True)
        toast("🎙️ 录音", "没录到声音，已忽略")
        return
    wav = np.concatenate(frames)
    if len(wav) < SR * MIN_SECONDS:
        print("✗ 录音太短，已忽略（防误触）", flush=True)
        toast("🎙️ 录音", "录音太短，已忽略")
        return
    ts = datetime.datetime.now()
    fname = f"{ts:%Y-%m-%d_%H-%M}.m4a"
    wav_path = os.path.join(VOICE_DIR, fname.replace(".m4a", ".wav"))
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(wav.tobytes())
    out = os.path.join(VOICE_DIR, fname)
    r = subprocess.run(["ffmpeg", "-y", "-i", wav_path,
                        "-c:a", "aac", "-b:a", "96k", out],
                       capture_output=True)
    os.remove(wav_path)
    if r.returncode != 0:
        print(f"✗ 转 m4a 失败: {r.stderr.decode('utf-8', 'ignore')[:200]}",
              flush=True)
        toast("🎙️ 录音", "转换失败，看日志")
        return
    print(f"✅ 录音保存: kanban/录音/{fname}", flush=True)
    try:
        print("转录中…（本地 whisper，稍等）", flush=True)
        text = transcribe(out)
        print(f"转录: {text[:100]}", flush=True)
    except Exception as e:
        text = f"[转录失败: {e}]"
        print(text, flush=True)
    update_data_json(f"录音/{fname}", f"{ts:%H:%M}", text)
    append_review(text, f"{ts:%H:%M}")
    _state["last"] = {"file": fname, "transcript": text}
    print("✅ 已写看板 + 每日复盘", flush=True)
    toast("✅ 录音已保存", f"{fname}\n转录: {text[:50]}")


def toggle():
    """切换录音/停止（线程安全，热键与 HTTP 共用）"""
    with _lock:
        if not _state["recording"]:
            _state["recording"] = True
            _state["frames"] = []
            dev = _pick_input()
            _state["stream"] = sd.InputStream(samplerate=SR, channels=1,
                                              dtype="int16", callback=_cb,
                                              device=dev)
            _state["stream"].start()
            print(f"🎤 输入设备: {sd.query_devices(dev)['name']}", flush=True)
            print("🔴 录音开始", flush=True)
            toast("🎙️ 录音开始",
                  "正在录，说完按 Ctrl+Alt+R 或点看板按钮停止")
        else:
            stream = _state["stream"]
            frames = _state["frames"]
            _state["stream"] = None
            _state["frames"] = []
            _state["recording"] = False
            stream.stop()
            stream.close()
            print("⏹ 录音停止，处理中…", flush=True)
            threading.Thread(target=_finish, args=(frames,), daemon=True).start()


class _Handler(http.server.SimpleHTTPRequestHandler):
    """看板静态服务（挂 kanban 目录）+ 录音控制接口
    看板页走 http://127.0.0.1:8710/ 打开 → fetch 同目录 data.json 不被浏览器拦，
    真实数据双击入口脚本即看。SimpleHTTPRequestHandler 自带 Range 支持（音频拖进度条）。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=KANBAN_DIR, **kw)

    def end_headers(self):
        # CORS 全开：file:// 页面（origin null）也能调录音接口
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # 强制不缓存：看板数据和页面改了要立即生效，浏览器缓存旧页面会坑用户
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            body = json.dumps(
                {"recording": _state["recording"],
                 "last_file": _state["last"]["file"],
                 "last_transcript": _state["last"]["transcript"]},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] == "/toggle":
            threading.Thread(target=toggle, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path.split("?")[0] == "/review":
            # 看板「睡前复盘」手动编辑：{review_user: "文本"} → data.json + vault
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                text = str(req.get("review_user") or "")
            except Exception:
                text = ""
            save_user_review(text)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def _start_http():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"🌐 看板: http://127.0.0.1:{PORT}/ （GET /status, POST /toggle）",
          flush=True)
    srv.serve_forever()


def main():
    os.makedirs(VOICE_DIR, exist_ok=True)
    threading.Thread(target=_start_http, daemon=True).start()
    keyboard.add_hotkey(HOTKEY, toggle)
    print(f"🎙️ 录音模块已启动。热键 {HOTKEY} 或看板按钮控制", flush=True)
    keyboard.wait()


if __name__ == "__main__":
    main()
