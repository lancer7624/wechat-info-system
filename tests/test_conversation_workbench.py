"""Synthetic end-to-end conversation and loopback tests; no real account or model."""
from datetime import datetime, timedelta
import contextlib
import hashlib
import http.client
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import conversation_service as c
from chat_workbench import create_server

SID = 'C_11111111'
SECOND = 'C_22222222'


def fixture(root, sid=None, day=None, account='A_synthetic'):
    day = day or c.today()
    folder = Path(root) / 'captures' / uuid.uuid4().hex / 'export'
    folder.mkdir(parents=True)
    sources = [SID, SECOND] if sid is None else [sid]
    rows = []
    for index, source in enumerate(sources):
        for i, text in enumerate(['请核对合成项目进度，今天下午确认。', '已核对完成，请查收。']):
            rows.append({'uid': 'W_' + hashlib.md5((source + str(i)).encode()).hexdigest(), 'source_id': source,
                         'time': day + f'T{9+index:02}:0{i}:00+08:00', 'sender': '本人' if i else '合成联系人',
                         'is_self': bool(i), 'kind': 'text', 'text': text})
    raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode('utf-8')
    (folder / 'messages.jsonl').write_bytes(raw)
    meta = {'schema_version': 2, 'status': 'complete', 'account_id': account, 'source_scope': None,
            'snapshot_at': day + 'T12:00:00+08:00', 'start': day + 'T00:00:00+08:00',
            'end': day + 'T12:00:00+08:00', 'messages_sha256': hashlib.sha256(raw).hexdigest(),
            'exported_messages': len(rows), 'counts': {'all_messages': len(rows), 'text': len(rows),
                'reply': 0, 'file': 0, 'link': 0, 'share': 0, 'excluded_nontext': 0,
                'excluded_unparsed': 0, 'excluded_empty_or_decode_failed': 0},
            'by_day': {day: len(rows)}, 'missing_message_shards': [], 'main_database_only': True,
            'delta': {}, 'sources': [{'id': source, 'name': '合成项目群' if source == SID else '合成好友',
                'kind': '群聊' if source == SID else '私聊', 'total_messages': 2, 'exported_messages': 2,
                'self_messages': 1} for source in sources]}
    c.save_json(folder / 'summary.json', meta)
    return c.load_snapshot(folder)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.bad_evidence = False

    def complete(self, messages, max_tokens=4096):
        self.calls.append(messages)
        body = json.loads(messages[-1]['content'])
        rows = body.get('messages', body.get('evidence_messages', []))
        uid = 'W_' + 'f' * 32 if self.bad_evidence else rows[-1]['uid']
        if '恰好包含 reply' in messages[0]['content']:
            return json.dumps({'reply': '收到，我先看一下。', 'reason': '回应最新合成消息。', 'evidence_uids': [uid]})
        return json.dumps({'overview': '今天讨论了合成项目核对，随后确认完成。',
            'highlights': [{'title': '核对已完成', 'text': '后续消息确认了完成。', 'kind': 'fact', 'evidence_uids': [uid]}],
            'followups': [{'title': '合成项目核对', 'owner': '本人', 'status': '已完成', 'due_text': '今天',
                           'next_action': '保留记录', 'evidence_uids': [uid]}]})


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='author-workbench-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.calls = []
        def capture(sid, account, day):
            self.calls.append((sid, account, day))
            return fixture(self.service.root, sid, day)
        self.service = c.ConversationService(self.root, capture=capture, client_factory=lambda _: self.client)
        self.service.snapshot = fixture(self.service.root)
        self.batch_calls = []
        def capture_batch(sids, account, day):
            self.batch_calls.append((sids, account, day))
            return [fixture(self.service.root, sid, day) for sid in sids]
        self.service.capture_batch = capture_batch
        c.save_json(self.root / 'private' / 'ai.json', {'base_url': 'http://127.0.0.1:12345/v1',
            'api_key': 'synthetic-key', 'model': 'synthetic', 'allow_chat_upload': True})

    def args(self, sid=SID):
        state = self.service.state()
        return {'source_id': sid, 'account': state['account'], 'model_signature': state['model']['signature']}

    def wait(self):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            value = self.service.state()['job']
            if value['status'] != 'running':
                return value
            time.sleep(.01)
        self.fail('Background job did not complete')

    def test_complete_today_summary_reopens_and_overlays_author_board(self):
        self.service.submit('summary', self.args())
        job = self.wait()
        self.assertEqual(job['status'], 'complete', job)
        self.assertEqual(job['result']['count'], 2)
        self.assertEqual(self.calls[0], (SID, self.service.snapshot['account'], c.today()))
        self.assertEqual(self.service.result(SID, self.service.snapshot['account']), job['result'])
        self.assertEqual(len(self.client.calls), 1)
        board = self.service.board()
        self.assertEqual(board['today']['brief']['群聊'][0]['source'], '合成项目群')
        self.assertNotIn('api_key', json.dumps(self.service.state()))
        self.assertNotIn(str(self.root), json.dumps(job['result']))

    def batch_args(self):
        return {**self.args(), 'source_ids': [SID, SECOND]}

    def test_batch_summarizes_selected_conversations_with_independent_evidence(self):
        self.service.submit('batch-summary', self.batch_args())
        job = self.wait()
        self.assertEqual(job['status'], 'complete', job)
        self.assertEqual(job['result']['complete'], 2)
        self.assertEqual(len(self.batch_calls), 1)
        self.assertEqual(len(self.client.calls), 2)
        for sid in (SID, SECOND):
            result = self.service.result(sid, job['account'])
            self.assertTrue(result['evidence'])
            self.assertEqual({row['source_id'] for row in result['evidence'].values()}, {sid})

    def test_batch_rejects_duplicates_unknown_contacts_and_wrong_account(self):
        for args in ({**self.batch_args(), 'source_ids': []},
                     {**self.batch_args(), 'source_ids': [SID, SID]},
                     {**self.batch_args(), 'source_ids': [SID, 'C_ffffffff']},
                     {**self.batch_args(), 'account': 'another-account'}):
            with self.assertRaises(c.WorkbenchError): self.service.submit('batch-summary', args)
        self.assertEqual(self.batch_calls, [])
        self.assertEqual(self.client.calls, [])

    def test_whole_batch_binding_is_checked_before_any_model_request(self):
        self.service.capture_batch = lambda *_: [fixture(self.service.root, SID), fixture(self.service.root, SECOND, account='different')]
        self.service.submit('batch-summary', self.batch_args())
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.client.calls, [])

    def test_one_failed_summary_keeps_the_other_completed_result(self):
        original = self.service._summarize
        def summarize(snapshot, day, sid, config):
            if sid == SID: raise c.WorkbenchError('合成失败')
            return original(snapshot, day, sid, config)
        self.service._summarize = summarize
        self.service.submit('batch-summary', self.batch_args())
        result = self.wait()['result']
        self.assertEqual((result['complete'], result['failed']), (1, 1))
        self.assertIsNotNone(self.service.result(SECOND, self.service.snapshot['account']))

    def test_cancel_finishes_current_conversation_and_skips_remaining_requests(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._summarize
        def summarize(*args):
            entered.set(); release.wait(3)
            return original(*args)
        self.service._summarize = summarize
        response = self.service.submit('batch-summary', self.batch_args())
        try:
            self.assertTrue(entered.wait(3))
            self.service.cancel_batch(response['job_id'])
        finally: release.set()
        result = self.wait()['result']
        self.assertEqual((result['complete'], result['skipped']), (1, 1))
        self.assertEqual(len(self.client.calls), 1)

    def test_batch_capture_reuses_one_database_snapshot(self):
        keys = {'mode': 'per_database_raw', 'db_root': str(self.root / 'synthetic' / 'db_storage'),
                'keys': {'message/message_0.db': '0' * 64}}
        c.save_json(self.root / 'private' / 'db_keys.json', keys)
        def export(_copied, _keys, _days, *, output_dir, requested_sources):
            snap = fixture(self.service.root, requested_sources[0])
            return Path(snap['export_dir']), None
        with mock.patch.object(c.export_recent, 'account_identity', return_value=('', 'A_synthetic')), \
             mock.patch.object(c.export_recent, 'snapshot', return_value=self.root / 'copy') as copy_db, \
             mock.patch.object(c.export_recent, 'export', side_effect=export) as exporter:
            snapshots = self.service._capture_batch([SID, SECOND], self.service.snapshot['account'], c.today())
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(copy_db.call_count, 1)
        self.assertEqual(exporter.call_count, 2)
        self.assertFalse(list(self.service.root.glob('captures/*/snapshots')))

    def test_private_summary_uses_private_board_section(self):
        self.service.submit('summary', self.args(SECOND))
        self.assertEqual(self.wait()['status'], 'complete')
        board = self.service.board()
        self.assertEqual(board['today']['brief']['群聊'], [])
        self.assertEqual(board['today']['brief']['私聊'][0]['source'], '合成好友')

    def test_old_day_is_rejected_before_any_model_call(self):
        yesterday = (datetime.fromisoformat(c.today()) - timedelta(days=1)).date().isoformat()
        self.service.capture = lambda *_: fixture(self.service.root, SID, yesterday)
        self.service.submit('summary', self.args())
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.client.calls, [])

    def test_account_switch_is_rejected_before_any_model_call(self):
        self.service.capture = lambda *_: fixture(self.service.root, SID, account='A_other_synthetic')
        self.service.submit('reply', self.args())
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.client.calls, [])

    def test_wrong_source_capture_is_rejected(self):
        self.service.capture = lambda *_: fixture(self.service.root, SECOND)
        self.service.submit('summary', self.args())
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.client.calls, [])

    def test_model_consent_and_service_change_do_not_inherit_old_optins(self):
        config = c.read_json(self.root / 'private' / 'ai.json')
        config['allow_chat_upload'] = False
        c.save_json(self.root / 'private' / 'ai.json', config)
        with self.assertRaises(c.WorkbenchError):
            self.service.submit('summary', self.args())
        args = {**self.args(), 'consent': True}
        config['model'] = 'different-synthetic-model'
        c.save_json(self.root / 'private' / 'ai.json', config)
        with self.assertRaises(c.WorkbenchError):
            self.service.submit('summary', args)
        self.service.submit('summary', {**self.args(), 'consent': True})
        self.assertEqual(self.wait()['status'], 'complete')
        self.assertFalse(c.read_json(self.root / 'private' / 'ai.json')['allow_chat_upload'])

    def test_manual_scene_persists_for_one_conversation_only(self):
        account = self.service.snapshot['account']
        self.service.set_scene(SID, account, 'customer')
        second = c.ConversationService(self.root)
        self.assertEqual(second.preferences[account + ':' + SID], 'customer')
        self.assertNotIn(account + ':' + SECOND, second.preferences)
        self.service.submit('reply', self.args())
        job = self.wait()
        self.assertEqual(job['status'], 'complete')
        self.assertIn('客户沟通', self.client.calls[0][0]['content'])
        self.assertEqual(job['result']['context_count'], 2)
        self.assertFalse(self.service.state()['automatic_chat'])

    def test_unknown_evidence_never_publishes_a_reply(self):
        self.client.bad_evidence = True
        self.service.submit('reply', self.args())
        job = self.wait()
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('result', job)

    def test_reply_accepts_a_single_json_code_block_but_not_extra_prose(self):
        complete = self.client.complete
        self.client.complete = lambda *a, **k: '```json\n' + complete(*a, **k) + '\n```'
        self.service.submit('reply', self.args())
        self.assertEqual(self.wait()['status'], 'complete')
        self.client.complete = lambda *a, **k: 'Unstructured explanation\n' + complete(*a, **k)
        self.service.submit('reply', self.args())
        self.assertEqual(self.wait()['status'], 'failed')

    def test_modified_saved_evidence_is_rejected(self):
        self.service.submit('summary', self.args())
        self.assertEqual(self.wait()['status'], 'complete')
        saved = next((self.service.root / 'results').rglob('*.json'))
        value = c.read_json(saved)
        Path(value['export_dir'], 'messages.jsonl').write_text('changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.service.result(SID, self.service.snapshot['account'])

    def test_busy_job_cannot_be_replaced_by_another_contact(self):
        gate = threading.Event()
        original = self.service.capture
        self.service.capture = lambda *args: (gate.wait(3), original(*args))[1]
        self.service.submit('refresh', {})
        try:
            with self.assertRaises(c.WorkbenchError):
                self.service.submit('reply', self.args(SECOND))
        finally:
            gate.set()
        self.assertEqual(self.wait()['status'], 'complete')

    def test_startup_and_refresh_never_call_model_or_desktop_tools(self):
        self.service.submit('refresh', {})
        self.assertEqual(self.wait()['status'], 'complete')
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.service.state()['window_interaction'])
        self.assertNotIn('uiautomation', sys.modules)
        self.assertNotIn('conversation_tracker', sys.modules)

    def test_real_exporter_filters_yesterday_and_records_excluded_media(self):
        day = c.today()
        keys = {'mode': 'per_database_raw', 'db_root': str(self.root / 'synthetic_account_f00d' / 'db_storage'),
                'keys': {'contact/contact.db': '0' * 64, 'message/message_0.db': '0' * 64}}
        c.save_json(self.root / 'private' / 'db_keys.json', keys)
        def snapshot(_keys, parent):
            folder = Path(parent) / 'fixture'
            folder.mkdir()
            contact = folder / 'contact.decrypted.db'
            messages = folder / 'message_0.decrypted.db'
            with contextlib.closing(sqlite3.connect(contact)) as connection:
                connection.execute('CREATE TABLE contact(username,remark,nick_name)')
                connection.execute('INSERT INTO contact VALUES(?,?,?)', ('synthetic_peer', '合成好友', ''))
                connection.commit()
            table = 'Msg_' + hashlib.md5(b'synthetic_peer').hexdigest()
            with contextlib.closing(sqlite3.connect(messages)) as connection:
                connection.execute('CREATE TABLE Name2Id(user_name)')
                connection.execute('INSERT INTO Name2Id VALUES(?)', ('synthetic_peer',))
                connection.execute(f'CREATE TABLE "{table}" (local_id,server_id,local_type,real_sender_id,create_time,message_content,compress_content)')
                midnight = int(datetime.fromisoformat(day + 'T00:00:00+08:00').timestamp())
                connection.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)', [
                    (1, 1, 1, 1, midnight - 1, '昨天不能进入总结', None),
                    (2, 2, 1, 1, midnight, '今天的完整消息', None),
                    (3, 3, 3, 1, midnight + 1, '<msg><img/></msg>', None)])
                connection.commit()
            c.save_json(folder / 'snapshot_manifest.json', {'schema_version': 2, 'status': 'complete',
                'db_root': keys['db_root'], 'captured_at': day + 'T12:00:00+08:00',
                'message_shards_at_capture': ['message_0.db'], 'databases': {
                    relative: {'output': path.name, 'quick_check': 'ok', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for relative, path in [('contact/contact.db', contact), ('message/message_0.db', messages)]}})
            return folder
        with mock.patch.object(c.export_recent, 'snapshot', side_effect=snapshot):
            result = self.service._capture(None, None, day)
        self.assertEqual([row['text'] for row in result['rows']], ['今天的完整消息'])
        self.assertEqual(result['meta']['counts']['excluded_nontext'], 1)
        self.assertFalse(list(self.service.root.glob('captures/*/snapshots')))

    def test_http_token_origin_host_and_asset_boundaries(self):
        note_launcher = mock.Mock(return_value={'ok': True})
        server = create_server(self.service, 0, note_launcher=note_launcher)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, value=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                body = None if value is None else json.dumps(value)
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        try:
            status, raw = request('GET', '/api/state')
            self.assertEqual(status, 200)
            state = json.loads(raw)
            headers = {'Content-Type': 'application/json', 'Origin': server.origin,
                       'X-Workbench-Token': state['token']}
            self.assertEqual(request('POST', '/api/scene', {**self.args(), 'scene': 'daily'}, headers)[0], 200)
            self.assertEqual(request('POST', '/api/refresh', {}, {**headers, 'Origin': 'https://example.invalid'})[0], 403)
            self.assertEqual(request('POST', '/api/refresh', {}, {**headers, 'X-Workbench-Token': 'wrong'})[0], 403)
            self.assertEqual(request('GET', '/api/state', headers={'Host': 'example.invalid'})[0], 403)
            self.assertEqual(request('GET', '/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
            for path in ('/../private/ai.json', '/config.json', '/scripts/conversation_service.py'):
                self.assertEqual(request('GET', path)[0], 404)
            self.assertEqual(request('GET', '/conversations')[0], 200)
            self.assertEqual(request('GET', '/')[0], 200)
            self.assertEqual(request('POST', '/api/send', {}, headers)[0], 404)
            self.assertEqual(request('POST', '/api/note', {}, headers)[0], 200)
            note_launcher.assert_called_once_with(server.origin + '/conversations')
            self.assertEqual(request('POST', '/api/note', {'command': 'untrusted'}, headers)[0], 400)
            self.assertEqual(note_launcher.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
