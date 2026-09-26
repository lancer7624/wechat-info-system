# -*- coding: utf-8 -*-
"""第三轮：定点 dump 解密链 ctx 对象（依据 f0 反汇编定位）
反汇编发现 0xAC5340 = 加密流对象 read 包装器：
- 错误路径把 ctx+0x58（32 字节）转 hex 写日志 → 密钥疑似在 ctx+0x58
- 内部调用链：0x5F4340(check) / 0x5F45F0(read) / 0x5F4BA0(tell) / 0x5F5210
本轮 hook 这 5 个函数入口，dump args[0..3] 各 512B（含 ctx 结构体、
+0x48 起的 64B 重点区）+ 二级指针，去重后 Python 侧试解：
16B 窗口 AES-128 / 32B 窗口 AES-256 / 16B 窗口当作 AES 第10轮密钥逆推。
命中 → 写 image_key.json → 立即 detach。全程只读。
"""
import frida
import json
import os
import struct
import sys
import time

from Crypto.Cipher import AES

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(HERE, "image_key.json")
DLL = r"C:\Program Files\Tencent\Weixin\4.1.13.61\Weixin.dll"
MAX_SECONDS = 480

# (anchor, RVA) — RVA 由 f0 反汇编的 call 目标减去模块基址所得
HOOKS = [
    ("read", 0xAC5340),   # 图片解密读包装器（已确认的解密函数）
    ("chk",  0x5F4340),
    ("rd",   0x5F45F0),
    ("tell", 0x5F4BA0),
    ("f5210", 0x5F5210),
]

# ---------- .pdata 校验函数头 ----------
with open(DLL, "rb") as f:
    pe = f.read()
