"""Open/reuse the bundled conversation note with the isolated window-link component."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / 'conversation_engine'


class NoteLauncher:
    def __init__(self, data_root, *, window_link=True):
        self.root = Path(data_root).resolve()
        if self.root.is_relative_to(REPO): raise ValueError('Use an external data root')
        self.lock = threading.Lock()
        self.window_link = window_link

    def open(self, workbench_url):
        from conversation_service import WorkbenchError
        sys.path.insert(0, str(ENGINE / 'scripts')) if str(ENGINE / 'scripts') not in sys.path else None
        from desktop_instance import restore
        from desktop_assistant import local_workbench_url
        local_workbench_url(workbench_url)
        with self.lock:
            if os.name != 'nt' or importlib.util.find_spec('webview') is None:
                raise WorkbenchError('悬浮窗需要 Windows 桌面环境，请安装 requirements-note.txt。')
            if restore(self.root):
                return {'ok': True, 'reused': True, 'message': '会话笺已显示。'}
            interpreter = Path(sys.executable).with_name('pythonw.exe')
            if not interpreter.is_file():
                raise WorkbenchError('没有可用的桌面运行环境。')
            command = [str(interpreter), '-B', str(ENGINE / 'desktop_entry.py'),
                       '--data-root', str(self.root), '--workbench-url', workbench_url]
            if self.window_link: command.append('--window-link')
            try:
                pointer = json.loads((self.root / 'author-workbench' / 'latest.json').read_text(encoding='utf-8'))
                export = Path(pointer['export_dir']).resolve()
                if export.is_relative_to(self.root / 'author-workbench' / 'captures') and (export / 'summary.json').is_file():
                    command.extend(['--export', str(export)])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            env = {**os.environ, 'WECHAT_ANALYZER_DATA': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'}
            child = subprocess.Popen(command, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if restore(self.root):
                    return {'ok': True, 'reused': False, 'message': '会话笺已打开。点小窗上的“笺”可以打开功能菜单。'}
                if child.poll() is not None: break
                time.sleep(.2)
            raise WorkbenchError('悬浮窗尚未就绪，请检查桌面运行环境后重试。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--workbench-url', default='http://127.0.0.1:8711/conversations')
    args = parser.parse_args()
    os.environ['WECHAT_ANALYZER_DATA'] = str(args.data_root.resolve())
    try:
        result = NoteLauncher(args.data_root).open(args.workbench_url)
    except Exception:
        result = {'ok': False, 'message': '会话笺未就绪，请从工作台重试并查看提示。'}
    if sys.stdout is not None: print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
