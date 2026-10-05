# -*- coding: utf-8 -*-
"""只读内存扫描:定位微信会话对象,找置顶标志。
纪律:attach 只读,不写内存,不 hook 函数,扫完即 detach。
用法: python mem_scan_top.py
"""
import frida
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 34 个 chatroom username,来自 session.db
with open(os.path.join(HERE, "tmp_users.json"), encoding="utf-8") as f:
    USERS = json.load(f)

JS = r"""
'use strict';
const users = %s;

// 发送搜索任务给 Python 端分批做,这里只枚举内存范围
function main() {
    // 枚举可读写区域,过滤掉巨型的文件映射(>256MB)
    const ranges = Process.enumerateRanges('rw-').filter(r => r.size > 0x1000 && r.size < 0x10000000);
    const hits = [];
    const encUsers = users.map(u => {
        const buf = [];
        for (let i = 0; i < u.length; i++) {
            const c = u.charCodeAt(i);
            buf.push(c & 0xFF, c >> 8);
        }
        return { u, bytes: buf };
    });

    let ri = 0, off = 0;
    const CHUNK = 0x400000;  // 4MB per slice

    function step() {
        if (ri >= ranges.length) {
            send({ type: 'hits', hits: hits.splice(0) });
            send({ type: 'done' });
            return;
        }
        const r = ranges[ri];
        try {
            const len = Math.min(CHUNK, r.size - off);
            if (len <= 0) { ri++; off = 0; }
            else {
                const data = r.base.add(off).readByteArray(len);
                const u8 = new Uint8Array(data);
                for (const { u, bytes } of encUsers) {
                    const idx = indexOfBytes(u8, bytes);
                    if (idx >= 0) {
                        const start = Math.max(0, idx - 48);
                        const dump = Array.from(u8.slice(start, idx + bytes.length + 96));
                        const addr = r.base.add(off).add(idx);
                        hits.push({
                            user: u,
                            addr: addr.toString(),
                            range: r.base.toString() + ' +' + r.size,
                            hex: dump.map(b => b.toString(16).padStart(2, '0')).join(''),
                        });
                        if (hits.length > 200) {
                            send({ type: 'hits', hits: hits.splice(0) });
                        }
                    }
                }
                off += len;
                if (off >= r.size) { ri++; off = 0; }
            }
        } catch (e) {
            ri++; off = 0;  // 读失败的区域跳过
        }
        send({ type: 'progress', ri: ri, n: ranges.length });
        setTimeout(step, 0);  // 让出事件循环,避免卡死 load
    }

    send({ type: 'ready' });
    setTimeout(step, 10);
}

function indexOfBytes(haystack, needle) {
    const h = haystack.length, n = needle.length;
    if (n === 0 || h < n) return -1;
    outer: for (let i = 0; i <= h - n; i++) {
        if (haystack[i] !== needle[0]) continue;
        for (let j = 1; j < n; j++) {
            if (haystack[i + j] !== needle[j]) continue outer;
        }
        return i;
    }
    return -1;
}

main();
""" % json.dumps(USERS)


def main():
    print(f"目标: 34 个 chatroom username, attach Weixin 主进程", flush=True)
    dev = frida.get_local_device()
    # 找内存最大的 Weixin 进程
    procs = [p for p in dev.enumerate_processes() if p.name == "Weixin.exe"]
    if not procs:
        print("微信没在跑", flush=True)
        sys.exit(1)
    target = max(procs, key=lambda p: p.parameters.get("memory_usage", 0)
                 if hasattr(p, "parameters") else 0)
    print(f"attach PID={target.pid}", flush=True)

    session = dev.attach(target.pid)
    script = session.create_script(JS)
    hits = []
    done = False

    def on_msg(msg, data):
        nonlocal done
        if msg.get("type") == "send":
            p = msg["payload"]
            if p.get("type") == "hits":
                hits.extend(p["hits"])
                print(f"  命中累计 {len(hits)} 条", flush=True)
            elif p.get("type") == "done":
                done = True
            elif p.get("type") == "progress":
                pass
        elif msg.get("type") == "error":
            print(f"JS 错误: {msg.get('description')}", flush=True)
            done = True

    script.on("message", on_msg)
    script.load()

    import time
    while not done:
        time.sleep(1)

    out = os.path.join(HERE, "mem_hits.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(hits, f, ensure_ascii=False, indent=1)
    print(f"完成: {len(hits)} 个命中 → {out}", flush=True)
    session.detach()
    print("已 detach(微信保持运行)", flush=True)


if __name__ == "__main__":
    main()
