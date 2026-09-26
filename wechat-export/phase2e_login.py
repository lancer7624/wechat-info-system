# -*- coding: utf-8 -*-
"""Phase 2e：登录感知捕捞 v4

v3 教训：90s 观察窗在用户登录前就结束了（登录发生在捕捞结束后 14 分钟）。
v4 改进：
1. 登录检测：轮询 message_0.db / contact.db 修改时间（登录打开库的瞬间触发）
2. 最长等 15 分钟，登录信号后继续观察 120s
3. 候选按登录前/后标记
4. 32 字节窗口频率统计（主密钥在多次 DB 打开中反复出现 → 高频窗口优先验证）
5. 每次读 512 字节（覆盖更深 struct）
"""
import frida
import sys
import time
import json
import os
import subprocess
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phase2d_wide import load_anchors  # noqa: E402

WECHAT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
LOGIN_TIMEOUT = 900    # 等登录最多 15 分钟
POST_LOGIN_WATCH = 120  # 登录信号后继续观察 120 秒
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "candidates.json")

SIGNAL_DBS = [
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\message\message_0.db",
    r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage\contact\contact.db",
]

CAND_JS = r"""
var sendBudget = 50, sendCount = 0, sendWindowStart = Date.now();
var T = new Uint8Array(256);

function windowPass(w) {
  T.fill(0);
  var d = 0, zeros = 0, printable = 0;
  for (var i = 0; i < 32; i++) {
    var b = w[i];
    if (T[b] === 0) { T[b] = 1; d++; }
    if (b === 0) zeros++;
    if (b >= 32 && b < 127) printable++;
  }
  if (zeros > 9) return false;
  if (printable > 21) return false;
  return d >= 16;
}

function hexRun(buf) {
  var best = 0, cur = 0, bestOff = -1;
  for (var i = 0; i < buf.length; i++) {
    var b = buf[i];
    var isHex = (b >= 48 && b <= 57) || (b >= 65 && b <= 70) || (b >= 97 && b <= 102);
    if (isHex) { cur++; if (cur > best) { best = cur; bestOff = i - cur + 1; } }
    else cur = 0;
  }
  if (best >= 64) {
    var out = '';
    for (var i = 0; i < best; i++) out += String.fromCharCode(buf[bestOff + i]);
    return out;
  }
  return null;
}

function onEnter(name, args) {
  for (var i = 0; i < 4; i++) {
    var p = args[i];
    if (p.isNull()) continue;
    var v = p.toInt32();
    if (v === 0 || (v & 3) !== 0) continue;
    var buf;
    try { buf = p.readByteArray(512); } catch (e) { continue; }
    var u8 = new Uint8Array(buf);
    var hx = hexRun(u8);
    if (hx !== null) {
      var now = Date.now();
      if (now - sendWindowStart > 1000) { sendWindowStart = now; sendCount = 0; }
      if (sendCount >= sendBudget) return;
      sendCount++;
      send({type: 'hex', anchor: name, reg: i, text: hx});
      return;
    }
    for (var off = 0; off + 32 <= u8.length; off += 4) {
      if (windowPass(u8.subarray(off, off + 32))) {
        var now2 = Date.now();
        if (now2 - sendWindowStart > 1000) { sendWindowStart = now2; sendCount = 0; }
        if (sendCount >= sendBudget) return;
        sendCount++;
        send({type: 'cand', anchor: name, reg: i, off: off,
              data: Array.prototype.slice.call(u8.subarray(off, off + 32))});
        break;
      }
    }
  }
}
"""


def build_hook_js(anchor_list, wait_for_module):
    attach_lines = []
    for i, a in enumerate(anchor_list):
        attach_lines.append(
            "  Interceptor.attach(base.add(0x%X), {onEnter: function(args){"
            "onEnter('a%04d', args);}});" % (a["rva"], i))
    attach_block = "\n".join(attach_lines)
    interval = 50 if wait_for_module else 100
    return CAND_JS + r"""
var installed = false;
var base = null;
function install() {
  if (installed) return;
  var mod = Process.findModuleByName('Weixin.dll');
  if (!mod) return;
  base = mod.base;
%s
  installed = true;
  send({type:'log', msg:'[main] %d hooks installed @ ' + base});
}
install();
setInterval(install, %d);
""" % (attach_block, len(anchor_list), interval)


def toast(title, body):
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=[System.Windows.Forms.NotifyIcon]::new();"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(10000,'%s','%s',[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 10" % (title, body)
    )
    subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])


