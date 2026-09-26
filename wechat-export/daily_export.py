# -*- coding: utf-8 -*-
"""微信信息管理系统 · 增量导出（Windows 计划任务三班：12:00 / 18:00 / 22:00）
流程：快照 6 库(+WAL) → 解密+MAC 校验 → 增量导出 Markdown（水位过滤）→
     订阅号推送清单（biz 库 zstd 解压）→ 关键词兜底 toast → 媒体归档 → 清理
全程离线，不碰微信进程。日志：wechat-export/export.log
水位：export_watermark.json（{"ts": 上次班次快照时间}，5 分钟回看防漏）
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decrypt_db import decrypt_db, load_master
from export_chat import (export_decrypted, export_biz_decrypted,
                         load_name_map, norm_name, sanitize_name)
from media_archive import archive_media, load_image_key

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
DB_ROOT = r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\db_storage"
MSG_ROOT = r"C:\Users\YOURNAME\Documents\xwechat_files\wxid_YOURWXID\msg"
SNAP_ROOT = os.path.join(HERE, "dbsnap", "daily")
OUT_ROOT = os.path.join(HERE, "export")
KEEP_DAYS = 14
WATERMARK = os.path.join(HERE, "export_watermark.json")
KW_STATE = os.path.join(HERE, "kw_state.json")
LOOKBACK = 300          # 水位回看 5 分钟，防 WAL 合并延迟漏消息
FIRST_RUN_WINDOW = 86400  # 首次运行补最近 24 小时

DBS = [
    ("message", "message_0.db"),
    ("message", "biz_message_0.db"),
    ("contact", "contact.db"),
    ("session", "session.db"),
    ("sns", "sns.db"),
    ("favorite", "favorite.db"),
]

KEYWORDS = ["截止", "报名", "明天", "今晚", "今天下午", "今天上午", "会议",
            "活动", "通知", "缴费", "考试", "提交", "地点", "别忘了", "记得"]


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(HERE, "export.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_group_whitelist():
    """config.json → 群聊白名单（仅爬取/分析这些群）。空名单 → None（全量）"""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        groups = [g.strip() for g in (cfg.get("群聊") or []) if g.strip()]
        return groups or None
    except Exception:
        return None


def load_biz_whitelist():
    """config.json → {归一化公众号名: 类别}。空名单 → None（全量）"""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        out = {}
        for cat, names in (cfg.get("公众号") or {}).items():
            for n in names:
                if n.strip():
                    out[norm_name(n.strip())] = cat
        return out or None
    except Exception:
        return None


def load_watermark():
    """水位：{ts: 上次班次快照时间}。不存在 → 首跑，补最近 24h"""
    if os.path.exists(WATERMARK):
        try:
            with open(WATERMARK, encoding="utf-8") as f:
                w = json.load(f)
            if w.get("ts"):
                return w["ts"]
        except Exception:
            pass
    return None


def save_watermark(ts):
    with open(WATERMARK, "w", encoding="utf-8") as f:
        json.dump({"ts": ts}, f)


def cleanup(root, keep):
    if not os.path.isdir(root):
        return
    dirs = sorted([d for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d))], reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        log(f"清理旧目录: {root}\\{d}")


def build_session_names(data, name_map):
    """解密后的 message_0 字节流 → {md5(username): 显示名}（媒体归档用）"""
    tmp = os.path.join(HERE, "_tmp_names.db")
    with open(tmp, "wb") as f:
        f.write(data)
    con = sqlite3.connect(tmp)
    try:
        cur = con.cursor()
        cur.execute("SELECT user_name FROM Name2Id")
        users = [r[0] for r in cur.fetchall() if r[0]]
    finally:
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = {}
    for u in users:
        if isinstance(u, bytes):
            u = u.decode("utf-8", "ignore")
        h = hashlib.md5(u.encode()).hexdigest()
        disp = (name_map or {}).get(u)
        out[h] = sanitize_name(disp) if disp else None
    return out


def global_max_ts(data, tmp_name):
    """解密字节流 → 所有 Msg 表的最大 create_time"""
    tmp = os.path.join(HERE, tmp_name)
    with open(tmp, "wb") as f:
        f.write(data)
    con = sqlite3.connect(tmp)
    max_ts = 0
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'Msg_%'")
        for (t,) in cur.fetchall():
            try:
                cur.execute(f'SELECT MAX(create_time) FROM "{t}"')
                v = cur.fetchone()[0]
                if v:
                    max_ts = max(max_ts, v)
            except Exception:
                pass
    finally:
        con.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
    return max_ts


def keyword_scan(out_dir):
    """关键词兜底：扫当天增量 md，命中活动类关键词 → toast（去重，每行只报一次）"""
    hits = []
    seen = set()
    if os.path.exists(KW_STATE):
        try:
            with open(KW_STATE, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("date") == time.strftime("%Y-%m-%d"):
                seen = set(st.get("seen", []))
        except Exception:
            pass
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".md") or fn.startswith("公众号_"):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line.startswith("[20"):
                continue
            if any(k in line for k in KEYWORDS):
                key = hashlib.md5(line.encode("utf-8")).hexdigest()[:12]
                if key not in seen:
                    seen.add(key)
                    hits.append((fn.rsplit("_", 1)[0], line[:90]))
    with open(KW_STATE, "w", encoding="utf-8") as f:
        json.dump({"date": time.strftime("%Y-%m-%d"), "seen": list(seen)}, f)
    return hits


def notify(title, body):
    try:
        from win11toast import toast
        toast(title, body, duration="short")
    except Exception as e:
        log(f"toast 失败: {e}")
    # 手机推送（飞书群机器人，线一活动提醒；webhook 未配置时静默跳过）
    try:
        sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import feishu_notify
        ok, msg = feishu_notify.send_text(title, body)
        if not ok:
            log(f"飞书推送跳过: {msg}")
    except Exception as e:
        log(f"飞书推送异常: {e}")


def main():
    log("=== 增量导出开始 ===")
    today = time.strftime("%Y-%m-%d")
    snap_dir = os.path.join(SNAP_ROOT, today)
    out_dir = os.path.join(OUT_ROOT, today)
    os.makedirs(snap_dir, exist_ok=True)

    # 1. 快照（主库 + WAL 留档）
    snap_ts = time.time()
    wal_sizes = {}
    for sub, name in DBS:
        src = os.path.join(DB_ROOT, sub, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(snap_dir, name))
        wal = src + "-wal"
        if os.path.exists(wal):
            shutil.copy2(wal, os.path.join(snap_dir, name + "-wal"))
            wal_sizes[name] = os.path.getsize(wal)
    log(f"快照完成 → {snap_dir}")

    master = load_master(os.path.join(HERE, "db_key.json"))

    # 2. 解密（全量 MAC 校验；biz 库与导出时一致用宽松模式）
    ok = True
    for sub, name in DBS:
        p = os.path.join(snap_dir, name)
        if not os.path.exists(p):
            continue
        try:
            decrypt_db(p, master, verify_mac=True)
            log(f"解密通过: {name}")
        except Exception as e:
            log(f"解密失败: {name} - {e}")
            ok = False
    if not ok:
        log("=== 增量导出中止（解密失败）===")
        sys.exit(1)

    # 3. 水位与增量窗口
    # 同一天三班都导到同一个 out_dir，且每班清空重写——若窗口按水位走，
    # 后班会把前班产物覆盖丢。所以：水位日期=今天 → 窗口从当天 0 点起（重写全天）；
    # 水位日期<今天（跨天首班）→ 正常按水位回看增量
    prev_water = load_watermark()
    today_start = time.mktime(time.strptime(today, "%Y-%m-%d"))
    if prev_water is None:
        since = snap_ts - FIRST_RUN_WINDOW
        log(f"首次运行：无水位，补最近 24h（自 "
            f"{time.strftime('%H:%M', time.localtime(since))}）")
    elif time.strftime("%Y-%m-%d", time.localtime(prev_water)) < today:
        since = max(prev_water - LOOKBACK, today_start)
        log(f"增量窗口：自 {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}"
            f"（跨天首班，水位回看 {LOOKBACK}s，且不早于当天 0 点）")
    else:
        since = today_start
        log(f"增量窗口：自当天 0 点重写全天"
            f"（同天多班防覆盖：{time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}）")

    # 4. 导出聊天记录（增量，白名单群聊）
    try:
        data = decrypt_db(os.path.join(snap_dir, "message_0.db"),
                          master, verify_mac=False, use_wal=False)
        cdata = decrypt_db(os.path.join(snap_dir, "contact.db"),
                           master, verify_mac=False, use_wal=False)
        name_map = load_name_map(cdata)
        groups = load_group_whitelist()
        exported, total = export_decrypted(data, out_dir, name_map=name_map,
                                           since_ts=since, only_names=groups)
        log(f"聊天导出: {exported} 个会话 {total} 条新消息"
            f"（白名单 {len(groups or [])} 群）→ {out_dir}")
    except Exception as e:
        log(f"聊天导出失败: {e}")
        sys.exit(1)

    # 5. 订阅号推送清单（biz 库，zstd 解压，白名单两类）
    biz_acc = biz_cnt = 0
    try:
        bdata = decrypt_db(os.path.join(snap_dir, "biz_message_0.db"),
                           master, verify_mac=False, use_wal=False)
        biz_wl = load_biz_whitelist()
        biz_acc, biz_cnt = export_biz_decrypted(bdata, out_dir,
                                                name_map=name_map, since_ts=since,
                                                whitelist=biz_wl)
        log(f"订阅号导出: {biz_acc} 个公众号 {biz_cnt} 条推送"
            f"（白名单 {len(biz_wl or {})} 个）→ biz_articles.json")
    except Exception as e:
        log(f"订阅号导出失败（不影响聊天导出）: {e}")

    # 6. 水位推进（取两库消息最大时间戳与上次水位的大者）
    new_water = max(snap_ts - LOOKBACK, prev_water or 0)
    try:
        new_water = max(new_water, global_max_ts(data, "_tmp_wm_chat.db"))
    except Exception as e:
        log(f"chat 水位计算失败: {e}")
    try:
        if biz_cnt:
            bdata2 = decrypt_db(os.path.join(snap_dir, "biz_message_0.db"),
                                master, verify_mac=False, use_wal=False)
            new_water = max(new_water, global_max_ts(bdata2, "_tmp_wm_biz.db"))
    except Exception as e:
        log(f"biz 水位计算失败: {e}")
    save_watermark(new_water)
    log(f"水位推进: {time.strftime('%Y-%m-%d %H:%M', time.localtime(new_water))}")

    # 7. 关键词兜底 toast（不依赖 Claude 会话）
    try:
        hits = keyword_scan(out_dir)
        if hits:
            title = f"⏰ 关键词提醒 +{len(hits)} 条"
            body = "\n".join(f"{n}: {t}" for n, t in hits[:3])
            if len(hits) > 3:
                body += f"\n…等 {len(hits)} 条"
            notify(title, body)
            log(f"关键词兜底: {len(hits)} 条命中，已通知")
    except Exception as e:
        log(f"关键词兜底失败: {e}")

    # 8. 媒体增量归档
    try:
        session_names = build_session_names(data, name_map)
        stats = archive_media(
            os.path.join(MSG_ROOT, "attach"),
            os.path.join(MSG_ROOT, "file"),
            os.path.join(MSG_ROOT, "video"),
            os.path.join(HERE, "media"),
            session_names, aes_key=load_image_key())
        log(f"媒体归档: 图片 {stats['images']}（解密 {stats['images_dec']}）"
            f"、文件 {stats['files']}、视频 {stats['videos']}")
    except Exception as e:
        log(f"媒体归档失败（不影响导出）: {e}")

    # 9. 活动检测 + 桌面通知（对比当天目录消息数，跨天自动重建基线）
    try:
        import activity_check
        total, changed, new_st = activity_check.detect_activity()
        activity_check.save_state(new_st)
        if changed:
            title, body = activity_check.build_message(total, changed)
            activity_check.notify(title, body)
            log(f"活动通知: {title} - {body}")
    except Exception as e:
        log(f"活动检测失败（不影响导出）: {e}")

    # 10. 清理，保留最近 KEEP_DAYS 天
    cleanup(SNAP_ROOT, KEEP_DAYS)
    cleanup(OUT_ROOT, KEEP_DAYS)

    log("=== 增量导出完成 ===")


if __name__ == "__main__":
    main()
