# -*- coding: utf-8 -*-
"""媒体增量归档：图片(dat) / 文件 / 视频 → wechat-export/media/
- 图片源 msg/attach/<md5(username)>/<YYYY-MM>/Img/*.dat → 按会话分目录
  有 image_key.json 时直接解密为 jpg/png 等；没有则原样暂存 .dat（密钥到位后可统一转）
- 文件源 msg/file/<YYYY-MM>/（明文，文件名原样）
- 视频源 msg/video/<YYYY-MM>/
增量：archive_state.json 记录 {相对路径: [mtime, size]}，只处理新增/变化文件。
纯本地拷贝+离线解密，不碰微信进程。
"""
import json
import os
import shutil

from decrypt_dat import decrypt_dat, detect_ext
import wxam

HERE = os.path.dirname(os.path.abspath(__file__))


def load_image_key():
    p = os.path.join(HERE, "image_key.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return bytes.fromhex(json.load(f)["key_hex"])
    return None


class ArchiveState:
    def __init__(self, path):
        self.path = path
        self.map = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.map = json.load(f)
            except Exception:
                self.map = {}

    def is_new(self, rel, mtime, size):
        return self.map.get(rel) != [mtime, size]

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.map, f, ensure_ascii=False)


def _copy_new(src, dst, rel, state, stats_key, stats):
    """增量拷贝一个文件，返回是否处理了"""
    mtime = int(os.path.getmtime(src))
    size = os.path.getsize(src)
    if not state.is_new(rel, mtime, size):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    state.map[rel] = [mtime, size]
    stats[stats_key] += 1
    return True


def archive_media(attach_root, file_root, video_root, media_root,
                  session_names, aes_key=None, log=print):
    """增量归档全部媒体。session_names: {md5(username): 显示名}
    返回统计 dict"""
    state = ArchiveState(os.path.join(media_root, "archive_state.json"))
    stats = {"images": 0, "images_dec": 0, "images_pending": 0,
             "files": 0, "videos": 0}

    # 1. 图片（按会话目录；密钥缺失时原样暂存 dat）
    if os.path.isdir(attach_root):
        for sess_md5 in os.listdir(attach_root):
            sess_dir = os.path.join(attach_root, sess_md5)
            if not os.path.isdir(sess_dir):
                continue
            sname = session_names.get(sess_md5)
            out_sub = (f"{sname}_{sess_md5[:8]}" if sname
                       else f"_其他_{sess_md5[:8]}")
            for ym in os.listdir(sess_dir):
                imgdir = os.path.join(sess_dir, ym, "Img")
                if not os.path.isdir(imgdir):
                    continue
                for fn in os.listdir(imgdir):
                    if not fn.endswith(".dat"):
                        continue
                    src = os.path.join(imgdir, fn)
                    rel = f"images/{sess_md5}/{ym}/{fn}"
                    mtime, size = int(os.path.getmtime(src)), os.path.getsize(src)
                    if not state.is_new(rel, mtime, size):
                        continue
                    base = os.path.join(media_root, "images", out_sub,
                                        ym, fn.rsplit(".", 1)[0])
                    os.makedirs(os.path.dirname(base), exist_ok=True)
                    done = False
                    if aes_key:
                        try:
                            with open(src, "rb") as f:
                                plain, _ = decrypt_dat(f.read(),
                                                       aes_key=aes_key)
                            ext = detect_ext(plain)
                            if plain[:4] == b"wxgf":  # 原图：WXGF → ffmpeg 转 JPEG
                                plain = wxam.decode(plain)
                                ext = ".jpg"
                            with open(base + ext, "wb") as f:
                                f.write(plain)
                            stats["images_dec"] += 1
                            done = True
                        except Exception:
                            pass  # 解密失败 → 回退暂存 dat
                    if not done:
                        shutil.copy2(src, base + ".dat")
                        stats["images_pending"] += 1
                    state.map[rel] = [mtime, size]
                    stats["images"] += 1

    # 2. 文件（明文）
    if os.path.isdir(file_root):
        for ym in os.listdir(file_root):
            d = os.path.join(file_root, ym)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                src = os.path.join(d, fn)
                if not os.path.isfile(src):
                    continue
                _copy_new(src, os.path.join(media_root, "files", ym, fn),
                          f"files/{ym}/{fn}", state, "files", stats)

    # 3. 视频
    if os.path.isdir(video_root):
        for ym in os.listdir(video_root):
            d = os.path.join(video_root, ym)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                src = os.path.join(d, fn)
                if not os.path.isfile(src):
                    continue
                _copy_new(src, os.path.join(media_root, "video", ym, fn),
                          f"video/{ym}/{fn}", state, "videos", stats)

    state.save()
    return stats


if __name__ == "__main__":
    # 独立运行：自己构建会话名映射（解密 contact.db）
    import hashlib
    import sqlite3
    from decrypt_db import decrypt_db, load_master
    from export_chat import load_name_map, sanitize_name

    m = load_master(os.path.join(HERE, "db_key.json"))
    base = os.path.expanduser(r"~\Documents\xwechat_files")
    acc = next(os.path.join(base, d) for d in os.listdir(base)
               if os.path.isdir(os.path.join(base, d, "msg")))
    db_root = os.path.join(acc, "db_storage")
    msg = os.path.join(acc, "msg")

    cdata = decrypt_db(os.path.join(db_root, "contact", "contact.db"),
                       m, verify_mac=False)
    name_map = load_name_map(cdata)

    mdata = decrypt_db(os.path.join(db_root, "message", "message_0.db"),
                       m, verify_mac=False)
    tmp = os.path.join(HERE, "_tmp_names.db")
    open(tmp, "wb").write(mdata)
    con = sqlite3.connect(tmp)
    cur = con.cursor()
    cur.execute("SELECT user_name FROM Name2Id")
    users = [r[0] for r in cur.fetchall() if r[0]]
    con.close()
    os.remove(tmp)
    session_names = {}
    for u in users:
        if isinstance(u, bytes):
            u = u.decode("utf-8", "ignore")
        h = hashlib.md5(u.encode()).hexdigest()
        disp = (name_map or {}).get(u, u)
        session_names[h] = sanitize_name(disp) if disp else None

    print("开始增量归档…", flush=True)
    stats = archive_media(
        os.path.join(msg, "attach"), os.path.join(msg, "file"),
        os.path.join(msg, "video"), os.path.join(HERE, "media"),
        session_names, aes_key=load_image_key())
    print(f"归档完成: 图片 {stats['images']}（解密 {stats['images_dec']}，"
          f"暂存 {stats['images_pending']}）、文件 {stats['files']}、"
          f"视频 {stats['videos']}", flush=True)
