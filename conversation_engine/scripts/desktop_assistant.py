"""A restricted local WebView2 host for the conversation assistant."""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from runtime_paths import DATA_ROOT, FROZEN, PROJECT_ROOT
from desktop_instance import DesktopInstance


APP_NAME = 'WeChatConversationAssistant'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
EXPANDED_SIZE = (300, 460)
COLLAPSED_SIZE = (48, 96)


def parser():
    result = argparse.ArgumentParser(description='打开本地会话笺；普通回复按需生成，指定私聊可启用自动聊天。')
    result.add_argument('--export', type=Path, help='使用已有完整导出作为初始数据')
    result.add_argument('--config', type=Path, help='模型配置路径')
    result.add_argument('--data-root', type=Path, help='外部个人数据目录')
    result.add_argument('--window-link', '--experimental-interaction', dest='window_link', action='store_true', help='使用独立窗口联动组件识别会话和填入草稿')
    result.add_argument('--workbench-url', type=local_workbench_url, help='本机当天会话工作台地址')
    return result


def local_workbench_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.path != '/conversations'
            or parsed.query or parsed.fragment):
        raise argparse.ArgumentTypeError('工作台必须是本机当天会话页面。')
    return value


def launch(argv=None):
    """Route the ordinary CLI to its isolated, windowed desktop environment."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    root = args.data_root.expanduser().resolve() if args.data_root else DATA_ROOT
    environment = root / 'desktop-env'
    if not FROZEN and Path(sys.prefix).resolve() != environment.resolve():
        python = environment / 'Scripts' / 'pythonw.exe'
        if not python.is_file():
            print('缺少独立桌面环境，请先安装 requirements-desktop.txt。')
            return 1
        env = os.environ.copy()
        env['WECHAT_ANALYZER_DATA'] = str(root)
        subprocess.Popen([str(python), '-B', str(PROJECT_ROOT / 'desktop_entry.py'), *argv],
                         cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
        print('会话笺已启动。')
        return 0
    return main(argv)


class AssetServer:
    """Only explicit bundled static assets are reachable; never serve a directory."""
    def __init__(self, asset_root=PROJECT_ROOT):
        root = Path(asset_root)
        self.token = secrets.token_urlsafe(24)
        prefix = '/' + self.token
        files = {
            prefix + '/desktop/index.html': (root / 'desktop' / 'index.html', 'text/html; charset=utf-8'),
            prefix + '/desktop/style.css': (root / 'desktop' / 'style.css', 'text/css; charset=utf-8'),
            prefix + '/desktop/app.js': (root / 'desktop' / 'app.js', 'text/javascript; charset=utf-8'),
            prefix + '/desktop/card-motion.js': (root / 'desktop' / 'card-motion.js', 'text/javascript; charset=utf-8'),
            prefix + '/desktop/assets/courier-magpie.png': (root / 'desktop/assets/courier-magpie.png', 'image/png'),
            prefix + '/desktop/assets/jade-bamboo.png': (root / 'desktop/assets/jade-bamboo.png', 'image/png'),
            prefix + '/desktop/assets/ivory-paper.png': (root / 'desktop/assets/ivory-paper.png', 'image/png'),
            prefix + '/dashboard/fonts/NotoSerifSC.woff2': (root / 'dashboard' / 'fonts' / 'NotoSerifSC.woff2', 'font/woff2'),
        }
        # Load only bundled, explicit files; no request is ever mapped to a path.
        assets = {url: (path.read_bytes(), mime) for url, (path, mime) in files.items()}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    self.send_error(403, 'Forbidden')
                    return
                parsed = urlsplit(self.path)
                asset = assets.get(parsed.path) if not parsed.query and not parsed.fragment else None
                if asset is None:
                    self.send_error(404, 'Not found')
                    return
                body, mime = asset
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'none'; form-action 'none'; frame-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self): self.send_error(405, 'Method not allowed')
            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}{prefix}/desktop/index.html'
        self.thread = threading.Thread(target=self.server.serve_forever, name='assistant-assets', daemon=True)

    def start(self): self.thread.start()

    def close(self):
        if self.thread.is_alive(): self.server.shutdown()
        self.server.server_close()


def startup_command(data_root=DATA_ROOT):
    if FROZEN:
        args = [sys.executable]
    else:
        args = [str(Path(data_root) / 'desktop-env' / 'Scripts' / 'pythonw.exe'),
                '-B', str(PROJECT_ROOT / 'desktop_entry.py')]
    return subprocess.list2cmdline([*args, '--data-root', str(Path(data_root).resolve())])


def startup_enabled(data_root=DATA_ROOT):
    if os.name != 'nt': return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, APP_NAME)
        return kind == winreg.REG_SZ and value == startup_command(data_root)
    except OSError:
        return False


def set_startup(enabled, data_root=DATA_ROOT):
    if type(enabled) is not bool or os.name != 'nt':
        raise ValueError('invalid startup setting')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command(data_root))
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass


def copy_text(text, hwnd):
    """Write Unicode text only; there is deliberately no clipboard read API."""
    if not isinstance(text, str) or not text or len(text) > 20_000 or '\x00' in text:
        raise ValueError('invalid clipboard text')
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.OpenClipboard.argtypes, user.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user.EmptyClipboard.argtypes, user.EmptyClipboard.restype = [], wintypes.BOOL
    user.CloseClipboard.argtypes, user.CloseClipboard.restype = [], wintypes.BOOL
    user.SetClipboardData.argtypes, user.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
    kernel.GlobalAlloc.argtypes, kernel.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [wintypes.HGLOBAL], wintypes.HGLOBAL
    content = text.encode('utf-16-le') + b'\x00\x00'
    memory = kernel.GlobalAlloc(0x0002, len(content))
    if not memory: raise RuntimeError('clipboard unavailable')
    transferred = opened = False
    try:
        pointer = kernel.GlobalLock(memory)
        if not pointer: raise RuntimeError('clipboard unavailable')
        try: ctypes.memmove(pointer, content, len(content))
        finally: kernel.GlobalUnlock(memory)
        if not user.OpenClipboard(hwnd): raise RuntimeError('clipboard unavailable')
        opened = True
        if not user.EmptyClipboard() or not user.SetClipboardData(13, memory):
            raise RuntimeError('clipboard unavailable')
        transferred = True
    finally:
        if opened: user.CloseClipboard()
        if not transferred: kernel.GlobalFree(memory)


class NativeWindow:
    """Own-window geometry only; all movement explicitly avoids activation."""
    def __init__(self, window, allowed_url):
        self.window, self.allowed_url, self.hwnd = window, allowed_url, None
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetWindowRect.restype = wintypes.BOOL
        self.user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.SetWindowPos.restype = wintypes.BOOL
        self.user.ShowWindow.argtypes, self.user.ShowWindow.restype = [wintypes.HWND, ctypes.c_int], wintypes.BOOL
        self.user.IsIconic.argtypes, self.user.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
        self.user.MonitorFromWindow.argtypes, self.user.MonitorFromWindow.restype = [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
        self.user.GetMonitorInfoW.argtypes, self.user.GetMonitorInfoW.restype = [wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL
        self.user.GetDpiForWindow.argtypes, self.user.GetDpiForWindow.restype = [wintypes.HWND], wintypes.UINT
        self.user.GetWindowLongPtrW.argtypes, self.user.GetWindowLongPtrW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
        self.user.SetWindowLongPtrW.argtypes, self.user.SetWindowLongPtrW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        self.user.SetThreadDpiAwarenessContext.argtypes, self.user.SetThreadDpiAwarenessContext.restype = [wintypes.HANDLE], wintypes.HANDLE
        self._navigation_handlers = []
        self.follow_rect = None

    def _dispatch(self, function, wait=True):
        from System import Action
        done, succeeded = threading.Event(), []
        def call():
            try: function(); succeeded.append(True)
            except Exception: succeeded.append(False)
            finally: done.set()
        self.window.native.BeginInvoke(Action(call))
        if not wait: return True
        return done.wait(3) and bool(succeeded and succeeded[0])

    def prepare(self):
        def native_prepare():
            if self._navigation_handlers: return
            self.hwnd = int(self.window.native.Handle.ToInt64())
            view = self.window.native.webview
            def navigation(_sender, event):
                if str(event.Uri).split('#', 1)[0] != self.allowed_url: event.Cancel = True
            def new_window(_sender, event): event.Handled = True
            view.NavigationStarting += navigation
            view.CoreWebView2.NewWindowRequested -= self.window.native.browser.on_new_window_request
            view.CoreWebView2.NewWindowRequested += new_window
            self._navigation_handlers = [navigation, new_window]
            view.CoreWebView2.Settings.AreDevToolsEnabled = False
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = False
            # focus=False prevents the hidden constructor's initial Show from
            # activating. Remove that persistent style before actual use.
            self.window.focus = True
            self.window.native.ShowInTaskbar = True
            style = self.user.GetWindowLongPtrW(self.hwnd, -20)
            self.user.SetWindowLongPtrW(self.hwnd, -20, style & ~0x08000000)
            self._geometry(*EXPANDED_SIZE, initial=True)
            self.window.native.Opacity = 1.0
            self.user.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE.
        return self._dispatch(native_prepare)

    def _geometry(self, width=None, height=None, initial=False):
        if not self.hwnd or self.user.IsIconic(self.hwnd): return
        class MonitorInfo(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                        ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
        previous = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            rect = wintypes.RECT()
            info = MonitorInfo(); info.size = ctypes.sizeof(info)
            monitor = self.user.MonitorFromWindow(self.hwnd, 2)
            if not self.user.GetWindowRect(self.hwnd, ctypes.byref(rect)) or not self.user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                raise RuntimeError('window bounds unavailable')
            dpi = (self.user.GetDpiForWindow(self.hwnd) or 96) / 96
            w = round(width * dpi) if width is not None else rect.right - rect.left
            h = round(height * dpi) if height is not None else rect.bottom - rect.top
            work = info.work
            w, h = min(w, work.right - work.left), min(h, work.bottom - work.top)
            x = work.right - w - round(20 * dpi) if initial else rect.left
            y = work.top + round(60 * dpi) if initial else rect.top
            x, y = max(work.left, min(x, work.right - w)), max(work.top, min(y, work.bottom - h))
            if (x, y, w, h) != (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top):
                if not self.user.SetWindowPos(self.hwnd, None, x, y, w, h, 0x0004 | 0x0010):
                    raise RuntimeError('window resize unavailable')
        finally:
            if previous: self.user.SetThreadDpiAwarenessContext(previous)

    def resize(self, collapsed):
        return self._dispatch(lambda: self._geometry(*(COLLAPSED_SIZE if collapsed else EXPANDED_SIZE)))

    def minimize(self):
        return self._dispatch(self.window.minimize)

    def clamp(self, *_args):
        if self.hwnd: self._dispatch(self._geometry, wait=False)

    def follow(self, rect):
        if (not isinstance(rect, list) or len(rect) != 4 or
                not all(isinstance(n, (float, int)) for n in rect) or rect == self.follow_rect or not self.hwnd):
            return
        self.follow_rect = rect[:]
        def move():
            if self.user.IsIconic(self.hwnd): return
            own = wintypes.RECT()
            if not self.user.GetWindowRect(self.hwnd, ctypes.byref(own)): return
            width = own.right - own.left
            x = round(rect[2] + 8)
            # The final clamp uses the current display's work area and DPI.
            screen_width = self.user.GetSystemMetrics(0)
            if x + width > screen_width: x = round(rect[0] - width - 8)
            if x < 0: x = round(rect[2] - width - 12)
            self.user.SetWindowPos(self.hwnd, None, x, round(rect[1] + 60), 0, 0, 0x0001 | 0x0004 | 0x0010)
            self._geometry()
        self._dispatch(move, wait=False)


class CloseGuard:
    """Let the page flush draft writes before an ordinary native close."""
    def __init__(self, window):
        self.window = window
        self.ready = self.allow_close = self.pending = self.failed = False
        self.timer = None
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            self.allow_close = True
            if self.timer is not None: self.timer.cancel()

    def _failed(self):
        with self.lock:
            if self.allow_close or self.failed: return
            self.failed = True
        _show_error('退出尚未完成，草稿可能尚未保存。请检查保存提示；若再次关闭窗口，将直接退出。')

    def _request(self):
        try:
            self.window.evaluate_js("window.dispatchEvent(new Event('assistant-request-close'))")
        except Exception:
            self._failed()

    def closing(self):
        with self.lock:
            # An initialization failure must always be able to destroy its hidden
            # window. The guard is armed only after the real page is ready.
            if not self.ready or self.allow_close: return True
            if self.failed:
                self.allow_close = True
                return True
            if self.pending: return False
            self.pending = True
            self.timer = threading.Timer(10, self._failed)
            self.timer.daemon = True
            self.timer.start()
        # The closing callback blocks the UI thread until it returns. Invoking
        # JavaScript synchronously here would deadlock WebView2's GUI dispatch.
        threading.Thread(target=self._request, name='assistant-close', daemon=True).start()
        return False


def bridge_functions(controller, native, window, data_root=DATA_ROOT, close_guard=None, workbench_url=None):
    """Return explicit closures; controller/window objects are never a js_api."""
    functions = []
    def expose(name, operation):
        def call(*args):
            try: return operation(*args)
            except Exception: return {'ok': False, 'message': '操作暂未完成，请稍后重试。'}
        call.__name__ = name
        functions.append(call)
    def get_state(since_revision=None):
        value = controller.get_state(since_revision)
        if isinstance(value, dict) and not value.get('unchanged'):
            value = {**value, 'workbench_available': bool(workbench_url)}
            if value.get('binding', {}).get('mode') == 'auto': native.follow(value.get('window_rect'))
        return value
    expose('get_state', get_state)
    def open_workbench():
        if not workbench_url: return {'ok': False, 'message': '请先启动微信信息工作台。'}
        webbrowser.open(local_workbench_url(workbench_url))
        return {'ok': True}
    expose('open_workbench', open_workbench)
    for name in ('select_conversation', 'refresh', 'update_analysis', 'summarize_today', 'open_today_summary', 'suggest_reply',
                 'take_suggestion', 'put_draft', 'grant_consent', 'save_drafts', 'open_dashboard', 'open_image',
                 'get_style_profile', 'save_style_profile', 'learn_style', 'set_reply_scene',
                 'get_contact_memory', 'save_contact_memory', 'update_memory_item', 'clear_contact_memory',
                 'set_auto_chat', 'pause_auto_chat'):
        expose(name, getattr(controller, name))
    def following(enabled):
        if type(enabled) is not bool: raise ValueError
        return controller.set_following(enabled)
    expose('set_following', following)
    def collapsed(value):
        if not native.resize(value): return {'ok': False, 'message': '暂时无法调整窗口，请重试。'}
        return controller.set_collapsed(value)
    expose('collapse', lambda: collapsed(True))
    expose('expand', lambda: collapsed(False))
    def minimize():
        if not native.minimize(): return {'ok': False, 'message': '暂时无法最小化，请重试。'}
        return {'ok': True}
    expose('minimize', minimize)
    def startup(enabled):
        set_startup(enabled, data_root)
        controller.set_startup_state(startup_enabled(data_root))
        return {'ok': True}
    expose('set_startup', startup)
    def copy(value):
        copy_text(value, native.hwnd)
        return {'ok': True}
    expose('copy_text', copy)
    def quit_app():
        if close_guard is not None: close_guard.allow()
        controller.close()
        window.destroy()
        return {'ok': True}
    expose('quit', quit_app)
    return functions


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    if os.name != 'nt':
        print('桌面助手目前仅支持 Windows。')
        return 1
    root = args.data_root.expanduser().resolve() if args.data_root else DATA_ROOT
    if root != DATA_ROOT:
        print('请通过 desktop_entry.py 或 wechat.py assistant 指定数据目录。')
        return 1
    assets = controller = instance = interaction = None
    try:
        instance = DesktopInstance(root)
        if not instance.owner:
            return 0 if instance.restore_existing() else 1
        import webview
        from assistant_controller import AssistantController
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['ALLOW_DOWNLOADS'] = False
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
        if args.window_link:
            from isolated_interaction import IsolatedInteraction, helper_path
            interaction = IsolatedInteraction(helper_path(root))
        controller = AssistantController(export_dir=args.export, config_path=args.config, data_root=root,
            window_interaction=bool(interaction), automatic_chat=False,
            native_factory=(lambda: (interaction,) * 3) if interaction else None)
        controller.set_startup_state(startup_enabled(root))
        assets = AssetServer(); assets.start()
        window = webview.create_window('会话笺 · 微信助手', assets.url, js_api=None,
                                       width=EXPANDED_SIZE[0], height=EXPANDED_SIZE[1], min_size=COLLAPSED_SIZE,
                                       frameless=True, easy_drag=False, on_top=True,
                                       hidden=True, focus=False, resizable=False,
                                       background_color='#fbf8f0', text_select=True)
        native = NativeWindow(window, assets.url)
        close_guard = CloseGuard(window)
        window.expose(*bridge_functions(controller, native, window, root, close_guard, args.workbench_url))
        def loaded():
            if native.prepare():
                close_guard.ready = True
                instance.ready(lambda: native._dispatch(lambda: (window.restore(), window.show())))
            else:
                close_guard.allow()
                _show_error('会话笺窗口初始化失败，请退出后重试。')
                window.destroy()
        window.events.loaded += loaded
        window.events.moved += native.clamp
        window.events.restored += native.clamp
        window.events.closing += close_guard.closing
        window.events.closed += controller.close
        controller.start()
        webview.start(gui='edgechromium', debug=False, http_server=False, private_mode=True,
                      storage_path=str(root / 'assistant' / 'webview'))
        return 0
    except Exception:
        # Never put model errors, data paths, or chat-bearing tracebacks in a GUI.
        if not FROZEN and sys.stderr is not None:
            print('会话笺未能启动，请检查桌面环境与 WebView2。', file=sys.stderr)
        _show_error('会话笺未能启动。请检查 WebView2 与独立桌面环境。')
        return 1
    finally:
        if controller is not None: controller.close()
        if assets is not None: assets.close()
        if instance is not None: instance.close()
        if interaction is not None: interaction.close()


def _show_error(message):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    user.MessageBoxW(None, message, '会话笺', 0x10)


if __name__ == '__main__':
    raise SystemExit(launch())
