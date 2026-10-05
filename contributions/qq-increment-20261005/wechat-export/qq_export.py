# -*- coding: utf-8 -*-
"""QQ NT 群消息导出 v2:白名单过滤 + 真群名映射,格式对齐微信 export
用法: python qq_export.py [--limit N]
输出: qq_export\<日期>_<群名>.md
"""
import datetime
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
import sqlcipher3

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "<你的QQ-nt_msg库key,用qq_auto_refresh.py自动抓>"
KEY2 = "<你的QQ-group_info库key,用qq_auto_refresh.py自动抓>"
DB_PATH = r"C:\Users\<你的用户名>\Documents\Tencent Files\<你的QQ号>\nt_qq\nt_db\nt_msg.db"
DB2_PATH = r"C:\Users\<你的用户名>\Documents\Tencent Files\<你的QQ号>\nt_qq\nt_db\group_info.db"
STRIP = os.path.join(HERE, "qq_nt_msg_stripped.db")
STRIP2 = os.path.join(HERE, "qq_group_info_stripped.db")
OUT = os.path.join(HERE, "qq_export")

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2000-\u206f0-9a-zA-Z:/.\-@\s]{2,}")
JUNK_WORDS = ("gtip", "uin", "col", "jp", "nor", "txt", "align", "center",
              "qq", "invitor", "invitee", "msg", "num", "keywords", "nt_",
              "b0621c08", "mmbiz", "qpic")


def open_db(path, key, checkpoint=False):
    con = sqlcipher3.connect(path)
    cur = con.cursor()
    cur.execute("PRAGMA key = \"x'%s'\"" % key)
    cur.execute("PRAGMA kdf_iter = 4000")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    if checkpoint:
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # 无 WAL 或已合并,忽略
    return con, cur


def strip(src, dst):
    """剥 1024 头 + 复制 -wal。竞态防护:复制前后 wal 的 mtime/size 变了就重试。
    注意 QQ 的库头是魔改的(offset32 是 QQ_NT DB magic),不能用标准 salt 校验。"""
    import time
    wal = src + "-wal"
    for attempt in range(8):
        before_db = (os.path.getmtime(src), os.path.getsize(src))
        before_wal = None
        if os.path.exists(wal):
            before_wal = (os.path.getmtime(wal), os.path.getsize(wal))
        raw = open(src, "rb").read()
        with open(dst, "wb") as f:
            f.write(raw[1024:])
        wal_data = b""
        if os.path.exists(wal):
            wal_data = open(wal, "rb").read()
            with open(dst + "-wal", "wb") as f:
                f.write(wal_data)
            after_wal = (os.path.getmtime(wal), os.path.getsize(wal))
            if after_wal != before_wal:
                time.sleep(1)
                continue
        after_db = (os.path.getmtime(src), os.path.getsize(src))
        if after_db != before_db:
            time.sleep(1)
            continue
        if os.path.exists(dst + "-shm"):
            os.remove(dst + "-shm")
        return
    raise RuntimeError("复制竞态重试耗尽:QQ 写入太频繁")


def extract_text(blob):
    """提取消息文本:中文为主的长段,过滤 QQ 富文本标记段"""
    if not isinstance(blob, bytes):
        return ""
    try:
        s = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    parts = []
    for m in CJK_RE.finditer(s):
        seg = m.group(0).strip()
        if len(seg) < 2:
            continue
        low = seg.lower()
        if any(w in low for w in JUNK_WORDS):
            continue
        # 中文占比须 >40%,过滤 uid base64 类
        cjk = sum(1 for ch in seg if "\u4e00" <= ch <= "\u9fff")
        if cjk / len(seg) < 0.4:
            continue
        parts.append(seg)
    return " ".join(parts)


def main():
    import time as _t
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    cfg = json.load(open(os.path.join(HERE, "qq_config.json"), encoding="utf-8"))
    whitelist = set(cfg["群聊"])
    persons = set(cfg["个人"])

    # 复制 + 完整性检查重试:QQ 运行中复制有概率拿到坏页
    for attempt in range(5):
        for p in (STRIP, STRIP + "-wal", STRIP + "-shm", STRIP2, STRIP2 + "-wal", STRIP2 + "-shm"):
            if os.path.exists(p):
                os.remove(p)
        strip(DB_PATH, STRIP)
        strip(DB2_PATH, STRIP2)
        con_t, cur_t = open_db(STRIP, KEY)
        try:
            cur_t.execute("PRAGMA integrity_check(1)")
            r = cur_t.fetchone()
            con_t.close()
            if r and r[0] == "ok":
                break
            print("integrity:", str(r)[:60], "重试", flush=True)
        except Exception as e:
            con_t.close()
            print("integrity 炸:", type(e).__name__, "重试", flush=True)
        _t.sleep(2)
    else:
        print("!! 复制重试 5 次仍不完整,继续用最后一份(可能丢消息)", flush=True)

    # 群号 → 群名
    con2, c2 = open_db(STRIP2, KEY2)
    c2.execute('SELECT "60001", "60007" FROM group_detail_info_ver1')
    gname = {str(a): b for a, b in c2.fetchall() if b}
    con2.close()

    # uid → uin(该表在 QQ 运行中有页损坏风险,失败则跳过映射)
    con, cur = open_db(STRIP, KEY)
    uid_uin = {}
    try:
        cur.execute('SELECT "48902", "1002" FROM nt_uid_mapping_table')
        uid_uin = {a: str(b) for a, b in cur.fetchall() if a and b}
    except Exception as e:
        print("uid映射跳过:", type(e).__name__)

    q = ('SELECT "40050", "40020", "40021", "40090", "40800" FROM group_msg_table '
         'ORDER BY "40050" ASC')
    if limit:
        q += " LIMIT %d" % limit
    cur.execute(q)
    rows = cur.fetchall()

    groups = {}
    skipped = 0
    for ts, sender_uid, gid, cached_name, blob in rows:
        gid = str(gid) if gid is not None else ""
        is_person = sender_uid in persons
        if gid not in whitelist and not is_person:
            skipped += 1
            continue
        text = extract_text(blob)
        if not text:
            skipped += 1
            continue
        if is_person:
            key = "个人:" + sender_uid
        else:
            key = gname.get(gid, cached_name or ("群" + gid))
        sender = uid_uin.get(sender_uid, sender_uid or "?")
        groups.setdefault(key, []).append((ts, sender, text))

    day = (sys.argv[1] if len(sys.argv) > 1 else
           datetime.date.today().isoformat())
    out_day = os.path.join(OUT, day)
    os.makedirs(out_day, exist_ok=True)
    count = 0
    for g, msgs in sorted(groups.items(), key=lambda x: -len(x[1])):
        # 只保留 <day> 当天的消息(全量表里有历史,闸门按天核对会命中旧块)
        day_msgs = [(ts, s, t) for ts, s, t in msgs
                    if datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") == day]
        if not day_msgs:
            continue
        fn = os.path.join(out_day, "%s.md" % re.sub(r'[\\/:*?"<>|]', "_", g))
        lines = ["# QQ: %s" % g, "", "> 消息数: %d" % len(day_msgs), ""]
        for ts, sender, text in day_msgs:
            t = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            lines.append("[%s] %s:" % (t, sender))
            lines.append(text)
            lines.append("")
        with open(fn, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        count += 1
    print("白名单导出: %d 个会话, 跳过 %d 条" % (count, skipped), "→", OUT)


if __name__ == "__main__":
    main()
