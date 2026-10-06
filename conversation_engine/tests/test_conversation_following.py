"""Retain a displayed conversation across missing samples; no real windows."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from assistant_controller import AssistantController


class FollowingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.controller = AssistantController(
            data_root=self.directory.name, executor=mock.Mock(_processes={}),
            config_loader=lambda _: {}, automatic_chat=False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.controller.close)
        self.controller.account = 'a' * 64
        self.controller.state['account_fingerprint'] = self.controller.account
        self.sources = [
            {'id': 'C_11111111', 'name': '合成会话甲', 'kind': '私聊'},
            {'id': 'C_22222222', 'name': '合成会话乙', 'kind': '群聊'},
        ]
        self.controller.state['sources'] = self.sources

    def sample(self, sid='C_11111111'):
        return {'status': 'matched', 'source_id': sid, 'reason': '',
                'window_rect': [0, 0, 800, 600]}

    def choose(self):
        self.controller.apply_tracking(self.sample())
        record = self.controller._record()
        record['context'] = {'messages': [], 'context_hash': 'synthetic-context'}
        record['drafts'] = {'short': '合成待编辑草稿', 'full': ''}
        self.controller._publish_record()

    def test_focus_loss_and_missing_title_preserve_context_draft_and_inflight_task(self):
        self.choose()
        c = self.controller
        context, drafts = copy.deepcopy(c.state['context']), copy.deepcopy(c.state['drafts'])
        epoch = c.epoch
        task = object()
        c.ai_request = task
        c.state['status']['ai'] = 'running'
        with mock.patch.object(c, '_load_context') as load:
            for status in ('inactive', 'title_unavailable', 'unmatched', 'ambiguous', 'unsupported'):
                with self.subTest(status=status):
                    c.apply_tracking({'status': status, 'reason': '合成暂时不可用'})
                    self.assertEqual(c.state['binding']['source_id'], self.sources[0]['id'])
                    self.assertEqual(c.state['binding']['status'], 'retained')
                    self.assertEqual(c.state['context'], context)
                    self.assertEqual(c.state['drafts'], drafts)
                    self.assertEqual(c.epoch, epoch)
                    self.assertIs(c.ai_request, task)
                    self.assertEqual(c.state['status']['ai'], 'running')
                    self.assertIsNone(c.state['window_rect'])
            c.apply_tracking(self.sample())
            self.assertEqual(c.state['binding']['status'], 'matched')
            self.assertEqual(c.epoch, epoch)
            load.assert_not_called()

    def test_only_a_known_unique_new_conversation_switches(self):
        self.choose()
        c = self.controller
        epoch = c.epoch
        with mock.patch.object(c, '_load_context') as load:
            c.apply_tracking(self.sample('C_33333333'))
            self.assertEqual(c.epoch, epoch)
            self.assertEqual(c.state['binding']['source_id'], self.sources[0]['id'])
            c.apply_tracking(self.sample(self.sources[1]['id']))
            self.assertEqual(c.state['binding']['source_id'], self.sources[1]['id'])
            self.assertEqual(c.epoch, epoch + 1)
            self.assertEqual(c.state['drafts']['short'], '')
            c.apply_tracking(self.sample(self.sources[1]['id']))
            load.assert_called_once_with(self.sources[1]['id'])
        c.apply_tracking(self.sample(self.sources[0]['id']))
        self.assertEqual(c.state['drafts']['short'], '合成待编辑草稿')

    def test_no_previous_conversation_does_not_invent_a_binding(self):
        c = self.controller
        c.apply_tracking({'status': 'title_unavailable'})
        self.assertIsNone(c.state['binding']['source_id'])
        c.apply_tracking(self.sample('C_33333333'))
        self.assertIsNone(c.state['binding']['source_id'])
        self.assertEqual(c.state['binding']['status'], 'unmatched')

    def test_manual_pin_remains_pinned_and_follow_can_resume(self):
        self.choose()
        c = self.controller
        self.assertTrue(c.set_following(False)['ok'])
        c.apply_tracking(self.sample(self.sources[1]['id']))
        self.assertEqual(c.state['binding']['source_id'], self.sources[0]['id'])
        c.set_following(True)
        c.apply_tracking({'status': 'unmatched'})
        self.assertEqual(c.state['binding']['source_id'], self.sources[0]['id'])
        c.apply_tracking(self.sample(self.sources[1]['id']))
        self.assertEqual(c.state['binding']['source_id'], self.sources[1]['id'])

    def test_retained_display_never_authorizes_an_old_pending_window_write(self):
        self.choose()
        c = self.controller
        c.native_binding = ((7, 8), self.sources[0]['id'])
        c._queue_placement(c.state['drafts']['short'])
        tracker = mock.Mock(last_wechat=(7, 8))
        tracker.sample.return_value = {'status': 'title_unavailable'}
        writer, sender = mock.Mock(), mock.Mock()
        c.native_factory = lambda: (tracker, writer, sender)
        with mock.patch.object(c.stopped, 'wait', side_effect=[False, True]), \
             mock.patch.object(c, '_check_updates'):
            c._watch()
        self.assertEqual(c.native_epoch, 1)
        self.assertEqual(c.state['binding']['source_id'], self.sources[0]['id'])
        self.assertEqual(c.state['placement']['status'], 'blocked')
        writer.place.assert_not_called()
        sender.send.assert_not_called()

    def test_account_switch_clears_retained_conversation(self):
        self.choose()
        c = self.controller
        c.apply_tracking({'status': 'inactive'})
        c._index_done({'ok': True, 'value': {
            'account_fingerprint': 'b' * 64,
            'sources': self.sources, 'export_dir': 'synthetic-export'}}, load_context=False)
        self.assertIsNone(c.state['binding']['source_id'])
        self.assertEqual(c.state['drafts'], {'short': '', 'full': ''})


if __name__ == '__main__':
    unittest.main()
