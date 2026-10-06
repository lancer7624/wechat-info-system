"""Python note adapter for the bounded C# window-link service."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading

from native_bridge import NativeAdapter, PipePeer


def verify_package(root):
    root = Path(root).resolve()
    path = root / 'WeChatWindowLink.exe'
    manifest = json.loads((root / 'build-manifest.json').read_text(encoding='utf-8'))
    files = manifest.get('files')
    if not isinstance(files, dict) or path.name not in files:
        raise ValueError('Missing window-link executable')
    for relative, digest in files.items():
        if not isinstance(relative, str) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid window-link manifest')
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Invalid window-link path')
        file = (root / relative).resolve()
        if not file.is_relative_to(root) or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError('Window-link package changed')
    return path


def helper_path(data_root):
    root = (Path(data_root) / 'window-link').resolve()
    current = root / 'current.json'
    if current.exists():
        pointer = json.loads(current.read_text(encoding='utf-8'))
        release = pointer.get('release')
        if not isinstance(release, str) or not re.fullmatch('[0-9a-f]{32}', release):
            raise ValueError('Invalid window-link release')
        package = (root / 'releases' / release).resolve()
        if not package.is_relative_to(root) or hashlib.sha256((package / 'build-manifest.json').read_bytes()).hexdigest() != pointer.get('manifest_sha256'):
            raise ValueError('Window-link release changed')
        return verify_package(package)
    return verify_package(root)


class IsolatedInteraction(NativeAdapter):
    def __init__(self, executable):
        self.executable = Path(executable)
        self.closed = threading.Event()
        self.child = self.wire = self.reader_thread = None
        self.call_lock = threading.Lock()
        self.tripped = False
        super().__init__(self)

    def _start(self):
        self.child = subprocess.Popen([str(self.executable), '--native-service', str(os.getpid())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding='utf-8', errors='strict', creationflags=subprocess.CREATE_NO_WINDOW)
        self.wire = PipePeer(self.child.stdout, self.child.stdin, outgoing_prefix='h:', incoming_prefix='p:')
        self.wire.handler = lambda method, args: (self.callback(args[0], record=method == 'native_record')
            if method in ('native_guard', 'native_record') and len(args) == 1 else False)
        self.reader_thread = threading.Thread(target=self.wire.run, name='window-link-reader', daemon=True)
        self.reader_thread.start()

    def call(self, method, args, timeout=15):
        with self.call_lock:
            if self.tripped or self.closed.is_set(): raise RuntimeError('Window link stopped')
            try:
                if self.child is None: self._start()
                return self.wire.call(method, args, timeout=min(timeout, 12 if method in ('native.place', 'native.send') else 4))
            except Exception:
                # One failure ends the provider for this session; no background
                # restart, late write, or automatic retry of an uncertain send.
                self.tripped = True
                self._stop_child()
                raise

    def sample(self, sources):
        value = super().sample(sources)
        if self.tripped:
            value.update(reason='窗口读取未及时响应，已停止本次联动；草稿保留，请检查微信。')
        return value

    def _stop_child(self):
        if self.wire: self.wire.close()
        if self.child:
            if self.child.poll() is None: self.child.kill()
            try: self.child.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
            if self.reader_thread: self.reader_thread.join(timeout=3)
            for stream in (self.child.stdin, self.child.stdout):
                try: stream.close()
                except (OSError, ValueError): pass
            self.child = None

    def close(self):
        self.closed.set()
        self._stop_child()
        super().close()
