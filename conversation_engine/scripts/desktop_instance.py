"""Single instance and authenticated restore for our own note window only."""
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.request


def instance_id(root):
    return hashlib.sha256(str(Path(root).resolve()).casefold().encode('utf-8')).hexdigest()


def restore(root):
    try:
        value = json.loads((Path(root) / 'assistant' / 'note-runtime.json').read_text(encoding='utf-8'))
        if (value.get('instance') != instance_id(root)
                or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value.get('origin', ''))
                or not re.fullmatch(r'[a-f0-9]{64}', value.get('token', ''))):
            return False
        request = urllib.request.Request(value['origin'] + '/show', data=b'', method='POST',
                                         headers={'X-Note-Token': value['token']})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=4) as response:
            reply = json.loads(response.read(1024))
        return reply.get('app') == 'conversation-note' and reply.get('instance') == value['instance'] and reply.get('ok') is True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class DesktopInstance:
    def __init__(self, root):
        self.root, self.identity = Path(root), instance_id(root)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\ConversationNote-' + self.identity)
        if not self.handle: raise OSError('Note instance unavailable')
        self.owner = ctypes.get_last_error() != 183
        self.server = self.thread = None
        self.marker = self.root / 'assistant' / 'note-runtime.json'
        self.token = secrets.token_hex(32)

    def restore_existing(self):
        for _ in range(30):
            if restore(self.root): return True
            time.sleep(.1)
        return False

    def ready(self, show):
        instance = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                if (self.path != '/show' or self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}'
                        or self.headers.get('Origin') is not None or self.headers.get('Transfer-Encoding')
                        or self.headers.get('Content-Length') != '0'
                        or not secrets.compare_digest(self.headers.get('X-Note-Token', ''), instance.token)):
                    self.send_error(403); return
                body = json.dumps({'app': 'conversation-note', 'instance': instance.identity, 'ok': bool(show())}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='note-restore', daemon=True)
        self.thread.start()
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps({'instance': self.identity, 'token': self.token,
            'origin': f'http://127.0.0.1:{self.server.server_port}'}), encoding='utf-8')

    def close(self):
        if self.server:
            self.server.shutdown(); self.server.server_close()
            self.thread.join(timeout=3)
            self.marker.unlink(missing_ok=True)
            self.server = None
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
