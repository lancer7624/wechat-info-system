"""Synthetic note lifecycle and fail-closed desktop behavior. No WeChat UI."""
import http.client
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'conversation_engine' / 'scripts'))
import desktop_instance
from assistant_controller import AssistantController


class NoteTests(unittest.TestCase):
    def test_paused_controller_never_creates_window_tools_or_ticks_old_optins(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = mock.Mock(side_effect=AssertionError('No native provider'))
            controller = AssistantController(data_root=directory, executor=mock.Mock(_processes={}),
                config_loader=lambda _: {}, native_factory=factory, window_interaction=False)
            try:
                controller.state['sources'] = [{'id': 'C_11111111', 'name': '合成好友', 'kind': '私聊'}]
                with mock.patch.object(controller.stopped, 'wait', side_effect=[False, False, True]), \
                     mock.patch.object(controller, '_check_updates') as sync, \
                     mock.patch.object(controller, '_place_pending_draft') as place, \
                     mock.patch.object(controller.auto_chat, 'tick') as tick:
                    controller._watch()
                self.assertEqual(sync.call_count, 2)
                factory.assert_not_called(); place.assert_not_called(); tick.assert_not_called()
                self.assertFalse(controller.set_following(True)['ok'])
                self.assertFalse(controller.set_auto_chat(True, 'C_11111111', 'synthetic')['ok'])
                self.assertFalse(controller._queue_placement('合成草稿')['ok'])
                with mock.patch.object(controller.auto_chat, 'public', return_value={
                        'enabled': True, 'supported': True, 'active_contacts': 1, 'status': 'ready'}):
                    state = controller.get_state()
                self.assertFalse(state['automatic']['enabled'])
                self.assertFalse(state['capabilities']['window_interaction'])
            finally: controller.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows instance lock')
    def test_single_instance_restore_checks_token_host_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            first = desktop_instance.DesktopInstance(directory)
            second = None
            try:
                self.assertTrue(first.owner)
                show = mock.Mock(return_value=True)
                first.ready(show)
                second = desktop_instance.DesktopInstance(directory)
                self.assertFalse(second.owner)
                self.assertTrue(second.restore_existing())
                show.assert_called_once_with()
                for headers in ({'X-Note-Token': 'wrong'},
                                {'X-Note-Token': first.token, 'Origin': 'https://example.invalid'},
                                {'X-Note-Token': first.token, 'Host': 'example.invalid'}):
                    connection = http.client.HTTPConnection('127.0.0.1', first.server.server_port, timeout=3)
                    try:
                        connection.request('POST', '/show', body=b'', headers=headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 403); response.read()
                    finally: connection.close()
                self.assertEqual(show.call_count, 1)
            finally:
                if second: second.close()
                first.close()
            self.assertFalse(desktop_instance.restore(directory))


if __name__ == '__main__': unittest.main()
