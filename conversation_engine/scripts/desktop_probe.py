"""Offline benchmark of the existing Python desktop shell, with an empty state.

Uses the actual asset server, native window, public bridge, controller and
pywebview settings. Does not start tracking/capture or load personal config.
"""
import argparse
import json
from pathlib import Path
import threading
import time


def main(started):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--probe-output', type=Path, required=True)
    args = parser.parse_args()
    import webview
    from assistant_controller import AssistantController
    from desktop_assistant import AssetServer, NativeWindow, bridge_functions, EXPANDED_SIZE, COLLAPSED_SIZE
    def no_config(_): raise ValueError('Offline benchmark')
    controller = AssistantController(data_root=args.data_root, config_loader=no_config, window_interaction=False)
    assets = AssetServer(); assets.start()
    window = webview.create_window('会话笺 · Python 对照测试', assets.url, js_api=None,
        width=EXPANDED_SIZE[0], height=EXPANDED_SIZE[1], min_size=COLLAPSED_SIZE,
        frameless=True, easy_drag=False, on_top=True, hidden=True, focus=False, resizable=False,
        background_color='#fbf8f0', text_select=True)
    native = NativeWindow(window, assets.url)
    window.expose(*bridge_functions(controller, native, window, args.data_root))
    ready_ms = None
    finished = threading.Event()
    def probe_done(result):
        if finished.is_set(): return
        finished.set()
        time.sleep(1.5)
        args.probe_output.write_text(json.dumps({'ok': bool(result.get('ok')), 'ready_ms': ready_ms,
            'elapsed_ms': (time.perf_counter() - started) * 1000, 'bridge': result,
            'width': EXPANDED_SIZE[0], 'height': EXPANDED_SIZE[1]}, ensure_ascii=False), encoding='utf-8')
        time.sleep(4)
        window.destroy()
    window.expose(probe_done)
    def loaded():
        nonlocal ready_ms
        if not native.prepare():
            args.probe_output.write_text('{"ok":false,"phase":"native-window"}', encoding='utf-8')
            window.destroy(); return
        ready_ms = (time.perf_counter() - started) * 1000
        window.evaluate_js("""(async()=>{
          const api=window.pywebview.api;
          const s=await api.get_state(null); const c=await api.collapse(); const e=await api.expand();
          await api.probe_done({ok:!!s.binding && c.ok && e.ok,bridge:!!api.summarize_today,title:document.title});
        })()""")
    window.events.loaded += loaded
    window.events.closed += controller.close
    webview.settings['ALLOW_FILE_URLS'] = False
    webview.settings['ALLOW_DOWNLOADS'] = False
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    try:
        webview.start(gui='edgechromium', debug=False, http_server=False, private_mode=True,
                      storage_path=str(args.data_root / 'assistant' / 'webview'))
    finally:
        controller.close(); assets.close()
    return 0
