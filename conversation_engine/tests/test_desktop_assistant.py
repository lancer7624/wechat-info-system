"""Desktop boundary tests with synthetic controllers, assets and native calls."""
import http.client
import argparse
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop_assistant as d
import runtime_paths


class AssetTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(Path, 'read_bytes', return_value=b'synthetic asset'):
            self.assets = d.AssetServer(Path('/synthetic'))
        self.assets.start()
        self.url = urlsplit(self.assets.url)

    def tearDown(self): self.assets.close()

    def request(self, path, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.url.port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: connection.close()

    def test_loopback_static_whitelist_only(self):
        self.assertEqual(self.assets.server.server_address[0], '127.0.0.1')
        status, headers, body = self.request(self.url.path)
        self.assertEqual((status, body), (200, b'synthetic asset'))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        motion_status, motion_headers, _ = self.request('/' + self.assets.token + '/desktop/card-motion.js')
        self.assertEqual(motion_status, 200)
        self.assertEqual(motion_headers['Content-Type'], 'text/javascript; charset=utf-8')
        for path in ('/', '/private/db_keys.json', self.url.path + '?path=private',
                     '/' + self.assets.token + '/desktop/../private/db_keys.json'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request(self.url.path, 'POST')[0], 405)
        self.assertEqual(self.request(self.url.path, headers={'Host':'unexpected.example'})[0], 403)

    def test_generated_images_use_png_mime_and_only_named_assets_are_reachable(self):
        prefix = '/' + self.assets.token + '/desktop/assets/'
        for name in ('courier-magpie.png', 'jade-bamboo.png', 'ivory-paper.png'):
            status, headers, body = self.request(prefix + name)
            self.assertEqual((status, body), (200, b'synthetic asset'))
            self.assertEqual(headers['Content-Type'], 'image/png')
        for name in ('README.md', 'unreviewed.png', '../private/ai.json'):
            self.assertEqual(self.request(prefix + name)[0], 404)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.controller, self.native, self.window = mock.Mock(), mock.Mock(), mock.Mock()
        self.native.resize.return_value = True
        self.calls = {function.__name__: function for function in
                      d.bridge_functions(self.controller, self.native, self.window, Path('/synthetic-data'))}

    def test_bridge_exposes_only_explicit_operations(self):
        self.assertEqual(set(self.calls), {'get_state', 'select_conversation', 'set_following', 'refresh',
                         'update_analysis', 'summarize_today', 'open_today_summary', 'suggest_reply', 'take_suggestion', 'put_draft', 'grant_consent', 'save_drafts',
                         'collapse', 'expand', 'minimize', 'open_dashboard', 'open_workbench', 'open_image', 'quit', 'set_startup', 'copy_text',
                         'get_style_profile', 'save_style_profile', 'learn_style', 'set_reply_scene',
                         'get_contact_memory', 'save_contact_memory', 'update_memory_item', 'clear_contact_memory',
                         'set_auto_chat', 'pause_auto_chat'})
        self.calls['suggest_reply']('目标', '补充', 'synthetic-source')
        self.controller.suggest_reply.assert_called_once_with('目标', '补充', 'synthetic-source')
        self.controller.refresh.side_effect = RuntimeError('private chat text')
        result = self.calls['refresh']('synthetic-source')
        self.assertFalse(result['ok'])
        self.assertNotIn('private chat text', str(result))

    def test_window_actions_do_not_generate_or_send(self):
        self.calls['collapse']()
        self.native.resize.assert_called_once_with(True)
        self.controller.set_collapsed.assert_called_once_with(True)
        self.controller.suggest_reply.assert_not_called()
        self.assertTrue(self.calls['minimize']()['ok'])
        self.native.minimize.assert_called_once_with()
        self.controller.close.assert_not_called()
        self.window.destroy.assert_not_called()
        self.assertFalse(self.calls['set_following']('true')['ok'])
        self.controller.set_following.assert_not_called()

    def test_startup_is_only_changed_by_explicit_action(self):
        with mock.patch.object(d, 'set_startup') as setter, mock.patch.object(d, 'startup_enabled', return_value=True):
            self.calls['get_state']()
            setter.assert_not_called()
            self.assertTrue(self.calls['set_startup'](True)['ok'])
            setter.assert_called_once_with(True, Path('/synthetic-data'))
            self.controller.set_startup_state.assert_called_once_with(True)

    def test_clipboard_receives_only_explicit_text(self):
        with mock.patch.object(d, 'copy_text') as writer:
            self.assertTrue(self.calls['copy_text']('合成回复')['ok'])
            writer.assert_called_once_with('合成回复', self.native.hwnd)
        for value in (None, '', 'a\x00b', 'a' * 20001):
            with self.assertRaises(ValueError): d.copy_text(value, None)


class RuntimeTests(unittest.TestCase):
    def test_minimized_window_does_not_clamp_or_change_restore_geometry(self):
        native = d.NativeWindow.__new__(d.NativeWindow)
        native.hwnd = 42
        native.user = mock.Mock()
        native.user.IsIconic.return_value = True
        native._geometry()
        native.user.GetWindowRect.assert_not_called()
        native.user.SetWindowPos.assert_not_called()

    def test_compact_geometry_used_for_both_initial_window_and_toggle(self):
        webview = mock.MagicMock()
        controller_module = mock.MagicMock()
        with mock.patch.dict(sys.modules, {'webview': webview, 'assistant_controller': controller_module}), \
             mock.patch.object(d, 'AssetServer'), mock.patch.object(d, 'NativeWindow'), \
             mock.patch.object(d, 'startup_enabled', return_value=False), mock.patch.object(d, 'DesktopInstance'), \
             mock.patch.object(d.os, 'name', 'nt'):
            self.assertEqual(d.main([]), 0)
        options = webview.create_window.call_args.kwargs
        self.assertEqual((options['width'], options['height']), (300, 460))
        self.assertEqual(options['min_size'], (48, 96))
        self.assertFalse(options['focus'])
        self.assertIs(controller_module.AssistantController.call_args.kwargs['window_interaction'], False)
        native = d.NativeWindow.__new__(d.NativeWindow)
        native._dispatch = lambda function: function()
        native._geometry = mock.Mock()
        native.resize(True)
        native.resize(False)
        self.assertEqual(native._geometry.call_args_list, [mock.call(48, 96), mock.call(300, 460)])

    def test_virtual_environment_never_loads_legacy_dependencies(self):
        with mock.patch.object(runtime_paths.sys, 'prefix', 'synthetic-venv'), \
             mock.patch.object(runtime_paths.sys, 'base_prefix', 'synthetic-base'), \
             mock.patch.object(runtime_paths.sys, 'path', []) as path, \
             mock.patch.object(Path, 'is_dir', return_value=True):
            runtime_paths.enable_dependencies()
            self.assertEqual(path, [])

    def test_workbench_links_allow_only_the_fixed_loopback_page(self):
        for value in ('https://example.invalid/conversations', 'http://127.0.0.1:8711/private',
                      'http://user@127.0.0.1:8711/conversations', 'http://127.0.0.1:8711/conversations?command=x'):
            with self.assertRaises(argparse.ArgumentTypeError): d.local_workbench_url(value)
        self.assertEqual(d.local_workbench_url('http://127.0.0.1:8711/conversations'),
                         'http://127.0.0.1:8711/conversations')

    def test_ordinary_cli_restarts_windowed_isolated_python(self):
        root = Path('/synthetic-data').resolve()
        with mock.patch.object(d, 'FROZEN', False), mock.patch.object(d.sys, 'prefix', '/ordinary-python'), \
             mock.patch.object(Path, 'is_file', return_value=True), mock.patch.object(d.subprocess, 'Popen') as popen:
            self.assertEqual(d.launch(['--data-root', str(root)]), 0)
        arguments = popen.call_args.args[0]
        self.assertTrue(arguments[0].endswith('pythonw.exe'))
        self.assertIn('desktop_entry.py', arguments[2])
        self.assertEqual(popen.call_args.kwargs['env']['WECHAT_ANALYZER_DATA'], str(root))


class ClosingTests(unittest.TestCase):
    def test_native_close_flushes_page_and_quit_releases_guard(self):
        window, controller, native = mock.Mock(), mock.Mock(), mock.Mock()
        guard = d.CloseGuard(window)
        self.assertTrue(guard.closing())  # Hidden/failed initialization is unblocked.
        guard.ready = True
        with mock.patch.object(d.threading, 'Thread') as thread, mock.patch.object(d.threading, 'Timer'):
            self.assertFalse(guard.closing())
            self.assertFalse(guard.closing())
            self.assertEqual(thread.call_count, 1)
            thread.call_args.kwargs['target']()
        window.evaluate_js.assert_called_once_with("window.dispatchEvent(new Event('assistant-request-close'))")
        controller.close.assert_not_called()
        bridge = {function.__name__: function for function in
                  d.bridge_functions(controller, native, window, close_guard=guard)}
        self.assertTrue(bridge['quit']()['ok'])
        self.assertTrue(guard.closing())
        controller.close.assert_called_once()
        window.destroy.assert_called_once()
        guard.timer.cancel.assert_called_once()

    def test_failed_page_close_warns_once_and_next_close_can_exit(self):
        window = mock.Mock()
        window.evaluate_js.side_effect = RuntimeError('private page details')
        guard = d.CloseGuard(window)
        guard.ready = True
        with mock.patch.object(d, '_show_error') as error:
            guard._request()
            guard._failed()
        self.assertEqual(error.call_count, 1)
        self.assertNotIn('private page details', str(error.call_args))
        self.assertTrue(guard.closing())


if __name__ == '__main__': unittest.main()
