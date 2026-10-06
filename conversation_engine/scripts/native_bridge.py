"""Bounded, bidirectional IPC. Only the parent host can invoke these operations.

Native guards are short-lived capabilities, kept out of the web UI. The reader
always remains available for replies, even while a controller call is pending.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import threading
import time
import uuid


MAX_FRAME = 16 * 1024 * 1024
PUBLIC_METHODS = frozenset((
    'get_state', 'select_conversation', 'refresh', 'update_analysis',
    'summarize_today', 'open_today_summary', 'suggest_reply', 'take_suggestion',
    'put_draft', 'grant_consent', 'save_drafts', 'open_dashboard', 'open_image',
    'get_style_profile', 'save_style_profile', 'learn_style', 'set_reply_scene',
    'get_contact_memory', 'save_contact_memory', 'update_memory_item',
    'clear_contact_memory', 'set_auto_chat', 'pause_auto_chat', 'set_following',
    'set_collapsed', 'set_startup_state',
))


def decode_frame(line):
    if len(line) > MAX_FRAME or not line.endswith('\n'):
        raise ValueError('Invalid frame size')
    value = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite number')))
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not re.fullmatch(r'[hp]:[0-9]{1,12}', value['id']):
        raise ValueError('Invalid message')
    if 'method' in value:
        if set(value) != {'id', 'method', 'args'} or not isinstance(value['method'], str) or not isinstance(value['args'], list) or len(value['args']) > 12:
            raise ValueError('Invalid request')
    elif set(value) != {'id', 'result'}:
        raise ValueError('Invalid response')
    return value


class PipePeer:
    def __init__(self, reader, writer, *, outgoing_prefix='p:', incoming_prefix='h:'):
        self.reader, self.writer = reader, writer
        if {outgoing_prefix, incoming_prefix} != {'h:', 'p:'}: raise ValueError('Invalid peer role')
        self.outgoing_prefix, self.incoming_prefix = outgoing_prefix, incoming_prefix
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.pending, self.counter = {}, 0
        self.closed = threading.Event()
        self.handler = None
        self.calls = ThreadPoolExecutor(max_workers=8, thread_name_prefix='host-call')
        self.guards = ThreadPoolExecutor(max_workers=2, thread_name_prefix='host-guard')
        self.capacity = threading.BoundedSemaphore(32)

    def write(self, value):
        line = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
        if len(line) > MAX_FRAME:
            raise ValueError('Frame too large')
        with self.write_lock:
            if self.closed.is_set():
                raise BrokenPipeError
            self.writer.write(line)
            self.writer.flush()

    def call(self, method, args, timeout=15):
        event, result = threading.Event(), []
        with self.lock:
            if self.closed.is_set():
                raise BrokenPipeError
            self.counter += 1
            identity = self.outgoing_prefix + str(self.counter)
            self.pending[identity] = (event, result)
        try:
            self.write({'id': identity, 'method': method, 'args': args})
            if not event.wait(timeout) or not result:
                raise TimeoutError('Native host unavailable')
            return result[0]
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def _dispatch(self, value):
        try:
            result = self.handler(value['method'], value['args'])
        except Exception:
            result = {'ok': False, 'error': '操作未完成，请稍后重试。'}
        try:
            self.write({'id': value['id'], 'result': result})
        except (OSError, ValueError):
            self.close()
        finally:
            self.capacity.release()

    def run(self):
        try:
            while not self.closed.is_set():
                line = self.reader.readline(MAX_FRAME + 1)
                if not line:
                    break
                value = decode_frame(line)
                if 'method' in value:
                    if not value['id'].startswith(self.incoming_prefix) or not self.capacity.acquire(blocking=False):
                        raise ValueError('Too many requests')
                    executor = self.guards if value['method'] in ('native_guard', 'native_record') else self.calls
                    executor.submit(self._dispatch, value)
                else:
                    with self.lock:
                        entry = self.pending.get(value['id'])
                        if entry:
                            entry[1].append(value['result'])
                            entry[0].set()
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            self.close()

    def close(self):
        self.closed.set()
        with self.lock:
            for event, _ in self.pending.values():
                event.set()
        self.calls.shutdown(wait=False, cancel_futures=True)
        self.guards.shutdown(wait=False, cancel_futures=True)


class NativeAdapter:
    def __init__(self, peer):
        self.peer, self.last_wechat = peer, None
        self.lock, self.tokens = threading.Lock(), {}

    def callback(self, token, record=False):
        with self.lock:
            entry = self.tokens.get(token) if isinstance(token, str) else None
            if not entry or time.monotonic() > entry['deadline'] or self.peer.closed.is_set():
                return False
            if record:
                if entry['recorded'] or entry['record'] is None:
                    return False
                entry['recorded'] = True
            callback = entry['record'] if record else entry['guard']
        try:
            return callback() is True
        except Exception:
            return False

    def sample(self, sources):
        try:
            result = self.peer.call('native.sample', [sources])
            key = result.get('window_key')
            self.last_wechat = tuple(key) if isinstance(key, list) and len(key) == 2 and all(type(n) is int and n > 0 for n in key) else None
            return result['tracking']
        except Exception:
            self.last_wechat = None
            return {'status': 'unsupported', 'source_id': None, 'reason': '会话识别暂不可用，请手动选择。'}

    def available(self, window_key):
        try:
            return self.peer.call('native.available', [window_key], timeout=3) is True
        except Exception:
            return False

    def _action(self, method, text, sid, sources, window_key, guard, record=None):
        token = uuid.uuid4().hex
        with self.lock:
            self.tokens[token] = {'guard': guard, 'record': record, 'deadline': time.monotonic() + 14, 'recorded': False}
        try:
            result = self.peer.call(method, [text, sid, sources, window_key, token])
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('Invalid native result')
            return result
        except Exception:
            return {'ok': False, 'invoked': method == 'native.send',
                    'error': '窗口交互结果未确认，请检查微信；不会自动重试。'}
        finally:
            with self.lock:
                self.tokens.pop(token, None)

    def place(self, text, sid, sources, window_key, still_current):
        return self._action('native.place', text, sid, sources, window_key, still_current)

    def send(self, text, sid, sources, window_key, still_current, record_attempt):
        return self._action('native.send', text, sid, sources, window_key, still_current, record_attempt)

    def close(self):
        with self.lock:
            self.tokens.clear()


def dispatch(controller, native, method, args):
    if method in ('native_guard', 'native_record') and len(args) == 1:
        return native.callback(args[0], record=method == 'native_record')
    if method == 'ping' and not args:
        return {'ok': True, 'protocol': 1}
    if method not in PUBLIC_METHODS:
        raise ValueError('Unknown method')
    if method in ('set_following', 'set_collapsed', 'set_startup_state'):
        if len(args) != 1 or type(args[0]) is not bool:
            raise ValueError('Expected bool')
    return getattr(controller, method)(*args)


def probe_worker():
    """Synthetic package/spawn check, accessible only with --probe."""
    import os
    import sqlite3
    import zstandard
    from Crypto.Cipher import AES
    from PIL import Image
    # The normal export worker adds the library path when loading export_recent.
    # Do the same for this deliberately isolated package check.
    from runtime_paths import PROJECT_ROOT
    sys.path.insert(0, str(PROJECT_ROOT / 'lib'))
    import db_crypto
    print('offline child protocol check', flush=True)
    return {'ok': True, 'pid': os.getpid(), 'stdout_is_stderr': sys.stdout is sys.stderr}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--probe', action='store_true', help='Empty offline state; no WeChat, model, or private configuration access')
    parser.add_argument('--experimental-interaction', action='store_true',
                        help='Enable unverified native window interaction for development; disabled in probe mode')
    parser.add_argument('--task-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    from native_process_pool import NativeProcessPool, serve
    if args.task_worker:
        return serve(probe=args.probe)
    peer = PipePeer(sys.stdin, sys.stdout)
    # Protocol output is never mixed with incidental library diagnostics.
    sys.stdout = sys.stderr
    from assistant_controller import AssistantController, read_json
    from pathlib import Path
    native = NativeAdapter(peer)
    options = {'data_root': args.data_root, 'state_directory': 'assistant-csharp',
               'native_factory': lambda: (native, native, native),
               'window_interaction': args.experimental_interaction and not args.probe,
               'executor': NativeProcessPool(args.data_root, probe=args.probe)}
    if args.probe:
        def no_config(_path):
            raise ValueError('Offline probe')
        options['config_loader'] = no_config
    else:
        native_pointer = Path(args.data_root) / 'assistant-csharp' / 'latest.json'
        if not native_pointer.exists():
            # Only reuse the same-account export pointer; opt-ins and drafts are
            # separate, and no automatic-chat settings are copied.
            options['export_dir'] = read_json(Path(args.data_root) / 'assistant' / 'latest.json', {}).get('export_dir')
    controller = AssistantController(**options)
    def handle(method, values):
        if args.probe and method == 'probe_worker' and not values:
            try:
                return controller.pool.submit(probe_worker).result(timeout=15)
            except Exception as exc:
                return {'ok': False, 'error_type': type(exc).__name__}
        return dispatch(controller, native, method, values)
    peer.handler = handle
    if not args.probe:
        controller.start()
    try:
        peer.run()
    finally:
        native.close()
        controller.close()
    return 0