def main():
    anchor_list, _ = load_anchors()
    dev = frida.get_local_device()

    running = [p.pid for p in dev.enumerate_processes() if p.name == "Weixin.exe"]
    if running:
        print(f"!! 微信仍在运行 (PIDs: {running})", flush=True)
        print("   请先完全退出微信：托盘图标右键 → 退出", flush=True)
        sys.exit(1)

    freq = Counter()          # 32B 窗口 → 出现次数
    first_meta = {}           # 32B 窗口 → 首次来源
    hex_freq = Counter()
    hex_meta = {}
    login_at = None
    sessions = {}

    def on_cand(p):
        key = bytes(p["data"])
        if login_at is None:
            freq[key] += 1
            first_meta.setdefault(key, (p["anchor"], p["reg"], p["off"]))
        else:
            freq[key] += 5  # 登录后出现的候选权重 x5
            first_meta.setdefault(key, (p["anchor"], p["reg"], p["off"]))
        if freq[key] in (1, 2, 5, 10, 20):
            print(f"  [cand freq={freq[key]}] {p['anchor']} r{p['reg']} off={p['off']}",
                  flush=True)

    def on_hex(p):
        t = p["text"]
        if len(t) % 2 == 0 and 64 <= len(t) <= 256:
            try:
                raw = bytes.fromhex(t)
            except ValueError:
                return
            hex_freq[raw] += 1
            hex_meta.setdefault(raw, (p["anchor"], p["reg"]))

    def make_handler(tag):
        def handler(msg, data):
            if msg.get("type") == "send":
                p = msg["payload"]
                if p.get("type") == "log":
                    print(f"  [{tag}] {p['msg']}", flush=True)
                elif p.get("type") == "cand":
                    on_cand(p)
                elif p.get("type") == "hex":
                    on_hex(p)
            elif msg.get("type") == "error":
                print(f"  [{tag} JS ERROR] {msg.get('description')}", flush=True)
        return handler

    def db_signal_mtimes():
        out = []
        for db in SIGNAL_DBS:
            try:
                out.append(os.path.getmtime(db))
            except OSError:
                out.append(0.0)
        return out

    print("spawn 微信 ...", flush=True)
    pid = dev.spawn([WECHAT_EXE])
    print(f"spawned PID={pid}", flush=True)

    session = frida.attach(pid)
    script = session.create_script(build_hook_js(anchor_list, wait_for_module=True))
    script.on("message", make_handler(f"main:{pid}"))
    script.load()
    sessions[pid] = session

    dev.resume(pid)
    print("resumed — 请尽快登录微信！", flush=True)
    toast("微信密钥捕捞 v4", "请尽快在微信窗口完成登录（点登录或扫码），登录后自动观察 2 分钟")

    baseline = db_signal_mtimes()
    known = {pid}
    t0 = time.time()
    notified_5m = False
    while True:
        if login_at is None:
            if time.time() - t0 > LOGIN_TIMEOUT:
                print("!! 15 分钟内未检测到登录（数据库未被打开），收工。", flush=True)
                break
            cur = db_signal_mtimes()
            if cur != baseline:
                login_at = time.time()
                print(f"== 检测到登录信号（数据库被写入），再观察 {POST_LOGIN_WATCH}s ==",
                      flush=True)
        else:
            if time.time() - login_at > POST_LOGIN_WATCH:
                print("登录后观察期满，收工。", flush=True)
                break
            if time.time() - login_at > 60 and not notified_5m:
                pass
        if login_at is None and time.time() - t0 > 300 and not notified_5m:
            notified_5m = True
            print("  已等 5 分钟，未检测到登录……（继续等待，最多 15 分钟）", flush=True)
        try:
            for p in dev.enumerate_processes():
                if p.name == "Weixin.exe" and p.pid not in known:
                    try:
                        s = frida.attach(p.pid)
                        sc = s.create_script(build_hook_js(anchor_list, wait_for_module=False))
                        sc.on("message", make_handler(f"child:{p.pid}"))
                        sc.load()
                        known.add(p.pid)
                        sessions[p.pid] = s
                        print(f"  attached 子进程 PID={p.pid}", flush=True)
                    except Exception as e:
                        print(f"  attach {p.pid} 失败: {e}", flush=True)
        except Exception as e:
            print(f"  扫描子进程异常: {e}", flush=True)
        time.sleep(2)

    print("detach 全部会话（微信保持运行）...", flush=True)
    for s in list(sessions.values()):
        try:
            s.detach()
        except Exception:
            pass

    top = freq.most_common(300)
    cands = []
    for key, cnt in top:
        anchor, reg, off = first_meta.get(key, ("?", "?", "?"))
        cands.append({"hex": key.hex(), "freq": cnt,
                      "anchor": anchor, "reg": reg, "off": off})
    for raw, cnt in hex_freq.most_common(20):
        anchor, reg = hex_meta.get(raw, ("?", "?"))
        cands.append({"hex": raw.hex(), "freq": cnt, "anchor": anchor,
                      "reg": reg, "off": 0, "is_hex_str": True})

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cands, f, ensure_ascii=False, indent=1)
    print(f"完成：{len(freq)} 种熵窗口 + {len(hex_freq)} 种 hex 串，"
          f"取高频 {len(cands)} 条 → {OUT}", flush=True)
    if login_at:
        print(f"登录时刻: 捕捞开始后 {login_at - t0:.0f} 秒", flush=True)
    print("\n=== 高频候选 TOP 20 ===", flush=True)
    for c in cands[:20]:
        print(f"  freq={c['freq']:>3}  {c['anchor']} r{c['reg']} off={c['off']}  "
              f"{c['hex'][:32]}...", flush=True)


if __name__ == "__main__":
    main()
