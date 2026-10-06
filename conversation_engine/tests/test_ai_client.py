"""Model transport tests use a local fake server, never actual credentials."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ai_client as a


class AIClientTests(unittest.TestCase):
    @contextmanager
    def server(self, payload, status=200):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if status == 302:
                    self.send_header('Location', 'http://example.invalid/private')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *args):
                pass
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            yield a.AIClient({'base_url': f'http://127.0.0.1:{srv.server_port}/v1',
                             'api_key': 'fictional-key', 'model': 'fictional-model'}), seen
        finally:
            srv.shutdown()
            thread.join(3)
            srv.server_close()

    @staticmethod
    def response(reason='stop', message=None):
        return {'model': 'reported-test-model', 'choices': [{'finish_reason': reason,
                'message': message or {'content': '{"ok":true}'}}]}

    def test_success_sends_compatible_request_and_tracks_reported_model(self):
        with self.server(self.response()) as (client, seen):
            self.assertEqual(client.complete([{'role': 'user', 'content': '虚构文本'}]), '{"ok":true}')
            self.assertEqual(seen[0]['model'], 'fictional-model')
            self.assertFalse(seen[0]['stream'])
            self.assertEqual(client.last_response_model, 'reported-test-model')

    def test_truncated_and_tool_outputs_are_rejected(self):
        for payload in (self.response('length'), self.response('tool_calls'),
                        self.response(message={'content': '{}', 'tool_calls': [{'name': 'evil'}]})):
            with self.subTest(payload=payload), self.server(payload) as (client, _seen):
                with self.assertRaises(a.AIError):
                    client.complete([])

    def test_redirect_does_not_forward_request(self):
        with self.server({'secret': 'must-not-leak'}, 302) as (client, seen):
            with self.assertRaises(a.AIError) as caught:
                client.complete([])
            self.assertNotIn('must-not-leak', str(caught.exception))
            self.assertEqual(len(seen), 1)

    def test_service_errors_are_sanitized_and_retry_is_bounded(self):
        with self.server({'error': 'private-body'}, 503) as (client, seen), patch.object(a.time, 'sleep'):
            with self.assertRaises(a.AIError) as caught:
                client.complete([])
            self.assertEqual(len(seen), 2)
            self.assertNotIn('private-body', str(caught.exception))

    def test_response_size_limit(self):
        with self.server(self.response(message={'content': 'x' * 200})) as (client, _), patch.object(a, 'MAX_RESPONSE_BYTES', 100):
            with self.assertRaisesRegex(a.AIError, '大小'):
                client.complete([])

    def test_retry_respects_shared_request_limit(self):
        with self.server({'error': 'private-body'}, 503) as (client, seen), patch.object(a.time, 'sleep'):
            client.request_limit = 1
            with self.assertRaisesRegex(a.AIError, '上限'):
                client.complete([])
            self.assertEqual(len(seen), 1)
            self.assertEqual(client.http_requests, 1)

    def test_config_rejects_external_urls_credentials_and_implicit_upload(self):
        base = {'model': 'test', 'api_key': 'fake'}
        for endpoint in ('https://example.com/v1', 'http://127.0.0.1:8000/v1?secret=a',
                         'http://key@localhost:8000/v1', 'http://127.0.0.1/v1', 'http://127.0.0.1:8000/other'):
            with self.subTest(endpoint=endpoint), self.assertRaises(a.AIError):
                a.validate_config({**base, 'base_url': endpoint})
        self.assertFalse(a.validate_config(base)['allow_chat_upload'])
        with self.assertRaises(a.AIError):
            a.validate_config({**base, 'allow_chat_upload': 'true'})

    def test_duplicate_json_and_nan_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                a.strict_json(raw)


if __name__ == '__main__':
    unittest.main()
