# -*- coding: utf-8 -*-
"""QQ key 自动刷新 + 导出(计划任务周期性跑,幂等)
流程: QQ 没跑→退出;跑了→读库盐,盐变→扫内存新 key→更新 qq_export.py→导出
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = r"C:\Users\<你的用户名>\Documents\Tencent Files\<你的QQ号>\nt_qq\nt_db\nt_msg.db"
STATE = os.path.join(HERE, "qq_key_state.json")
EXPORT_PY = os.path.join(HERE, "qq_export.py")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.c_void_p, ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.CloseHandle.argtypes = [wt.HANDLE]


def qq_pids():
    out = subprocess.check_output(
        'tasklist /FI "IMAGENAME eq QQ.exe"', shell=True).decode("gbk", "replace")
    return [int(m) for m in re.findall(r"QQ\.exe\s+(\d+)", out)]


def db_salt():
    if not os.path.exists(DB_PATH):
        return None
    with open(DB_PATH, "rb") as f:
        f.seek(1024)
        return f.read(16).hex()


def scan_key(salt_hex):
    """扫 QQ 进程内存找 x'<64hex><salt>'。返回 key hex 或 None"""
    rx = re.compile(("x'" + "[0-9a-fA-F]{64}" + salt_hex).encode())
    for pid in qq_pids():
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            continue
        addr = 0
        mbi = (ctypes.c_ubyte * 48)()
        buf = (ctypes.c_ubyte * 0x800000)()
        while addr < 0x7FFFFFFFFFFF:
            if k32.VirtualQueryEx(h, addr, mbi, 48) == 0:
                break
            base = ctypes.cast(mbi, ctypes.POINTER(ctypes.c_ulonglong))[0]
            region_size = ctypes.cast(ctypes.byref(mbi, 24),
                                      ctypes.POINTER(ctypes.c_ulonglong))[0]
            state = ctypes.cast(ctypes.byref(mbi, 32), ctypes.POINTER(ctypes.c_uint32))[0]
            protect = ctypes.cast(ctypes.byref(mbi, 36), ctypes.POINTER(ctypes.c_uint32))[0]
            if region_size == 0:
                break
            if state == 0x1000 and not (protect & 0x101):
                off = 0
                while off < region_size:
                    chunk = min(0x800000, region_size - off)
                    read = ctypes.c_size_t()
                    cur = base + off
                    if k32.ReadProcessMemory(h, cur, buf, chunk, ctypes.byref(read)):
                        data = bytes(buf[:read.value])
                        m = rx.search(data)
                        if m:
                            k32.CloseHandle(h)
                            return m.group()[2:66].decode()
                    else:
                        break
                    off += chunk
            addr = base + region_size
            if addr <= base:
                break
        k32.CloseHandle(h)
    return None


def update_key_in_export_py(key_hex):
    s = open(EXPORT_PY, encoding="utf-8").read()
    s = re.sub(r'KEY = "[0-9a-fA-F]{64}"', 'KEY = "%s"' % key_hex, s, count=1)
    open(EXPORT_PY, "w", encoding="utf-8").write(s)


def main():
    if not qq_pids():
        print("QQ 没跑,跳过", flush=True)
        return
    salt = db_salt()
    if not salt:
        print("db 不存在,跳过", flush=True)
        return
    state = {}
    if os.path.exists(STATE):
        state = json.load(open(STATE, encoding="utf-8"))

    if state.get("salt") != salt:
        print("盐变了: %s -> %s,重扫 key..." % (state.get("salt", "无"), salt), flush=True)
        key = scan_key(salt)
        if not key:
            print("key 没扫到(QQ 刚启动,key 可能还没加载),下次再试", flush=True)
            return
        update_key_in_export_py(key)
        state = {"salt": salt, "key": key}
        json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        print("key 已更新", flush=True)
    else:
        print("盐未变,直接用现有 key 导出", flush=True)

    py = r"C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe"
    r = subprocess.run([py, "-X", "utf8", EXPORT_PY], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip()[-200:] if r.stdout else r.stderr[-200:], flush=True)

    # 置顶同步:QQ 置顶表新增的群/联系人自动进白名单
    try:
        sync_pinned()
    except Exception as e:
        print("置顶同步失败(不影响导出): %s" % type(e).__name__, flush=True)


def sync_pinned():
    """读 recent_contact_top_table,把白名单里没有的置顶群号/个人 uid 自动加入 qq_config.json"""
    import sqlcipher3
    cfg_path = os.path.join(HERE, "qq_config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    wl_groups = set(str(x) for x in cfg.get("群聊", []))
    wl_persons = set(str(x) for x in cfg.get("个人", []))
    keys = {}
    if os.path.exists(os.path.join(HERE, "qq_key_state.json")):
        keys = json.load(open(os.path.join(HERE, "qq_key_state.json"), encoding="utf-8"))

    s_msg = os.path.join(HERE, "_sync_msg.db")
    src = DB_PATH
    if not os.path.exists(src):
        return
    # 用 qq_export 的 strip 剥头(重试保护)
    from qq_export import strip as qstrip, open_db as qopen
    for p in (s_msg, s_msg + "-wal", s_msg + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    qstrip(src, s_msg)
    con, cur = qopen(s_msg, keys["key"])
    cur.execute('SELECT "40010", "60001", "1000" FROM recent_contact_top_table')
    added_g, added_p = [], []
    for t, gid, uid in cur.fetchall():
        if t == 2 and gid and str(gid) not in wl_groups:
            wl_groups.add(str(gid))
            added_g.append(str(gid))
        elif uid and str(uid) not in wl_persons:
            wl_persons.add(str(uid))
            added_p.append(str(uid))
    con.close()
    if added_g or added_p:
        cfg["群聊"] = sorted(wl_groups)
        cfg["个人"] = sorted(wl_persons)
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("置顶同步: 新增群 %d 个(%s) 个人 %d 个(%s)"
              % (len(added_g), ",".join(added_g[:3]), len(added_p), ",".join(added_p[:3])),
              flush=True)
        with open(os.path.join(HERE, "sync.log"), "a", encoding="utf-8") as f:
            f.write("[%s] 置顶同步 新增群: %s 新增个人: %s\n"
                    % (__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                       ",".join(added_g), ",".join(added_p)))
    else:
        print("置顶同步: 无新增", flush=True)


if __name__ == "__main__":
    main()
