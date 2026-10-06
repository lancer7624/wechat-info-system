"""Disposable UI preview with fabricated conversations and a fake model."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18711)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = args.data_root.resolve()
    if root.exists() or root.is_relative_to(repo):
        parser.error('Preview needs a new external data directory')
    os.environ['WECHAT_ANALYZER_DATA'] = str(root)
    sys.path.insert(0, str(repo / 'tests'))
    from test_conversation_workbench import fixture, FakeClient
    from conversation_service import ConversationService, save_json
    from chat_workbench import create_server
    service = ConversationService(root, client_factory=lambda _: FakeClient())
    service.snapshot = fixture(service.root)
    service.capture = lambda sid, _account, day: fixture(service.root, sid, day)
    service.capture_batch = lambda sids, _account, day: [fixture(service.root, sid, day) for sid in sids]
    save_json(service.root / 'latest.json', {'export_dir': service.snapshot['export_dir']})
    save_json(root / 'private' / 'ai.json', {'base_url': 'http://127.0.0.1:12345/v1',
        'api_key': 'synthetic-only', 'model': '合成测试模型', 'allow_chat_upload': True})
    from launch_conversation_note import NoteLauncher
    server = create_server(service, args.port, note_launcher=NoteLauncher(root, window_link=False).open)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
