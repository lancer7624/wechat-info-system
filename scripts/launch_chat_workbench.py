"""Start/reuse this workbench without a console, checking the exact data-root identity."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8711)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    root = args.data_root.resolve()
    repo = Path(__file__).resolve().parents[1]
    if root.is_relative_to(repo) or not 1 <= args.port <= 65535:
        parser.error('Use an external data root and an available loopback port')
    identity = hashlib.sha256(json.dumps(str(root).casefold(), sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    url = 'http://127.0.0.1:' + str(args.port)
    def ready():
        try:
            with urllib.request.urlopen(url + '/api/state', timeout=1) as response:
                value = json.load(response)
            return value.get('app') == 'wechat-author-workbench' and value.get('instance') == identity
        except Exception:
            return False
    child = None
    if not ready():
        root.joinpath('author-workbench').mkdir(parents=True, exist_ok=True)
        interpreter = Path(sys.executable)
        if os.name == 'nt' and interpreter.with_name('pythonw.exe').is_file():
            interpreter = interpreter.with_name('pythonw.exe')
        command = [str(interpreter), '-B', str(repo / 'scripts' / 'chat_workbench.py'),
                   '--data-root', str(root), '--port', str(args.port), '--no-open']
        if args.refresh:
            command.append('--refresh')
        with (root / 'author-workbench' / 'server.log').open('ab') as log:
            child = subprocess.Popen(command, cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(40):
            if ready():
                break
            if child.poll() is not None:
                break
            time.sleep(.25)
        if not ready():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            print(json.dumps({'ok': False, 'error': '工作台未启动，端口可能已被其他程序占用。'}, ensure_ascii=False))
            return 1
        (root / 'author-workbench' / 'runtime.json').write_text(
            json.dumps({'pid': child.pid, 'url': url, 'instance': identity}) + '\n', encoding='utf-8')
    if not args.no_open:
        webbrowser.open(url + '/conversations')
    print(json.dumps({'ok': True, 'url': url + '/conversations', 'reused': child is None}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
