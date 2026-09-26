# -*- coding: utf-8 -*-
"""Phase 2d：宽网捕捞 v3

v2 教训：0 候选。静态取证发现真凶——
- 微信把 SQLCipher 标准报错串全删了，旧锚点 0x562A520 是错的
- 真正引用 codec 字符串的函数在 0x35xxxx 区域（旧核心区 0x56xxxx 之外！）
- "ATTACH DATABASE '%s' as migrate KEY '%q'" 表明是 SQLite Multiple Ciphers 体系

v3 锚点集合（自动从 DLL 提取）：
1. 精选函数（anchors_focus.json：pragma 表、cipher_migrate、codec open 等）
2. 每个精选函数 ±0x1000 邻域函数（同一编译单元）
3. 每个精选函数的调用者（E8 rel32 扫描）
4. 旧核心区 0x561F680-0x56B0000（1391 个，保底）
5. 旧锚点 0x562A520 / 0x5627D30

捕获规则升级：
- 32 字节熵滑窗（阈值放宽：kinds>=16, zeros<0.3, printable<0.7）
- 64 字符 hex 串（PRAGMA key = 'x...' 场景，纯 hex 会被熵过滤误杀）
- 每次调用计数，每 20s 汇总一次 → 即使 0 候选也能知道哪些锚点在登录时执行
"""
import frida
import sys
import time
import json
import os
import subprocess
import struct
import bisect

WECHAT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
DLL_PATH = r"C:\Program Files\Tencent\Weixin\4.1.12.55\Weixin.dll"
LOGIN_TIMEOUT = 300   # 等登录最多 5 分钟
POST_HIT_WATCH = 90   # 首次命中后继续观察 90 秒
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "candidates.json")

# ---------------- 静态锚点提取 ----------------

