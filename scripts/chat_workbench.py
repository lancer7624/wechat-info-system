"""Serve the author's board and today's conversation tools on loopback only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit, parse_qs
import webbrowser

REPO = Path(__file__).resolve().parents[1]
MAX_BODY = 16384


def create_server(service, port=8711, *, note_launcher=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value, mime='application/json; charset=utf-8'):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized_host(self):
            return (self.headers.get('Host') == self.server.host_header
                    and self.headers.get('Sec-Fetch-Site') != 'cross-site')

        def do_GET(self):
            if not self.authorized_host():
                return self.respond(403, {'error': '请通过本机工作台地址打开。'})
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/conversations': ('conversations.html', 'text/html; charset=utf-8'),
                      '/conversation.js': ('conversation.js', 'text/javascript; charset=utf-8'),
                      '/conversation.css': ('conversation.css', 'text/css; charset=utf-8')}
            try:
                if path in assets:
                    name, mime = assets[path]
                    return self.respond(200, (REPO / 'kanban' / name).read_bytes(), mime)
                if path == '/api/state':
                    return self.respond(200, {**service.state(), 'token': token})
                if path == '/api/result':
                    query = parse_qs(urlsplit(self.path).query)
                    result = service.result(query.get('source_id', [''])[0], query.get('account', [''])[0])
                    return self.respond(200, {'result': result})
                if path == '/data.json':
                    return self.respond(200, service.board())
                if path == '/status':
                    return self.respond(200, {'recording': False, 'supported': False})
                return self.respond(404, {'error': '没有这个入口。'})
            except (ValueError, OSError, KeyError):
                return self.respond(400, {'error': '读取结果未通过核对，请更新今天后重试。'})

        def do_POST(self):
            if (not self.authorized_host() or self.headers.get('Origin') != self.server.origin
                    or not secrets.compare_digest(self.headers.get('X-Workbench-Token', ''), token)):
                return self.respond(403, {'error': '页面已失效，请刷新后重试。'})
            if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                return self.respond(400, {'error': '请求格式无效。'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    return self.respond(413, {'error': '提交内容过长。'})
                self.connection.settimeout(10)
                args = json.loads(self.rfile.read(size))
                if not isinstance(args, dict):
                    raise ValueError
                path = urlsplit(self.path).path
                if path in ('/api/refresh', '/api/summary', '/api/reply', '/api/batch-summary'):
                    return self.respond(202, service.submit(path.rsplit('/', 1)[-1], args))
                if path == '/api/cancel-batch':
                    return self.respond(200, service.cancel_batch(args.get('job_id')))
                if path == '/api/note' and note_launcher is not None:
                    if args: raise ValueError('Unexpected note arguments')
                    return self.respond(200, note_launcher(self.server.origin + '/conversations'))
                if path == '/api/scene':
                    return self.respond(200, service.set_scene(args.get('source_id'), args.get('account'), args.get('scene')))
                if path == '/review':
                    text = args.get('review_user')
                    if not isinstance(text, str) or len(text) > 8000:
                        raise ValueError
                    from conversation_service import save_json, today
                    save_json(service.root / 'review.json', {'date': today(), 'text': text})
                    return self.respond(200, {'ok': True})
                return self.respond(404, {'error': '没有这个操作。'})
            except Exception as exc:
                from conversation_service import WorkbenchError
                message = str(exc) if isinstance(exc, WorkbenchError) else '请求未完成，请刷新后重试。'
                return self.respond(400, {'error': message})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.host_header = '127.0.0.1:' + str(server.server_port)
    server.origin = 'http://' + server.host_header
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path(os.environ.get('WECHAT_ANALYZER_DATA', Path.home() / '.wechat-info-data')))
    parser.add_argument('--port', type=int, default=8711)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Read today once using the existing account key; no model calls')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Invalid port')
    os.environ['WECHAT_ANALYZER_DATA'] = str(args.data_root.resolve())
    from conversation_service import ConversationService
    from launch_conversation_note import NoteLauncher
    service = ConversationService(args.data_root)
    try:
        server = create_server(service, args.port, note_launcher=NoteLauncher(args.data_root).open)
    except OSError:
        # Open an existing workbench only if its protocol marker matches.
        import urllib.request
        try:
            with urllib.request.urlopen('http://127.0.0.1:' + str(args.port) + '/api/state', timeout=2) as response:
                state = json.load(response)
            if (state.get('app') == 'wechat-author-workbench' and state.get('protocol') == 1
                    and state.get('instance') == service.state()['instance']):
                if not args.no_open:
                    webbrowser.open('http://127.0.0.1:' + str(args.port) + '/conversations')
                return 0
        except Exception:
            pass
        return 1
    if args.refresh:
        service.submit('refresh', {})
    if not args.no_open:
        webbrowser.open(server.origin + '/conversations')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
