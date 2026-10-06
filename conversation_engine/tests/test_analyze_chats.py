import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
import analyze_chats as a
import dashboard


SID_A = 'C_' + hashlib.md5(b'synthetic_a').hexdigest()[:8]
SID_B = 'C_' + hashlib.md5(b'synthetic_b').hexdigest()[:8]
UID_A, UID_DONE, UID_B = ('W_' + value * 32 for value in ('a', 'b', 'c'))
STAMP = '2026-09-30T12:00:00+08:00'


def result(uid, status='待跟进'):
    return {
        'overview': '合成任务的进展。',
        'highlights': [{'title': '任务进展', 'text': '这是合成消息中的进展。',
                        'kind': 'fact', 'evidence_uids': [uid]}],
        'followups': [{'title': '核对任务', 'owner': '我', 'status': status, 'due_text': '未知',
                       'next_action': '按原文确认进度。', 'evidence_uids': [uid]}],
    }


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, max_tokens=4096):
        self.calls.append((messages, max_tokens))
        if not self.replies:
            raise AssertionError('Unexpected model call')
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


class AnalyzeChatsTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('WECHAT_TEST_TMP', tempfile.gettempdir())).resolve()
        self.folder = base / ('wechat-analyze-test-' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.export = self.folder / 'export'
        self.export.mkdir()
        self.output = self.folder / 'output'
        self.cache = self.folder / 'cache'
        self.config = {'base_url': 'http://127.0.0.1:12345/v1', 'model': 'synthetic-model',
                       'api_key': 'synthetic-key-never-printed', 'allow_chat_upload': True,
                       'timeout_seconds': 30}
        self.rows = [
            {'uid': UID_A, 'source_id': SID_A, 'time': '2026-09-30T09:00:00+08:00',
             'sender': '测试甲', 'is_self': False, 'kind': 'text', 'text': '请核对这个合成任务。'},
            {'uid': UID_DONE, 'source_id': SID_A, 'time': '2026-09-30T10:00:00+08:00',
             'sender': '我', 'is_self': True, 'kind': 'text', 'text': '已经核对完成。'},
        ]
        self.extra_empty_source = False
        self.write_export()

    def write_export(self):
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows).encode('utf-8')
        (self.export / 'messages.jsonl').write_bytes(raw)
        sids = sorted({row['source_id'] for row in self.rows} | ({SID_B} if self.extra_empty_source else set()))
        self.summary = {
            'schema_version': 2, 'status': 'complete', 'source_scope': None,
            'snapshot_at': STAMP, 'start': '2026-09-30T00:00:00+08:00', 'end': STAMP,
            'messages_sha256': hashlib.sha256(raw).hexdigest(), 'exported_messages': len(self.rows),
            'counts': {'raw_rows': len(self.rows), 'all_messages': len(self.rows), 'duplicate_messages': 0,
                       'text': len(self.rows), 'reply': 0, 'file': 0, 'link': 0,
                       'excluded_nontext': 0, 'excluded_unparsed': 0, 'excluded_empty_or_decode_failed': 0,
                       'redacted_messages': 0, 'unresolved_senders': 0},
            'by_day': {'2026-09-30': len(self.rows)}, 'sources': [],
            'missing_message_shards': [], 'main_database_only': True,
            'delta': {'new': len(self.rows), 'updated': 0, 'unchanged': 0, 'context_messages': len(self.rows)},
        }
        for sid in sids:
            rows = [row for row in self.rows if row['source_id'] == sid]
            self.summary['sources'].append({'id': sid, 'name': '合成联系人', 'kind': '私聊',
                                             'total_messages': len(rows), 'exported_messages': len(rows),
                                             'self_messages': sum(row['is_self'] for row in rows)})
        (self.export / 'summary.json').write_text(json.dumps(self.summary, ensure_ascii=False), encoding='utf-8')

    def run_analysis(self, client, **kwargs):
        return a.analyze_export(self.export, kwargs.pop('config', self.config), self.output, self.cache,
                                client=client, **kwargs)

    def read_analysis(self):
        return json.loads((self.output / 'analysis.json').read_text(encoding='utf-8'))

    def make_long_conversation(self):
        first, last = self.rows
        filler = [
            {'uid': 'W_' + hashlib.md5(f'fictional_filler_{i}'.encode()).hexdigest(),
             'source_id': SID_A, 'time': f'2026-09-30T09:0{i + 1}:00+08:00',
             'sender': '测试甲', 'is_self': False, 'kind': 'text', 'text': '合成内容。' * 1000}
            for i in range(5)
        ]
        self.rows = [first, *filler, last]
        self.write_export()

    def test_publishes_valid_dashboard_analysis_and_only_safe_fields_are_sent(self):
        self.rows[0]['database'] = 'internal-private-path'
        self.write_export()
        client = FakeClient([result(UID_DONE, '已完成')])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            info = self.run_analysis(client)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(info['messages'], 2)
        self.assertEqual(info['sources'], 1)
        self.assertEqual(info['analyzed_sources'], 1)
        self.assertEqual(info['cached_sources'], 0)
        self.assertEqual(info['requests'], 1)
        self.assertEqual(info['analysis_path'], str(self.output / 'analysis.json'))
        analysis = self.read_analysis()
        self.assertEqual(analysis['export_sha256'], self.summary['messages_sha256'])
        self.assertEqual(analysis['followups'][0]['source_id'], SID_A)
        dashboard.build_payload(self.export, Path(info['analysis_path']))
        self.assertNotIn('internal-private-path', json.dumps(client.calls))
        self.assertNotIn(self.config['api_key'], json.dumps(client.calls))

    def test_unchanged_conversation_is_reused_without_authorization_or_calls(self):
        self.run_analysis(FakeClient([result(UID_A)]))
        client = FakeClient([])
        info = self.run_analysis(client, config={**self.config, 'allow_chat_upload': False}, max_requests=0)
        self.assertEqual(info['requests'], 0)
        self.assertEqual(info['cached_sources'], 1)
        self.assertEqual(info['analyzed_sources'], 0)
        self.assertEqual(client.calls, [])

    def test_message_and_model_and_prompt_changes_invalidate_cache(self):
        self.run_analysis(FakeClient([result(UID_A)]))
        self.rows[0]['text'] += '这是新的更改。'
        self.write_export()
        self.assertEqual(self.run_analysis(FakeClient([result(UID_A)]))['requests'], 1)
        changed = {**self.config, 'model': 'new-synthetic-model'}
        self.assertEqual(self.run_analysis(FakeClient([result(UID_A)]), config=changed)['requests'], 1)
        with patch.object(a, 'PROMPT_VERSION', 'synthetic-new-version'):
            self.assertEqual(self.run_analysis(FakeClient([result(UID_A)]))['requests'], 1)
        with patch.object(a, 'SYSTEM_PROMPT', a.SYSTEM_PROMPT + '\n新增合成规则。'):
            self.assertEqual(self.run_analysis(FakeClient([result(UID_A)]))['requests'], 1)

    def test_new_calls_require_explicit_boolean_authorization_even_with_injected_client(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                client = FakeClient([])
                with self.assertRaisesRegex(a.AnalysisError, '尚未允许'):
                    self.run_analysis(client, config={**self.config, 'allow_chat_upload': value})
                self.assertEqual(client.calls, [])
        self.assertFalse((self.output / 'analysis.json').exists())

    def test_unknown_and_cross_conversation_evidence_are_rejected(self):
        self.rows.append({'uid': UID_B, 'source_id': SID_B, 'time': STAMP, 'sender': '测试乙',
                          'is_self': False, 'kind': 'text', 'text': '另一个合成会话。'})
        self.write_export()
        first_sid = self.summary['sources'][0]['id']
        other_uid = UID_B if first_sid == SID_A else UID_A
        for uid in ('W_' + 'd' * 32, other_uid):
            with self.subTest(uid=uid), self.assertRaisesRegex(a.AnalysisError, '证据 UID'):
                self.run_analysis(FakeClient([result(uid)]))
        self.assertFalse((self.output / 'analysis.json').exists())

    def test_invalid_json_duplicate_keys_extra_fields_and_empty_evidence_are_rejected(self):
        empty = result(UID_A)
        empty['followups'][0]['evidence_uids'] = []
        extra = {**result(UID_A), 'source_id': SID_A}
        bad = ['not JSON', '{"overview":"a","overview":"b","highlights":[],"followups":[]}',
               '```json\n{}', extra, empty]
        for reply in bad:
            with self.subTest(reply_type=type(reply).__name__), self.assertRaises(a.AnalysisError):
                self.run_analysis(FakeClient([reply]))
        self.assertFalse((self.output / 'analysis.json').exists())

    def test_exact_json_code_fence_is_supported(self):
        raw = '```json\n' + json.dumps(result(UID_A), ensure_ascii=False) + '\n```'
        self.assertEqual(self.run_analysis(FakeClient([raw]))['requests'], 1)

    def test_later_completion_is_reconciled_after_batching(self):
        self.make_long_conversation()
        finished = result(UID_DONE, '已完成')
        finished['followups'][0]['evidence_uids'] = [UID_A, UID_DONE]
        client = FakeClient([result(UID_A), result(UID_DONE, '已完成'), finished])
        info = self.run_analysis(client)
        self.assertEqual(info['requests'], 3)
        bodies = [json.loads(call[0][1]['content']) for call in client.calls]
        self.assertEqual([body['mode'] for body in bodies], ['extract', 'extract', 'reconcile'])
        self.assertTrue(bodies[1]['context_messages'])
        self.assertLessEqual(len(bodies[1]['context_messages']), 4)
        self.assertTrue(all(len(a._canonical(body)) <= a.CHAR_BUDGET for body in bodies))
        self.assertEqual({row['uid'] for row in bodies[-1]['evidence_messages']}, {UID_A, UID_DONE})
        self.assertEqual(self.read_analysis()['followups'][0]['status'], '已完成')
        self.assertEqual(len(self.read_analysis()['followups']), 1)

    def test_request_budget_preserves_batch_cache_and_can_resume(self):
        self.make_long_conversation()
        with self.assertRaisesRegex(a.AnalysisError, '请求上限'):
            self.run_analysis(FakeClient([result(UID_A)]), max_requests=1)
        self.assertFalse((self.output / 'analysis.json').exists())
        resumed = FakeClient([result(UID_DONE, '已完成'), result(UID_DONE, '已完成')])
        info = self.run_analysis(resumed, max_requests=2)
        self.assertEqual(info['requests'], 2)

    def test_unavailable_overlap_is_explicit_and_new_messages_are_never_skipped(self):
        rows = [dict(self.rows[0], text='合' * 23200), dict(self.rows[1], text='成' * 900)]
        batches = a._batches({'source_id': SID_A, 'name': '合成联系人', 'kind': '私聊'}, rows)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[1]['context_messages'], [])
        self.assertIn('字符预算', batches[1]['context_note'])
        self.assertEqual([row['uid'] for body in batches for row in body['messages']], [UID_A, UID_DONE])

    def test_long_cited_messages_merge_via_explicit_index_without_truncation(self):
        self.rows[0]['text'] = '合' * 13000
        self.rows[1]['text'] = '成' * 13000
        self.write_export()
        client = FakeClient([result(UID_A), result(UID_DONE, '已完成'), result(UID_DONE, '已完成')])
        self.assertEqual(self.run_analysis(client)['requests'], 3)
        bodies = [json.loads(call[0][1]['content']) for call in client.calls]
        self.assertEqual(len(bodies[0]['messages'][0]['text']), 13000)
        self.assertEqual(len(bodies[1]['messages'][0]['text']), 13000)
        self.assertNotIn('evidence_messages', bodies[2])
        self.assertEqual({item['uid'] for item in bodies[2]['evidence_index']}, {UID_A, UID_DONE})
        self.assertIn('不新增事实', bodies[2]['instruction'])
        self.assertLessEqual(len(a._canonical(bodies[2])), a.CHAR_BUDGET)

    def test_http_attempts_including_retries_use_a_single_absolute_limit(self):
        class CountingClient(FakeClient):
            def __init__(self):
                super().__init__([result(UID_A)])
                self.http_requests = 7
                self.request_limit = None

            def complete(self, messages, max_tokens=4096):
                for _ in range(2):
                    if self.http_requests >= self.request_limit:
                        raise ValueError('synthetic HTTP limit')
                    self.http_requests += 1
                return super().complete(messages, max_tokens)

        client = CountingClient()
        info = self.run_analysis(client, max_requests=2)
        self.assertEqual(info['requests'], 2)
        self.assertEqual(client.request_limit, 9)
        self.rows[0]['text'] += '新内容使缓存失效。'
        self.write_export()
        limited = CountingClient()
        with self.assertRaisesRegex(ValueError, 'HTTP limit'):
            self.run_analysis(limited, max_requests=1)
        self.assertEqual(limited.http_requests, 8)
        self.assertEqual(limited.request_limit, 8)

    def test_global_overview_is_limited_to_five_conversation_excerpts(self):
        self.rows = [
            {'uid': 'W_' + hashlib.md5(f'synthetic_uid_{i}'.encode()).hexdigest(),
             'source_id': 'C_' + hashlib.md5(f'synthetic_source_{i}'.encode()).hexdigest()[:8],
             'time': STAMP, 'sender': '合成人员', 'is_self': False, 'kind': 'text', 'text': '合成事项。'}
            for i in range(8)
        ]
        self.write_export()
        replies = [result(next(row['uid'] for row in self.rows if row['source_id'] == source['id']))
                   for source in self.summary['sources']]
        self.run_analysis(FakeClient(replies))
        overview = self.read_analysis()['overview']
        self.assertIn('8 个会话', overview)
        self.assertIn('其余 3 个会话可按联系人查看', overview)
        self.assertEqual(overview.count('合成联系人：'), 5)
        self.assertLess(len(overview), 1500)

    def test_overview_uses_complete_short_text_without_cutting_long_qualifications(self):
        value = result(UID_A)
        value['overview'] = '需要保留的说明' * 100 + '，并不表示已经完成。'
        value['highlights'][0]['text'] = '长文本' * 100 + '，目前尚未确认。'
        summary = a._overview_excerpt(value)
        self.assertIn('完整内容见该会话', summary)
        self.assertNotIn('需要保留的说明', summary)
        value['overview'] = '对方尚未确认，不能视为已完成。'
        self.assertEqual(a._overview_excerpt(value), value['overview'])

    def test_overlong_single_message_is_not_truncated_or_sent(self):
        self.rows[0]['text'] = '合' * a.CHAR_BUDGET
        self.write_export()
        client = FakeClient([])
        with self.assertRaisesRegex(a.AnalysisError, '单条消息'):
            self.run_analysis(client)
        self.assertEqual(client.calls, [])

    def test_failure_keeps_previous_published_analysis(self):
        self.run_analysis(FakeClient([result(UID_A)]))
        previous = (self.output / 'analysis.json').read_bytes()
        self.rows[0]['text'] += '需要重新分析。'
        self.write_export()
        with self.assertRaises(a.AnalysisError):
            self.run_analysis(FakeClient(['bad JSON']))
        self.assertEqual((self.output / 'analysis.json').read_bytes(), previous)

    def test_empty_conversation_and_empty_export_need_no_model(self):
        self.rows = []
        self.extra_empty_source = True
        self.write_export()
        info = self.run_analysis(FakeClient([]), config={**self.config, 'allow_chat_upload': False})
        self.assertEqual(info['sources'], 1)
        self.assertEqual(info['messages'], 0)
        self.assertEqual(info['requests'], 0)
        self.assertEqual(info['analyzed_sources'] + info['cached_sources'], 0)
        self.assertEqual(self.read_analysis()['followups'], [])
        self.extra_empty_source = False
        self.write_export()
        self.assertEqual(self.run_analysis(FakeClient([]))['sources'], 0)

    def test_atomic_publication_failure_preserves_previous_file(self):
        self.run_analysis(FakeClient([result(UID_A)]))
        previous = (self.output / 'analysis.json').read_bytes()
        original_replace = os.replace

        def replace(src, dst):
            if Path(dst) == self.output / 'analysis.json':
                raise OSError('synthetic publication failure')
            return original_replace(src, dst)

        with patch.object(a.os, 'replace', side_effect=replace), self.assertRaises(OSError):
            self.run_analysis(FakeClient([]))
        self.assertEqual((self.output / 'analysis.json').read_bytes(), previous)
        self.assertFalse(list(self.output.glob('.analysis-*')))


if __name__ == '__main__':
    unittest.main()