def load_anchors():
    with open(DLL_PATH, "rb") as f:
        data = f.read()

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    opt_off = e_lfanew + 24
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    magic = struct.unpack_from("<H", data, opt_off)[0]
    assert magic == 0x20B
    image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
    num_sec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    sec_off = opt_off + opt_size

    secs = []
    for i in range(num_sec):
        o = sec_off + i * 40
        name = data[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        vsize = struct.unpack_from("<I", data, o + 8)[0]
        va = struct.unpack_from("<I", data, o + 12)[0]
        raw_size = struct.unpack_from("<I", data, o + 16)[0]
        raw_off = struct.unpack_from("<I", data, o + 20)[0]
        secs.append((name, va, vsize, raw_off, raw_size))

    pdata = next(s for s in secs if s[0] == ".pdata")
    _, _, _, praw, prsize = pdata
    funcs = []
    for i in range(prsize // 12):
        begin, end, uw = struct.unpack_from("<III", data, praw + i * 12)
        if begin:
            funcs.append((begin, end))
    funcs.sort()
    begins = [b for b, e in funcs]

    def rva_to_func(rva):
        i = bisect.bisect_right(begins, rva) - 1
        if i >= 0 and funcs[i][0] <= rva < funcs[i][1]:
            return i, funcs[i]
        return None

    text = next(s for s in secs if s[0] == ".text")
    _, tva, tvsize, traw, trsize = text
    text_data = data[traw:traw + trsize]

    anchors = {}  # rva -> 显示名

    # 1. 精选函数
    with open(os.path.join(HERE, "anchors_focus.json"), "r", encoding="utf-8") as f:
        focus = json.load(f)["anchors"]
    for a in focus:
        anchors.setdefault(a["rva"], f"focus:{a['name']}")
    print(f"精选函数: {len(focus)}")

    # 2. 邻域 ±0x1000
    nb = 0
    for a in focus:
        for b, e in funcs:
            if a["rva"] - 0x1000 <= b < a["end"] + 0x1000:
                if b not in anchors:
                    anchors[b] = "nbr"
                    nb += 1
    print(f"邻域函数: +{nb}")

    # 3. 调用者（E8 rel32）
    callers = 0
    for a in focus:
        tgt_lo, tgt_hi = a["rva"], a["end"]
        st = 0
        while True:
            i = text_data.find(b"\xE8", st)
            if i == -1:
                break
            disp = struct.unpack_from("<i", text_data, i + 1)[0]
            insn_rva = (tva + i)  # 相对 .text 起点
            target = insn_rva + 5 + disp
            if tgt_lo <= target < tgt_hi:
                r = rva_to_func(insn_rva)
                if r and r[1][0] not in anchors:
                    anchors[r[1][0]] = "caller"
                    callers += 1
            st = i + 1
    print(f"调用者函数: +{callers}")

    # 4. 旧核心区
    old = 0
    for b, e in funcs:
        if 0x561F680 <= b < 0x56B0000 and b not in anchors:
            anchors[b] = "regionA"
            old += 1
    print(f"旧核心区: +{old}")

    # 5. 旧锚点
    for rva, nm in [(0x562A520, "old:codec_attach"), (0x5627D30, "old:pragma_dispatch")]:
        anchors.setdefault(rva, nm)

    lst = [{"rva": rva, "tag": tag} for rva, tag in sorted(anchors.items())]
    print(f"锚点总数: {len(lst)}\n")
    return lst, image_base


# ---------------- JS 脚本 ----------------

CAND_JS = r"""
var sendBudget = 50, sendCount = 0, sendWindowStart = Date.now();
var counts = {};
var T = new Uint8Array(256);

function windowPass(w) {
  // w: 32 字节 Uint8Array
  T.fill(0);
  var d = 0, zeros = 0, printable = 0;
  for (var i = 0; i < 32; i++) {
    var b = w[i];
    if (T[b] === 0) { T[b] = 1; d++; }
    if (b === 0) zeros++;
    if (b >= 32 && b < 127) printable++;
  }
  if (zeros > 9) return false;        // zeros < 0.3
  if (printable > 21) return false;   // printable < 0.7
  return d >= 16;
}

function hexPass(buf) {
  // 寻找 64 个连续 hex 字符
  if (buf.length < 64) return null;
  var best = 0, cur = 0, bestOff = -1;
  for (var i = 0; i < buf.length; i++) {
    var b = buf[i];
    var isHex = (b >= 48 && b <= 57) || (b >= 65 && b <= 70) || (b >= 97 && b <= 102);
    if (isHex) {
      cur++;
      if (cur > best) { best = cur; bestOff = i - cur + 1; }
    } else {
      cur = 0;
    }
  }
  if (best >= 64) {
    var out = '';
    for (var i = 0; i < best; i++) out += String.fromCharCode(buf[bestOff + i]);
    return out;
  }
  return null;
}

function onEnter(name, args) {
  counts[name] = (counts[name] || 0) + 1;
  for (var i = 0; i < 4; i++) {
    var p = args[i];
    if (p.isNull()) continue;
    var v = p.toInt32();
    if (v === 0 || (v & 3) !== 0) continue;
    var buf;
    try { buf = p.readByteArray(256); } catch (e) { continue; }
    var u8 = new Uint8Array(buf);
    // hex 串优先（hex 内容会被熵过滤误杀）
    var hx = hexPass(u8);
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
    # 映射 a%04d -> rva/tag 在 Python 端反查
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
setInterval(function() {
  if (Object.keys(counts).length === 0) return;
  send({type: 'counts', c: counts});
  counts = {};
}, 20000);
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
    name_by_idx = {i: (a["rva"], a["tag"]) for i, a in enumerate(anchor_list)}

    dev = frida.get_local_device()

    running = [p.pid for p in dev.enumerate_processes() if p.name == "Weixin.exe"]
    if running:
        print(f"!! 微信仍在运行 (PIDs: {running})")
        print("   请先完全退出微信：托盘图标右键 → 退出")
        sys.exit(1)

    candidates = []
    hex_candidates = []
    seen = set()
    sessions = {}

    def make_handler(tag):
        def on_message(msg, data):
            if msg.get("type") == "send":
                p = msg["payload"]
                if p.get("type") == "log":
                    print(f"  [{tag}] {p['msg']}", flush=True)
                elif p.get("type") == "counts":
                    hot = sorted(p["c"].items(), key=lambda kv: -kv[1])[:12]
                    lines = ["%s=%d" % (a, c) for a, c in hot]
                    print(f"  [{tag} 调用计数] " + "  ".join(lines), flush=True)
                elif p.get("type") == "cand":
                    key = bytes(p["data"])
                    h = hash(key)
                    if h in seen:
                        return
                    seen.add(h)
                    candidates.append({
                        "anchor": p["anchor"], "reg": p["reg"],
                        "off": p["off"], "hex": key.hex(),
                    })
                    if len(candidates) % 10 == 1 or len(candidates) <= 3:
                        print(f"  [cand #{len(candidates)}] {p['anchor']} r{p['reg']} "
                              f"off={p['off']}", flush=True)
                elif p.get("type") == "hex":
                    t = p["text"]
                    if len(t) % 2 == 0 and 64 <= len(t) <= 256:
                        try:
                            raw = bytes.fromhex(t)
                        except ValueError:
                            return
                        h = hash(raw)
                        if h in seen:
                            return
                        seen.add(h)
                        hex_candidates.append({
                            "anchor": p["anchor"], "reg": p["reg"],
                            "off": 0, "hex": raw.hex(),
                        })
                        print(f"  [hex #{len(hex_candidates)}] {p['anchor']} r{p['reg']} "
                              f"len={len(raw)}B", flush=True)
            elif msg.get("type") == "error":
                print(f"  [{tag} JS ERROR] {msg.get('description')}", flush=True)
        return on_message

    print("spawn 微信 ...", flush=True)
    pid = dev.spawn([WECHAT_EXE])
    print(f"spawned PID={pid}", flush=True)

    session = frida.attach(pid)
    script = session.create_script(build_hook_js(anchor_list, wait_for_module=True))
    script.on("message", make_handler(f"main:{pid}"))
    script.load()
    sessions[pid] = session

    dev.resume(pid)
    print("resumed — 请登录微信！", flush=True)
    toast("微信密钥捕捞 v3", "请在微信窗口完成登录（扫码或点击登录）")

    known = {pid}
    first_hit_at = None
    t0 = time.time()
    while True:
        if first_hit_at is None:
            if time.time() - t0 > LOGIN_TIMEOUT:
                print("!! 5 分钟内未检测到登录（无 hit），收工。", flush=True)
                break
        else:
            if time.time() - first_hit_at > POST_HIT_WATCH:
                print("首次命中后观察期满，收工。", flush=True)
                break
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
        if first_hit_at is None and (candidates or hex_candidates):
            first_hit_at = time.time()
            print(f"== 检测到登录（首批候选），再观察 {POST_HIT_WATCH}s ==", flush=True)
        time.sleep(2)

    print("detach 全部会话（微信保持运行）...", flush=True)
    for s in list(sessions.values()):
        try:
            s.detach()
        except Exception:
            pass

    total = candidates + hex_candidates
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(total, f, ensure_ascii=False, indent=1)
    print(f"完成：{len(candidates)} 条熵候选 + {len(hex_candidates)} 条 hex 候选"
          f" → {OUT}", flush=True)


if __name__ == "__main__":
    main()
