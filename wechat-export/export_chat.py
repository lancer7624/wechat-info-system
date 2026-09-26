# -*- coding: utf-8 -*-
"""微信聊天记录导出：解密 message_0.db → 按会话导出 Markdown
用法:
    python export_chat.py <message_0.db快照路径> [输出目录]
输出: 输出目录/<会话名>_<hash前8位>.md
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decrypt_db import decrypt_db, load_master

HERE = os.path.dirname(os.path.abspath(__file__))


def try_decode(b):
    if b is None:
        return None
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk"):
        try:
            s = b.decode(enc)
            # 只接受可读文本（过滤 protobuf/压缩二进制）
            printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
            if printable / max(len(s), 1) > 0.9:
                return s
        except Exception:
            continue
    return None


def load_name_map(contact_data):
    """解密后的 contact 字节流 → {username: 显示名}
    显示名规则：备注 remark 优先，其次昵称 nick_name（群聊的群名也在 nick_name）"""
    tmp = os.path.join(HERE, "_tmp_contact_map.db")
    with open(tmp, "wb") as f:
        f.write(contact_data)
    con = sqlite3.connect(tmp)
    name_map = {}
    try:
        cur = con.cursor()
        cur.execute("SELECT username, remark, nick_name FROM contact")
        for u, r, n in cur.fetchall():
            if not u:
                continue
            disp = try_decode(r) or try_decode(n)
            if disp and disp.strip():
                name_map[u] = disp.strip()
        return name_map
    finally:
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def sanitize_name(s):
    """清理文件名中的 Windows 非法字符"""
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "_")
    return s


def norm_name(s):
    """群名归一化匹配：只留汉字/字母/数字（去 emoji、符号、空白）
    两边用同一函数归一化后再比对，emoji 变体差异不影响匹配"""
    return "".join(ch for ch in s
                   if ch.isalnum() or "一" <= ch <= "鿿")


def export_decrypted(data, out_dir, name_map=None, since_ts=None,
                     only_names=None):
    """解密后的 message_0 字节流 → 按会话导出 Markdown，返回 (会话数, 消息数)
    name_map: {username: 显示名}，缺省时文件名用原始 ID
    since_ts: 只导出 create_time > since_ts 的消息（增量模式），None = 全量
    only_names: 群聊白名单（显示名列表），非 None 时只导出名单内会话（归一化匹配）"""
    os.makedirs(out_dir, exist_ok=True)
    # 清掉旧导出文件（避免上一轮残留旧文件名）
    for old in os.listdir(out_dir):
        if old.endswith(".md"):
            try:
                os.remove(os.path.join(out_dir, old))
            except OSError:
                pass
    wanted = {norm_name(n) for n in (only_names or [])}
    tmp = os.path.join(HERE, "_tmp_export.db")
    with open(tmp, "wb") as f:
        f.write(data)
    con = sqlite3.connect(tmp)
    try:
        cur = con.cursor()

        # Name2Id：用户名列表（列名动态读）
        cur.execute("SELECT * FROM Name2Id LIMIT 1")
        cols = [d[0] for d in cur.description]
        usernames = []
        if "UsrName" in cols:
            cur.execute("SELECT UsrName FROM Name2Id")
            usernames = [r[0] for r in cur.fetchall() if r[0]]
        elif cols:
            cur.execute(f"SELECT {cols[0]} FROM Name2Id")
            usernames = [r[0] for r in cur.fetchall()
                         if isinstance(r[0], str) and r[0]]

        # md5(username) → Msg_<md5> 映射
        name_by_hash = {}
        for u in usernames:
            h = hashlib.md5(u.encode("utf-8")).hexdigest()
            name_by_hash[h] = u

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg_%'")
        tables = [r[0] for r in cur.fetchall()]

        total_msgs = 0
        exported = 0
        for t in tables:
            h = t[4:]
            raw = name_by_hash.get(h)
            if raw:
                sname = (name_map or {}).get(raw, raw)
            else:
                sname = "(未知会话)"
            if only_names is not None and norm_name(sname) not in wanted:
                continue
            if since_ts:
                cur.execute(f'SELECT local_id, create_time, message_content '
                            f'FROM "{t}" WHERE create_time > ? ORDER BY local_id',
                            (since_ts,))
            else:
                cur.execute(f'SELECT local_id, create_time, message_content '
                            f'FROM "{t}" ORDER BY local_id')
            rows = cur.fetchall()
            if not rows:
                continue
            lines = []
            msgs = 0
            for lid, ctime, mc in rows:
                text = try_decode(mc)
                if text is None:
                    continue
                ts = time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(ctime)) if ctime else "?"
                lines.append(f"[{ts}] {text.strip()}")
                msgs += 1
            if not lines:
                continue
            safe = sanitize_name(sname)
            fname = f"{safe}_{h[:8]}.md"
            with open(os.path.join(out_dir, fname), "w",
                      encoding="utf-8") as f:
                f.write(f"# 会话: {sname} ({h[:8]}…)\n\n")
                f.write(f"> 消息数: {msgs}（可读文本，自 {since_ts or 0} 起）\n\n")
                f.write("\n".join(lines) + "\n")
            exported += 1
            total_msgs += msgs
        return exported, total_msgs
    finally:
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def _grab_xml_cdata(text, tag):
    """从公众号推送 XML 里提取 <tag><![CDATA[xxx]]></tag> 或 <tag>xxx</tag>"""
    m = re.search(rf"<{tag}>\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*</{tag}>", text)
    if m:
        return m.group(1).strip()
    m = re.search(rf"<{tag}>\s*([\s\S]*?)\s*</{tag}>", text)
    return m.group(1).strip() if m else ""


def _extract_biz(mc):
    """biz 消息体 → {"title", "des", "url"} 或 None
    message_content 是 zstd 压缩的公众号推送 XML（2026-09-08 实测直解成功）"""
    if mc is None:
        return None
    text = try_decode(mc)
    if text is None:
        try:
            import zstandard as zstd
            raw = zstd.ZstdDecompressor().decompress(
                bytes(mc), max_output_size=4 << 20)
            text = raw.decode("utf-8", "ignore")
        except Exception:
            return None
    title = _grab_xml_cdata(text, "title")
    des = _grab_xml_cdata(text, "des")
    url = _grab_xml_cdata(text, "url")
    if not title and not url:
        return None
    return {"title": title, "des": des,
            "url": url if url.startswith("http") else ""}


def export_biz_decrypted(data, out_dir, name_map=None, since_ts=None,
                         whitelist=None):
    """解密后的 biz_message_0 字节流 → 订阅号推送清单
    产出：out_dir/公众号_<名>_<hash8>.md + out_dir/biz_articles.json（结构化）
    whitelist: {归一化名: 类别}，非 None 时只导出名单内公众号，
               命中推送在 json 里带 category 字段（信息活动类/知识价值类）
    返回 (公众号数, 推送数)"""
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(HERE, "_tmp_biz.db")
    with open(tmp, "wb") as f:
        f.write(data)
    con = sqlite3.connect(tmp)
    articles = []
    per_acc = {}
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM Name2Id LIMIT 1")
        cols = [d[0] for d in cur.description]
        if "user_name" in cols:
            cur.execute("SELECT user_name FROM Name2Id")
        else:
            cur.execute(f"SELECT {cols[0]} FROM Name2Id")
        users = [r[0] for r in cur.fetchall() if r[0]]

        name_by_hash = {}
        for u in users:
            if isinstance(u, bytes):
                u = u.decode("utf-8", "ignore")
            h = hashlib.md5(u.encode()).hexdigest()
            name_by_hash[h] = u

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg_%'")
        tables = [r[0] for r in cur.fetchall()]

        for t in tables:
            h = t[4:]
            gh = name_by_hash.get(h)
            sname = (name_map or {}).get(gh, gh or "(未知公众号)")
            if whitelist is not None:
                cat = whitelist.get(norm_name(sname))
                if cat is None:
                    continue
            else:
                cat = None
            if since_ts:
                cur.execute(f'SELECT create_time, message_content '
                            f'FROM "{t}" WHERE create_time > ? ORDER BY local_id',
                            (since_ts,))
            else:
                cur.execute(f'SELECT create_time, message_content '
                            f'FROM "{t}" ORDER BY local_id')
            for ctime, mc in cur.fetchall():
                info = _extract_biz(mc)
                if info is None:
                    continue
                info["gh"] = gh
                info["name"] = sname
                info["time"] = ctime
                info["category"] = cat
                articles.append(info)
                per_acc.setdefault(sname, []).append(info)

        for sname, lst in per_acc.items():
            safe = sanitize_name(sname)
            h = name_by_hash.get(None, "")
            lines = [f"# 公众号: {sname}",
                     f"> 推送数: {len(lst)}（带链接 {sum(1 for a in lst if a['url'])} 篇）",
                     ""]
            for a in lst:
                ts = time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(a["time"])) if a["time"] else "?"
                lines.append(f"[{ts}] {a['title']}")
                if a["url"]:
                    lines.append(f"  {a['url']}")
                if a["des"]:
                    lines.append(f"  → {a['des'][:150]}")
            with open(os.path.join(out_dir, f"公众号_{safe}.md"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

        with open(os.path.join(out_dir, "biz_articles.json"), "w",
                  encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, indent=1)
        return len(per_acc), len(articles)
    finally:
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def main():
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        HERE, "export", time.strftime("%Y-%m-%d"))
    master = load_master(os.path.join(HERE, "db_key.json"))
    print("解密 message_0.db …", flush=True)
    data = decrypt_db(src, master, verify_mac=False)
    exported, total = export_decrypted(data, out_dir)
    print(f"导出完成：{exported} 个会话，{total} 条可读消息 → {out_dir}",
          flush=True)


if __name__ == "__main__":
    main()