e_lfanew = struct.unpack_from("<I", pe, 0x3C)[0]
opt_off = e_lfanew + 24
opt_size = struct.unpack_from("<H", pe, e_lfanew + 20)[0]
num_sec = struct.unpack_from("<H", pe, e_lfanew + 6)[0]
sec_off = opt_off + opt_size
SECS = {}
for i in range(num_sec):
    o = sec_off + i * 40
    name = pe[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
    vsize = struct.unpack_from("<I", pe, o + 8)[0]
    va = struct.unpack_from("<I", pe, o + 12)[0]
    raw_size = struct.unpack_from("<I", pe, o + 16)[0]
    raw_off = struct.unpack_from("<I", pe, o + 20)[0]
    SECS[name] = (va, vsize, raw_off, raw_size)

_, _, p_raw_off, p_raw_size = SECS[".pdata"]
HEADS = set()
for i in range(p_raw_size // 12):
    begin, end, uw = struct.unpack_from("<III", pe, p_raw_off + i * 12)
    if begin:
        HEADS.add(begin)
for a, rva in HOOKS:
    print(f"  hook {a:6s} RVA=0x{rva:X} {'✓ 函数头' if rva in HEADS else '⚠ 非函数头'}")

# ---------- AES 工具 ----------
SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C)


def schedule_to_key(last_rk):
    w = [list(last_rk[i:i + 4]) for i in range(0, 16, 4)]
    ws = [None] * 44
    ws[40:44] = w
    for i in range(43, 3, -1):
        if i % 4 == 0:
            g = [SBOX[b] for b in (ws[i - 1][1:] + ws[i - 1][:1])]
            g[0] ^= RCON[i // 4 - 1]
            ws[i - 4] = [ws[i][j] ^ g[j] for j in range(4)]
        else:
            ws[i - 4] = [ws[i][j] ^ ws[i - 1][j] for j in range(4)]
    return bytes(sum(ws[0:4], []))


def is_head(b16):
    return ((b16[0] == 0xFF and b16[1] == 0xD8 and b16[2] == 0xFF
             and b16[3] in (0xE0, 0xE1, 0xDB, 0xC4, 0xFE))
            or b16[:8] == b"\x89PNG\r\n\x1a\n")


def find_samples():
    root = os.path.expanduser(r"~\Documents\xwechat_files")
    s16, s32 = [], []
    for d1 in os.listdir(root):
        attach = os.path.join(root, d1, "msg", "attach")
        if not os.path.isdir(attach):
            continue
        for d2 in os.listdir(attach):
            for d3 in os.listdir(os.path.join(attach, d2)):
                imgdir = os.path.join(attach, d2, d3, "Img")
                if not os.path.isdir(imgdir):
                    continue
                for fn in os.listdir(imgdir):
                    if not fn.endswith("_t.dat"):
                        continue
                    with open(os.path.join(imgdir, fn), "rb") as f:
                        data = f.read(47)
                    if data[:6] == b"\x07\x08V2\x08\x07":
                        s16.append(data[15:31])
                        s32.append(data[15:47])
                        if len(s16) >= 3:
                            return s16, s32
    print("未找到 V2 样本", flush=True)
    sys.exit(1)


JS = r"""
var HOOKS = %s;
var dumps = {};
var installed = false;

function tryRead(p, len) {
  try { return p.readByteArray(len); } catch (e) { return null; }
}
function toHex(b) {
  var u8 = new Uint8Array(b), s = '';
  for (var i = 0; i < u8.length; i++) {
    s += ('0' + u8[i].toString(16)).slice(-2);
  }
  return s;
}
function onEnter(anchor, args) {
  var st = dumps[anchor];
  if (!st) st = dumps[anchor] = {count: 0, seen: {}};
  if (st.count >= 50) return;
  for (var reg = 0; reg < 4; reg++) {
    var p = args[reg];
    if (p === undefined || p.isNull()) continue;
    var buf = tryRead(p, 512);
    if (buf === null) continue;
    var hex = toHex(buf);
    var tag = reg + ':' + hex;
    if (st.seen[tag]) continue;
    st.seen[tag] = 1;
    st.count++;
    var extra = null;
    if (reg === 0) {
      var b58 = tryRead(p.add(0x48), 64);
      if (b58 !== null) extra = toHex(b58);
    }
    send({type: 'ctx', anchor: anchor, reg: reg, data: hex, extra: extra});
    var u8 = new Uint8Array(buf);
    var chased = 0;
    for (var off = 0; off + 8 <= u8.length && chased < 6; off += 8) {
      var q = new DataView(u8.buffer, u8.byteOffset + off, 8).getBigUint64(0, true);
      if (q > 0x10000n && q < 0x7ffc00000000n) {
        var b2 = tryRead(ptr(q.toString()), 512);
        if (b2 !== null) {
          var hex2 = toHex(b2);
          var tag2 = reg + 'L2:' + hex2;
          if (!st.seen[tag2]) {
            st.seen[tag2] = 1;
            st.count++;
            send({type: 'ctx', anchor: anchor, reg: reg + 9,
                  data: hex2, extra: null});
          }
          chased++;
        }
      }
    }
    if (st.count >= 50) return;
  }
}
function jserr(a, e) {
  send({type: 'jserr', anchor: a, msg: String(e)});
}
function install() {
  var mod = Process.findModuleByName('Weixin.dll');
  if (!mod) return;
  if (installed) return;
  var base = mod.base;
  for (var i = 0; i < HOOKS.length; i++) {
    (function (anchor, rva) {
      try {
        Interceptor.attach(base.add(rva), {
          onEnter: function (args) { try { onEnter(anchor, args); } catch (e) { jserr(anchor, e); } }
        });
      } catch (e) {
        jserr(anchor + '(install)', e);
      }
    })(HOOKS[i][0], HOOKS[i][1]);
  }
  installed = true;
  send({type: 'log', msg: '[ctxkey] ' + HOOKS.length + ' hooks @ ' + base});
}
install();
setInterval(install, 100);
"""


def main():
    sample16, sample32 = find_samples()
    print(f"样本: {len(sample16)} 个 V2 缩略图", flush=True)

    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if p.name == "Weixin.exe"]
    print(f"微信进程: {[p.pid for p in procs]}", flush=True)

    found = {"hex": None, "src": None}
    sessions = []
    tested = 0

    def test_key16(k):
        return all(is_head(AES.new(k, AES.MODE_ECB).decrypt(ct)) for ct in sample16)

    def test_buf(b):
        n = len(b)
        if n < 16:
            return None
        for off in range(0, n - 15):
            c = b[off:off + 16]
            if test_key16(c):
                return c.hex()
            k0 = schedule_to_key(c)
            if test_key16(k0):
                return k0.hex()
        if n >= 32:
            for off in range(0, n - 31):
                c = b[off:off + 32]
                if all(is_head(AES.new(c, AES.MODE_ECB).decrypt(ct)) for ct in sample32):
                    return c.hex()
        return None

    def make_handler(pid):
        def handler(msg, data):
            nonlocal tested
            if msg.get("type") != "send":
                if msg.get("type") == "error":
                    print(f"  [pid{pid} JS ERROR] {msg.get('description')}", flush=True)
                return
            p = msg["payload"]
            t = p.get("type")
            if t == "log":
                print(f"  [pid{pid}] {p['msg']}", flush=True)
            elif t == "jserr":
                print(f"  [pid{pid} JS] {p['anchor']}: {p['msg']}", flush=True)
            elif t == "ctx":
                tested += 1
                if found["hex"]:
                    return
                for name, hx in (("buf", p["data"]), ("extra", p["extra"])):
                    if not hx:
                        continue
                    k = test_buf(bytes.fromhex(hx))
                    if k:
                        found["hex"] = k
                        found["src"] = f"pid{pid} {p['anchor']} r{p['reg']} {name}"
                        return
        return handler

    for pr in procs:
        try:
            s = frida.attach(pr.pid)
            sc = s.create_script(JS % json.dumps(
                [[a, rva] for a, rva in HOOKS]))
            sc.on("message", make_handler(pr.pid))
            sc.load()
            sessions.append(s)
            print(f"  attached PID={pr.pid}", flush=True)
        except Exception as e:
            print(f"  attach {pr.pid} 失败: {e}", flush=True)

    if not sessions:
        print("!! 无进程可挂", flush=True)
        sys.exit(1)

    print("hook 就绪。等待微信渲染图片（后台缩略图也会触发）……", flush=True)
    t0 = time.time()
    while not found["hex"] and time.time() - t0 < MAX_SECONDS:
        time.sleep(1)

    for s in sessions:
        try:
            s.detach()
        except Exception:
            pass
    print("detach 完成（微信保持运行）", flush=True)

    if found["hex"]:
        with open(KEY_PATH, "w", encoding="utf-8") as f:
            json.dump({"key_hex": found["hex"], "source": found["src"]}, f, indent=2)
        print(f"\n✅ 命中！AES 密钥: {found['hex']}（来源 {found['src']}）", flush=True)
        print(f"已保存 → {KEY_PATH}", flush=True)
        print(f"统计: 共 {tested} 个 ctx dump", flush=True)
        sys.exit(0)
    else:
        print(f"\n未命中。共 {tested} 个 ctx dump 试解。", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
