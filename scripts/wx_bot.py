# -*- coding: utf-8 -*-
"""wx_bot.py — 微信机器人通道（腾讯官方 iLink Bot API）

干什么：
    1) 推送：把通知/待审清单/日报发到你的微信——文本、图片、文件、视频都支持
    2) 接收：后台长轮询收你发给机器人的消息（图片/文件自动从腾讯 CDN 下载并解密落盘）
    3) 双向：收到的消息交给回调处理（scripts/wx_serve.py 会让命令行智能体作答）

为什么需要常驻长轮询：
    iLink 要求每条外发消息带上与该用户「最新的 context_token」，而这个 token 只有
    对方先给机器人发过消息才会有 → 所以本模块后台常驻一个 getupdates 长轮询，
    一边收消息一边刷新 token 缓存，发送时自动带上。发文本也能顺带刷新。

安全：
    只用腾讯官方 iLink HTTP API（ilinkai.weixin.qq.com），不 hook 微信进程、不碰本地
    消息库。账号凭据只写「账号目录」（account.json / context_tokens.json），不打印、不入库。

依赖：
    文本收发 —— 纯标准库，无需装任何东西
    图片/文件 —— cryptography（AES-128-ECB）。缺了照样能发文本，附件会明确报错
    扫码配对 —— qrcode（生成二维码图；缺了退化成打印链接）

用法：
    python scripts/wx_bot.py --login              # 扫码配对
    python scripts/wx_bot.py --status             # 看配对状态 / 令牌 / 最近收发
    python scripts/wx_bot.py --send "测试一下"     # 发一条文本
    python scripts/wx_bot.py --send-image a.png    # 发图片
    python scripts/wx_bot.py --send-file b.pdf     # 发文件
    python scripts/wx_bot.py --serve               # 前台长轮询（能看日志）

在代码里用：
    from wx_bot import WxBot
    bot = WxBot(conf, on_message=handle)     # handle(bot, msg)
    bot.start()                              # 起长轮询（可选）
    bot.send_text("标题", "正文")             # -> (ok, msg)
    bot.send_media("C:/a.png", "说明")        # -> (ok, msg)
"""
import os
import re
import sys
import json
import time
import base64
import socket
import hashlib
import secrets
import struct
import threading
import argparse
import datetime as dt

import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 技能根目录（config.json 所在处）

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.2.0"
APP_ID = "bot"
APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)   # 2.2.0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

API_TIMEOUT_MS = 20000
LONG_POLL_TIMEOUT_MS = 35000
QR_TIMEOUT_MS = 35000
QR_LOGIN_SECONDS = 480

SESSION_EXPIRED_ERRCODE = -14

# 媒体走腾讯 CDN，自己加解密（协议：AES-128-ECB + PKCS7）
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_DOWNLOAD = CDN_BASE + "/download"
CDN_UPLOAD = CDN_BASE + "/upload"
MAX_MEDIA_BYTES = 60 * 1024 * 1024        # 图片/文件单件上限
MAX_VIDEO_BYTES = 300 * 1024 * 1024       # 视频单件上限
MAX_OUT_BYTES = 90 * 1024 * 1024          # 往外发的单件上限

MEDIA_UPLOAD_TYPE = {"image": 1, "video": 2, "file": 3, "voice": 4}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}

DEFAULT_MAX_CHARS = 1500      # 单条文本上限，超了自动分段（iLink 对超长消息会拒）
DEFAULT_MIN_INTERVAL = 1.2    # 两条之间最小间隔，防限流

# 智能体在回复正文里用的「发附件」指令：单独一行写 [发图] C:\x\a.jpg
SEND_WORDS = ("发图片", "发照片", "发视频", "发文件", "发图",
              "send_image", "send_file", "send_video")
SEND_KIND = {"发图": "image", "发图片": "image", "发照片": "image", "send_image": "image",
             "发文件": "file", "send_file": "file",
             "发视频": "video", "send_video": "video"}
SEND_KIND_CN = {"image": "图片", "file": "文件", "video": "视频", "voice": "语音"}
SEND_RE = re.compile(
    r"^[ \t]*(?:[\[【(（][ \t]*(?P<word1>" + "|".join(map(re.escape, SEND_WORDS))
    + r")[ \t]*[\]】)）]|(?P<word2>" + "|".join(map(re.escape, SEND_WORDS))
    + r")[ \t]*[:：])[ \t]*(?P<path>.+?)[ \t]*$",
    re.M | re.I)

HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mask(s, keep=8):
    s = str(s or "")
    return s[:keep] + "…" if len(s) > keep else (s or "?")


def _has_crypto():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------- 媒体：AES-128-ECB + PKCS7 ----------------

def decode_media_key(aes_key, raw_hex=None):
    """把 iLink 的媒体 key 还原成 16 字节 AES key。

    协议里 key 有两种编码（都见过）：
      A base64(16 字节原始 key)        → 'ABEiM0RVZneImaq7zN3u/w=='
      B base64(32 个 hex 字符的 ASCII) → 'MDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVlZmY='
    另外 image_item.aeskey 是 32 位 hex 明文，优先级最高。
    """
    for cand in (raw_hex, aes_key):
        if not cand:
            continue
        s = str(cand).strip()
        if HEX32.match(s):
            return bytes.fromhex(s)
        try:
            b = base64.b64decode(s, validate=True)
        except Exception:
            continue
        if len(b) == 16:
            return b
        if len(b) == 32:
            t = b.decode("ascii", "ignore")
            if HEX32.match(t):
                return bytes.fromhex(t)
    return None


def decrypt_media(blob, key):
    """AES-128-ECB + PKCS7 解密"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    data = d.update(blob) + d.finalize()
    if data:
        pad = data[-1]
        if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
            data = data[:-pad]
    return data


def encrypt_media(data, key):
    """AES-128-ECB + PKCS7 加密（上传给 CDN 用，收方拿同一把 key 解）"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def pkcs7_size(n):
    """补块后的密文长度（腾讯客户端里就是 Math.ceil((n+1)/16)*16）"""
    return ((n + 16) // 16) * 16


def sniff_ext(data):
    """按文件头猜扩展名（解密后判类型用）"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"wxgf"):
        return ".wxgf"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return ".mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:2] == b"PK":
        return ".zip"
    return ".bin"


def cdn_download_url(encrypt_query_param):
    return CDN_DOWNLOAD + "?encrypted_query_param=" + urllib.parse.quote(
        str(encrypt_query_param), safe="")


def guess_kind(path, word=None):
    """按指令词优先、其次按扩展名，判断是图片/视频/文件"""
    w = str(word or "").strip().lower()
    if w in SEND_KIND:
        return SEND_KIND[w]
    ext = os.path.splitext(str(path))[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def split_text(text, max_chars=DEFAULT_MAX_CHARS):
    """按行切成若干段，避免单条超长被 iLink 拒"""
    text = text or ""
    if len(text) <= max_chars:
        return [text]
    out, buf = [], ""
    for line in text.splitlines():
        while len(line) > max_chars:              # 单行超长，硬切
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        if len(buf) + len(line) + 1 > max_chars:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return [x for x in out if x.strip()]


def parse_send_directives(text, base_dir=None):
    """把回复里的「[发图] 路径」这类行摘出来（正文照发，附件单独发）。

    返回 (剩下的正文, [{"kind","path","caption","raw"}])。
    路径：绝对路径最好；相对路径按 base_dir 解析。
    想带说明就写「[发图] C:\\a.jpg | 说明」。
    """
    text = text or ""
    sends, keep = [], []
    for line in text.splitlines():
        m = SEND_RE.match(line)
        if not m:
            keep.append(line)
            continue
        word = m.group("word1") or m.group("word2") or ""
        raw = m.group("path").strip().strip("`'\"“”‘’")
        caption = ""
        if "|" in raw:
            raw, caption = raw.split("|", 1)
            raw, caption = raw.strip().strip("`'\"“”‘’"), caption.strip()
        p = raw
        if base_dir and not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        if not os.path.exists(p):                 # 常见手滑：末尾带中文标点
            for junk in ("。", "，", ".", "、", "）", ")"):
                if p.endswith(junk) and os.path.exists(p[:-1]):
                    p = p[:-1]
                    break
        sends.append({"kind": guess_kind(p, word), "path": p,
                      "caption": caption, "raw": raw})
        keep.append("")                           # 占位，保持段落
    out = "\n".join(keep)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out.strip(), sends


# ---------------- iLink HTTP（纯标准库，同步） ----------------

def _body(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _headers(token, body):
    uin = base64.b64encode(str(struct.unpack(">I", secrets.token_bytes(4))[0]).encode()).decode()
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body)),
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": APP_CLIENT_VERSION,
    }
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _api_post(base_url, endpoint, payload, token, timeout_ms=API_TIMEOUT_MS):
    """POST 一个 iLink 接口，返回解析后的 JSON"""
    full = dict(payload)
    full["base_info"] = {"channel_version": CHANNEL_VERSION}
    body = _body(full)
    url = base_url.rstrip("/") + "/" + endpoint
    req = urllib.request.Request(url, data=body, headers=_headers(token, body))
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError("iLink HTTP %s: %s" % (e.code, detail))


def _api_get_sync(url, timeout_ms=QR_TIMEOUT_MS):
    req = urllib.request.Request(url, headers={
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": APP_CLIENT_VERSION,
    })
    with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_bytes(url, max_bytes=MAX_MEDIA_BYTES, timeout=90):
    if str(url or "")[:4] != "http":
        raise RuntimeError("不是 http 地址")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cl = int(r.headers.get("Content-Length") or 0)
        if cl and cl > max_bytes:
            raise RuntimeError("文件太大（%d MB，超过上限 %d MB）"
                               % (cl // 1048576, max_bytes // 1048576))
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("文件太大（超过上限 %d MB）" % (max_bytes // 1048576))
    return data


def extract_text(item_list):
    """从 item_list 里取可读文本（引用消息带上前文）"""
    for item in item_list or []:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if ref_item.get("type") in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE):
                title = ref.get("title") or ""
                return (("[引用媒体: %s]\n%s" % (title, text)) if title
                        else ("[引用媒体]\n" + text)).strip()
            if ref_item:
                parts = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                rt = extract_text([ref_item])
                if rt:
                    parts.append(rt)
                if parts:
                    return ("[引用: %s]\n%s" % (" | ".join(parts), text)).strip()
            return text
    for item in item_list or []:
        if item.get("type") == ITEM_VOICE:
            vt = str((item.get("voice_item") or {}).get("text") or "")
            if vt:
                return vt
    return ""


# ---------------- 扫码配对 ----------------

def qr_login_sync(state_dir, bot_type="3", timeout_seconds=QR_LOGIN_SECONDS, log=print):
    """扫码把机器人加成你的微信好友，凭据存 state_dir/account.json"""
    os.makedirs(state_dir, exist_ok=True)
    try:
        qr = _api_get_sync("%s/%s?bot_type=%s" % (ILINK_BASE_URL, EP_GET_BOT_QR, bot_type))
    except Exception as e:
        log("[!] 取二维码失败：%s" % e)
        return None
    value = str(qr.get("qrcode") or "")
    scan_data = str(qr.get("qrcode_img_content") or "") or value
    if not value:
        log("[!] 二维码响应异常：%s" % qr)
        return None

    png = os.path.join(state_dir, "qr.png")
    opened = False
    try:
        import qrcode
        qrcode.make(scan_data).save(png)
        log("[*] 二维码已存到 %s" % png)
        try:
            os.startfile(png)                     # Windows 直接弹出来
            opened = True
        except Exception:
            pass
    except Exception as e:
        log("[!] 生成二维码图片失败（可 pip install qrcode pillow）：%s" % e)
    if not opened:
        log("[*] 二维码链接（手机微信扫码用）：")
        log("    " + scan_data)
    log("[*] 手机微信扫码 → 确认添加机器人；最多等 %d 秒" % timeout_seconds)

    deadline = time.time() + timeout_seconds
    base_url = ILINK_BASE_URL
    refresh = 0
    while time.time() < deadline:
        try:
            st = _api_get_sync("%s/%s?qrcode=%s"
                               % (base_url, EP_GET_QR_STATUS, urllib.parse.quote(value, safe="")))
        except Exception:
            time.sleep(1)
            continue
        status = str(st.get("status") or "wait")
        if status == "wait":
            sys.stdout.write(".")
            sys.stdout.flush()
        elif status == "scaned":
            log("\n[*] 已扫码，请在微信里点确认…")
        elif status == "scaned_but_redirect":
            host = str(st.get("redirect_host") or "")
            if host:
                base_url = "https://" + host
        elif status == "expired":
            refresh += 1
            if refresh > 3:
                log("\n[!] 二维码反复过期，重跑一次 --login")
                return None
            log("\n[*] 二维码过期，刷新中 (%d/3)" % refresh)
            try:
                qr = _api_get_sync("%s/%s?bot_type=%s" % (ILINK_BASE_URL, EP_GET_BOT_QR, bot_type))
                value = str(qr.get("qrcode") or "")
                scan_data = str(qr.get("qrcode_img_content") or "") or value
                try:
                    import qrcode
                    qrcode.make(scan_data).save(png)
                    os.startfile(png)
                except Exception:
                    log("    " + scan_data)
            except Exception as e:
                log("[!] 刷新二维码失败：%s" % e)
                return None
        elif status == "confirmed":
            account = {
                "account_id": str(st.get("ilink_bot_id") or ""),
                "token": str(st.get("bot_token") or ""),
                "base_url": str(st.get("baseurl") or ILINK_BASE_URL),
                "user_id": str(st.get("ilink_user_id") or ""),
                "saved_at": _now(),
            }
            if not account["account_id"] or not account["token"]:
                log("[!] 确认了但凭据不完整，重试")
                return None
            path = os.path.join(state_dir, "account.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(account, f, ensure_ascii=False, indent=2)
            log("\n[*] 配对成功：account_id=%s  凭据已存 %s" % (_mask(account["account_id"]), path))
            return account
        time.sleep(1)
    log("\n[!] 登录超时")
    return None


# ---------------- 通道主体 ----------------

class WxBot:
    """微信机器人通道。

    conf 里读顶层「微信机器人」节：
        {
          "启用": true,
          "账号目录": "",          # 留空 = <技能目录>\\bot
          "收件人": "",            # 留空 = 配对时扫码的那个微信号
          "单条上限": 1500,
          "最小间隔秒": 1.2,
          "轮询": true,            # 常驻长轮询维护 context_token
          "来件目录": ""           # 收到的图片/文件存哪，留空 = 账号目录\\media
        }
    """

    MEDIA_KIND = {ITEM_IMAGE: "图片", ITEM_FILE: "文件", ITEM_VIDEO: "视频", ITEM_VOICE: "语音"}
    MEDIA_FIELD = {ITEM_IMAGE: "image_item", ITEM_FILE: "file_item",
                   ITEM_VIDEO: "video_item", ITEM_VOICE: "voice_item"}

    def __init__(self, conf=None, log=None, poll=True, on_message=None):
        conf = conf or {}
        cfg = conf.get("微信机器人") or {}
        self.cfg = cfg
        self.enabled = bool(cfg.get("启用", True))
        state_dir = str(cfg.get("账号目录") or "").strip() or os.path.join(ROOT, "bot")
        self.state_dir = state_dir
        self.account_file = os.path.join(state_dir, "account.json")
        self.token_file = os.path.join(state_dir, "context_tokens.json")
        self.sync_file = os.path.join(state_dir, "sync_buf.txt")
        self.media_dir_override = str(cfg.get("来件目录") or "").strip()
        self.max_chars = int(cfg.get("单条上限") or DEFAULT_MAX_CHARS)
        self.min_interval = float(cfg.get("最小间隔秒") or DEFAULT_MIN_INTERVAL)
        self.use_poll = bool(poll and cfg.get("轮询", True))
        self._log = log or (lambda m: None)
        self.on_message = on_message

        self.account = self._load_json(self.account_file, None)
        self.peer = (str(cfg.get("收件人") or "").strip()
                     or str((self.account or {}).get("user_id") or ""))
        self.tokens = self._load_json(self.token_file, {}) or {}

        self._thread = None
        self._stop = False
        self._last_in = ""
        self._last_out = ""
        self.last_error = ""
        self._send_at = 0.0
        self._send_lock = threading.Lock()       # 串行化发送，别撞限流

    # ---- 小工具 ----
    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write_json(self, path, obj):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- 状态 ----
    def ready(self):
        return bool(self.enabled and self.account and self.account.get("token") and self.peer)

    def describe(self):
        if not self.enabled:
            return "微信机器人（配置里关了）"
        if not self.account:
            return "微信机器人未配对（跑 python scripts/wx_bot.py --login）"
        tok = "有" if self.tokens.get(self.peer) else "无"
        return "微信机器人 iLink（对我 %s，上下文令牌%s）" % (_mask(self.peer), tok)

    def status_line(self):
        return ("[wxbot] enabled=%s peer=%s token=%s last_in=%s last_out=%s err=%s"
                % (self.enabled, _mask(self.peer),
                   "yes" if self.tokens.get(self.peer) else "no",
                   self._last_in or "-", self._last_out or "-", self.last_error or "-"))

    # ---- 后台长轮询 ----
    def start(self):
        """起后台线程维护 context_token，并接收你发来的消息"""
        if not self.ready() or (self._thread and self._thread.is_alive()):
            return
        self._stop = False
        self._thread = threading.Thread(target=self._thread_main, name="wxbot-poll", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def _thread_main(self):
        base_url = self.account.get("base_url") or ILINK_BASE_URL
        token = self.account["token"]
        sync_buf = ""
        if os.path.exists(self.sync_file):
            try:
                sync_buf = open(self.sync_file, encoding="utf-8").read().strip()
            except Exception:
                sync_buf = ""
        self._log("[wxbot] 长轮询启动（%s）" % _mask(self.peer))
        while not self._stop:
            try:
                resp = _api_post(base_url, EP_GET_UPDATES,
                                 {"get_updates_buf": sync_buf}, token, LONG_POLL_TIMEOUT_MS)
            except socket.timeout:
                continue                                  # 长轮询空转，正常
            except Exception as e:
                self.last_error = "%s: %s" % (type(e).__name__, e)
                self._log("[wxbot] 轮询错误：%s" % self.last_error)
                time.sleep(5)
                continue
            nb = str(resp.get("get_updates_buf") or sync_buf)
            if nb != sync_buf:
                sync_buf = nb
                try:
                    with open(self.sync_file, "w", encoding="utf-8") as f:
                        f.write(sync_buf)
                except Exception:
                    pass
            code = resp.get("errcode")
            if code == SESSION_EXPIRED_ERRCODE or resp.get("ret") == SESSION_EXPIRED_ERRCODE:
                self.last_error = "会话过期（重新 --login 配对）"
                self._log("[wxbot] " + self.last_error)
                time.sleep(30)
                continue
            if code:
                self.last_error = "iLink errcode=%s %s" % (code, str(resp.get("errmsg") or "")[:60])
                self._log("[wxbot] " + self.last_error)
                time.sleep(5)
                continue
            for msg in resp.get("msgs") or []:
                try:
                    self._handle_message(msg)
                except Exception as e:
                    self._log("[wxbot] 处理消息异常：%s: %s" % (type(e).__name__, e))
        self._log("[wxbot] 长轮询退出")

    def _handle_message(self, msg):
        """收到一条消息：刷新 token → 存媒体 → 交给回调"""
        peer = str(msg.get("from_user_id") or "").strip()
        ctoken = str(msg.get("context_token") or "").strip()
        if peer and ctoken and self.tokens.get(peer) != ctoken:
            self.tokens[peer] = ctoken
            self._write_json(self.token_file, self.tokens)
        items = msg.get("item_list") or []
        text = extract_text(items).strip()
        attach = []
        if self._has_media(items):
            try:
                attach = self._save_inbound_media(msg, items)
            except Exception as e:
                self._log("[wxbot] 媒体保存异常：%s: %s" % (type(e).__name__, e))
        if not text and not attach:
            return
        self._last_in = "%s %s" % (_now(), (text or "[附件]").replace("\n", " ")[:40])
        self._log("[wxbot] 收到 %s: %s" % (_mask(peer), (text or "[附件]")[:60]))
        if self.on_message:
            self.on_message(self, {
                "peer": peer,
                "text": text,
                "attachments": attach,
                "context_token": ctoken,
                "message_id": str(msg.get("message_id") or msg.get("seq") or ""),
                "raw": msg,
            })

    # ---- 入站媒体 ----
    @staticmethod
    def _has_media(items):
        return any(int((it or {}).get("type") or 0) in
                   (ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO) for it in (items or []))

    def media_dir(self):
        if self.media_dir_override:
            d = self.media_dir_override
        else:
            d = os.path.join(self.state_dir, "media")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_inbound_media(self, msg, items):
        """图片/文件/视频落盘，返回 [{"kind","path","name"}]"""
        out = []
        mid = str(msg.get("message_id") or msg.get("seq") or int(time.time()))
        idx = 0
        for it in items or []:
            t = int((it or {}).get("type") or 0)
            if t not in self.MEDIA_KIND:
                continue
            sub = it.get(self.MEDIA_FIELD[t]) or {}
            kind = self.MEDIA_KIND[t]
            if t == ITEM_VOICE:
                continue                     # 有转写文字的语音已当文本处理
            idx += 1
            fname = str(sub.get("file_name") or "")
            media = sub.get("media") or {}
            cap = MAX_VIDEO_BYTES if t == ITEM_VIDEO else MAX_MEDIA_BYTES
            raw, err = None, ""
            param = str(media.get("encrypt_query_param") or "")
            if param:
                try:
                    blob = http_get_bytes(cdn_download_url(param), max_bytes=cap)
                    key = decode_media_key(media.get("aes_key"), sub.get("aeskey"))
                    if key:
                        dec = b""
                        try:
                            dec = decrypt_media(blob, key)
                        except Exception:
                            dec = b""
                        if dec and sniff_ext(dec) != ".bin":
                            raw = dec
                        elif sniff_ext(blob) != ".bin":
                            raw = blob                 # 服务端本来就是明文
                        elif dec:
                            raw = dec                  # 文档类 sniff 不出来
                        else:
                            err = "AES 解密失败"
                    else:
                        raw = blob                     # 没有 key：按明文收
                except Exception as e:
                    err = "%s: %s" % (type(e).__name__, str(e)[:80])
            if raw is None and str(sub.get("url") or "")[:4] == "http":
                try:
                    raw = http_get_bytes(sub["url"], max_bytes=cap)
                except Exception as e:
                    err = err or ("%s: %s" % (type(e).__name__, str(e)[:80]))
            if raw is None:
                out.append({"kind": kind, "note": err or "消息里没有可下载的地址"})
                continue
            ext = ""
            if fname:
                e = os.path.splitext(fname)[1].lower()
                if re.fullmatch(r"\.[a-z0-9]{1,5}", e or ""):
                    ext = e
            snap = sniff_ext(raw)
            if t == ITEM_IMAGE or not ext:
                ext = snap if snap != ".bin" else (ext or ".bin")
            path = os.path.join(self.media_dir(),
                                "%s-%s-%d%s" % (time.strftime("%Y%m%d-%H%M%S"), mid, idx, ext))
            try:
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception as e:
                out.append({"kind": kind, "note": "写盘失败 %s" % type(e).__name__})
                continue
            self._log("[wxbot] 收到%s %d 字节 → %s" % (kind, len(raw), path))
            out.append({"kind": kind, "path": path, "name": fname})
        return out

    # ---- 出站：文本 ----
    def send_text(self, title, body):
        """发文本，title 作为第一行。(ok, msg)"""
        if not self.enabled:
            return False, "微信机器人通道已关闭（配置「微信机器人」→「启用」= false）"
        if not self.account:
            return False, "微信机器人没配对：python scripts/wx_bot.py --login"
        if not self.peer:
            return False, "没有收件人：account.json 缺 user_id，或填配置「微信机器人」→「收件人」"
        text = ("%s\n%s" % (title, body) if title else (body or ""))
        if not text.strip():
            return False, "内容为空"
        chunks = split_text(text, self.max_chars)
        with self._send_lock:
            return self._send_chunks(chunks)

    def _send_chunks(self, chunks):
        base_url = self.account.get("base_url") or ILINK_BASE_URL
        token = self.account["token"]
        try:
            for i, ch in enumerate(chunks):
                gap = max(0.0, self.min_interval - (time.time() - self._send_at))
                if gap:
                    time.sleep(gap)
                resp = self._send_item(base_url, token, self.peer,
                                       {"type": ITEM_TEXT, "text_item": {"text": ch}},
                                       self.tokens.get(self.peer))
                err = self._check(resp)
                if err:
                    self.last_error = err
                    return False, err
                self._send_at = time.time()
            self._last_out = "%s %s" % (_now(), (chunks[0] if chunks else "")[:30])
            self.last_error = ""
            return True, "已发 %d 条到微信机器人" % len(chunks)
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return False, self.last_error

    # ---- 出站：媒体 ----
    def send_media(self, path, caption=None):
        """发一个图片/文件/视频（按扩展名自动判类型）。(ok, msg)"""
        if not self.enabled:
            return False, "微信机器人通道已关闭（配置「微信机器人」→「启用」= false）"
        if not self.account:
            return False, "微信机器人没配对：python scripts/wx_bot.py --login"
        if not self.peer:
            return False, "没有收件人：account.json 缺 user_id，或填配置「微信机器人」→「收件人」"
        path = str(path)
        if not os.path.isfile(path):
            return False, "文件不存在：%s" % path
        size = os.path.getsize(path)
        if size > MAX_OUT_BYTES:
            return False, "文件太大（%.0f MB，上限 %d MB）" % (size / 1048576.0,
                                                              MAX_OUT_BYTES // 1048576)
        if not _has_crypto():
            return False, "缺 cryptography，发不了附件：pip install cryptography"
        with self._send_lock:
            if caption:
                ok, msg = self._send_chunks(split_text(caption, self.max_chars))
                if not ok:
                    self._log("[wxbot] 附件说明发送失败：%s" % msg)
            try:
                up = self._upload_media(path)
                resp = self._send_item(self.account.get("base_url") or ILINK_BASE_URL,
                                       self.account["token"], self.peer,
                                       self._media_item(up["kind"], path, up),
                                       self.tokens.get(self.peer))
            except Exception as e:
                self.last_error = "%s: %s" % (type(e).__name__, str(e)[:150])
                return False, self.last_error
            err = self._check(resp)
            if err:
                self.last_error = err
                return False, err
            self._last_out = "%s 发了%s %s" % (_now(), SEND_KIND_CN.get(up["kind"], "附件"),
                                              os.path.basename(path))
            self.last_error = ""
            return True, "已发%s（%d 字节）" % (SEND_KIND_CN.get(up["kind"], "附件"), up["raw_size"])

    def send_attachments(self, paths, caption=None):
        """连着发多个附件。(ok, msg)"""
        sent, failed = 0, []
        for p in paths:
            ok, msg = self.send_media(p, caption if sent == 0 else None)
            if ok:
                sent += 1
            else:
                failed.append("%s（%s）" % (os.path.basename(str(p)), msg))
        if failed:
            return False, "成功 %d 个，失败：%s" % (sent, "；".join(failed))
        return True, "已发 %d 个附件" % sent

    @staticmethod
    def _check(resp):
        """把 iLink 应答里的错误翻成人话；没问题返回空串"""
        if not isinstance(resp, dict):
            return "应答异常：%s" % str(resp)[:100]
        code = resp.get("errcode")
        if code == SESSION_EXPIRED_ERRCODE or resp.get("ret") == SESSION_EXPIRED_ERRCODE:
            return "会话过期（重新 --login 配对）"
        if code:
            return "iLink errcode=%s %s" % (code, str(resp.get("errmsg") or "")[:80])
        return ""

    def _send_item(self, base_url, token, to, item, context_token=None):
        message = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": hashlib.md5(("%s%s" % (time.time(), str(item)[:32])).encode()).hexdigest()[:16],
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [item],
        }
        if context_token:
            message["context_token"] = context_token
        return _api_post(base_url, EP_SEND_MESSAGE, {"msg": message}, token, API_TIMEOUT_MS)

    # ---- 出站媒体：① getuploadurl ② 加密传 CDN ③ 拿 x-encrypted-param ----
    def _upload_media(self, path):
        with open(path, "rb") as f:
            data = f.read()
        kind = guess_kind(path)
        raw_size = len(data)
        filekey = os.urandom(16).hex()             # 32 位 hex
        aeskey = os.urandom(16)                    # 真正的 16 字节 key
        payload = {
            "filekey": filekey,
            "media_type": MEDIA_UPLOAD_TYPE.get(kind, MEDIA_UPLOAD_TYPE["file"]),
            "to_user_id": self.peer,
            "rawsize": raw_size,
            "rawfilemd5": hashlib.md5(data).hexdigest(),
            "filesize": pkcs7_size(raw_size),
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),
        }
        resp = _api_post(self.account.get("base_url") or ILINK_BASE_URL,
                         EP_GET_UPLOAD_URL, payload, self.account["token"], API_TIMEOUT_MS)
        if resp.get("errcode") or resp.get("ret") not in (None, 0):
            raise RuntimeError("getuploadurl 失败：%s"
                               % json.dumps(resp, ensure_ascii=False)[:200])
        full = str(resp.get("upload_full_url") or "").strip()
        param = str(resp.get("upload_param") or "").strip()
        if full:
            url = full
        elif param:
            url = (CDN_UPLOAD + "?encrypted_query_param=" + urllib.parse.quote(param, safe="")
                   + "&filekey=" + urllib.parse.quote(filekey, safe=""))
        else:
            raise RuntimeError("getuploadurl 没给上传地址：%s"
                               % json.dumps(resp, ensure_ascii=False)[:200])

        blob = encrypt_media(data, aeskey)
        headers = {"Content-Type": "application/octet-stream",
                   "Content-Length": str(len(blob))}
        last = ""
        for attempt in (1, 2, 3):
            try:
                req = urllib.request.Request(url, data=blob, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as r:
                    download_param = r.headers.get("x-encrypted-param")
                if not download_param:
                    raise RuntimeError("CDN 应答里没有 x-encrypted-param")
                return {
                    "kind": kind, "filekey": filekey, "raw_size": raw_size,
                    "cipher_size": pkcs7_size(raw_size),
                    "download_param": download_param,
                    # 发送时的 aes_key：base64(32 位 hex 的 ASCII)，与腾讯客户端一致
                    "aes_key_b64": base64.b64encode(aeskey.hex().encode()).decode(),
                }
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = (e.headers.get("x-error-message") or e.read().decode("utf-8", "replace")[:150])
                except Exception:
                    pass
                last = "CDN 上传 HTTP %s：%s" % (e.code, detail)
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, str(e)[:150])
            if attempt < 3:
                time.sleep(1.0 * attempt)
        raise RuntimeError("CDN 上传三次都失败：" + last)

    @staticmethod
    def _media_item(kind, path, up):
        media = {
            "encrypt_query_param": up["download_param"],
            "aes_key": up["aes_key_b64"],
            "encrypt_type": 1,
        }
        if kind == "image":
            return {"type": ITEM_IMAGE,
                    "image_item": {"media": media, "mid_size": up["cipher_size"]}}
        if kind == "video":
            return {"type": ITEM_VIDEO,
                    "video_item": {"media": media, "video_size": up["cipher_size"]}}
        return {"type": ITEM_FILE,
                "file_item": {"media": media,
                              "file_name": os.path.basename(path),
                              "len": str(up["raw_size"])}}


# ---------------- 配置 + 命令行 ----------------

def load_conf(path=None):
    """找 config.json：--conf → 环境变量 → 技能根目录"""
    cands = [path, os.environ.get("WX_BOT_CONF"), os.path.join(ROOT, "config.json")]
    for c in cands:
        if c and os.path.exists(c):
            with open(c, encoding="utf-8-sig") as f:
                conf = json.load(f)
            conf["_path"] = os.path.abspath(c)
            return conf
    return {"_path": "(没找到 config.json，用默认配置)"}


def build_bot(conf, poll=True, log=print, on_message=None):
    return WxBot(conf, log=log, poll=poll, on_message=on_message)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="微信机器人通道（腾讯 iLink Bot）")
    ap.add_argument("--conf", default=None, help="config.json 路径")
    ap.add_argument("--login", action="store_true", help="扫码配对")
    ap.add_argument("--status", action="store_true", help="看配对状态")
    ap.add_argument("--send", default=None, help="发一条文本")
    ap.add_argument("--send-image", default=None, help="发一张图片")
    ap.add_argument("--send-file", default=None, help="发一个文件")
    ap.add_argument("--send-video", default=None, help="发一个视频")
    ap.add_argument("--caption", default=None, help="附件前面附的一句话")
    ap.add_argument("--serve", action="store_true", help="前台跑长轮询")
    ap.add_argument("--bot-type", default="3")
    a = ap.parse_args()

    conf = load_conf(a.conf)
    wb = conf.get("微信机器人") or {}
    state_dir = str(wb.get("账号目录") or "").strip() or os.path.join(ROOT, "bot")

    if a.login:
        acc = qr_login_sync(state_dir, bot_type=a.bot_type)
        sys.exit(0 if acc else 1)

    bot = build_bot(conf, poll=(a.serve or not any([a.send, a.send_image, a.send_file, a.send_video])))

    if a.status:
        print("=== 微信机器人通道 ===")
        print("  配置：", conf.get("_path"))
        print("  账号目录：", state_dir)
        print("  账号：", _mask((bot.account or {}).get("account_id")) if bot.account else "（未配对）")
        print("  收件人：", _mask(bot.peer))
        print("  通道：", bot.describe())
        print("  最近收：", bot._last_in or "-")
        print("  最近发：", bot._last_out or "-")
        print("  令牌文件：", bot.token_file, "(有)" if os.path.exists(bot.token_file) else "(无)")
        print("  媒体依赖：", "cryptography 已装" if _has_crypto() else "缺 cryptography（发不了附件）")
        print("  提示：对方给机器人发过消息后令牌才会出现；发「状态」机器人会回执")
        return

    media = a.send_image or a.send_file or a.send_video
    if a.send or media:
        ok, msg = (bot.send_media(media, a.caption) if media else bot.send_text("", a.send))
        print(("[OK] " if ok else "[FAIL] ") + msg)
        sys.exit(0 if ok else 1)

    if a.serve:
        if not bot.ready():
            sys.exit("尚未配对：" + bot.describe())
        print("[*] 长轮询启动（Ctrl+C 退出）；给机器人发「状态」可验通道")
        bot.start()
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            bot.stop()
        return

    ap.print_help()


if __name__ == "__main__":
    main()
