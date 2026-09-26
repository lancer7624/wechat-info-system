# -*- coding: utf-8 -*-
"""一键打开看板：先保活 recorder.py（127.0.0.1:8710），再开浏览器。
供桌面快捷方式调用（pythonw 运行，无窗口）。"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

PORT = 8710
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
URL = f'http://127.0.0.1:{PORT}/?v={int(time.time())}'  # 带时间戳绕过浏览器缓存
FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


def alive():
    try:
        s = socket.create_connection(('127.0.0.1', PORT), 0.5)
        s.close()
        return True
    except OSError:
        return False


if not alive():
    subprocess.Popen([PYW, os.path.join(BASE, 'scripts', 'recorder.py')], cwd=BASE,
                     creationflags=FLAGS, close_fds=True)
    for _ in range(24):  # 最多等 12 秒
        if alive():
            break
        time.sleep(0.5)

if not alive():
    # 兜底：recorder 起不来就用静态服务器直接把看板挂出来
    subprocess.Popen([PYW, '-m', 'http.server', str(PORT),
                      '--directory', os.path.join(BASE, 'kanban')],
                     cwd=BASE, creationflags=FLAGS, close_fds=True)
    time.sleep(1)

webbrowser.open(URL)
