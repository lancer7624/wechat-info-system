# -*- coding: utf-8 -*-
"""QQ 置顶会话名单:recent_contact_top_table → 群号/uid → 名称"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
import sqlcipher3

KEY = "1d2a8f4f1c1237d89684b2c2c4725bd30391f0c6dcdb3cb4ce6ab9a679cd74a5"
KEY2 = "<你的QQ-group_info库key,用qq_auto_refresh.py自动抓>"


def open_db(path, key):
    con = sqlcipher3.connect(path)
    cur = con.cursor()
    cur.execute("PRAGMA key = \"x'%s'\"" % key)
    cur.execute("PRAGMA kdf_iter = 4000")
    cur.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
    cur.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
    return con, cur


def main():
    con2, c2 = open_db("qq_group_info_stripped.db", KEY2)
    c2.execute('SELECT "60001", "60007" FROM group_detail_info_ver1')
    gname = {str(a): b for a, b in c2.fetchall() if b}
    con2.close()

    con, cur = open_db("qq_nt_msg_stripped.db", KEY)
    cur.execute('SELECT "40010", "60001", "1000" FROM recent_contact_top_table')
    tops = cur.fetchall()
    print("置顶 %d 个:" % len(tops))
    for t, gid, uid in tops:
        if t == 2 and gid:
            print("  [群] %s -> %s" % (gid, gname.get(str(gid), "(不在群详情)")))
        elif gid:
            print("  [类型%d] 群号%s -> %s" % (t, gid, gname.get(str(gid), "?")))
        else:
            print("  [个人] uid=%s" % uid)
    con.close()


if __name__ == "__main__":
    main()
