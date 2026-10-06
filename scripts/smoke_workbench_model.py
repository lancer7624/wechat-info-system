"""Test the configured model with fabricated messages only; never capture real chats."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(repo):
        parser.error('Use a new output directory outside the checkout')
    sys.path.insert(0, str(repo / 'tests'))
    from test_conversation_workbench import fixture, SID
    from conversation_service import ConversationService, today, save_json, ai_client
    config = ai_client.load_config(args.data_root.resolve() / 'private' / 'ai.json')
    # This permission is restricted to the fabricated fixture in this function.
    # The user's configuration is never changed.
    config = {**config, 'allow_chat_upload': True}
    service = ConversationService(output)
    service.snapshot = fixture(service.root)
    selected = fixture(service.root, SID)
    summary = service._summarize(selected, today(), SID, config)
    reply = service._reply(selected, today(), SID, config, '简短确认收到，不增加承诺', 'work')
    report = {'ok': bool(summary['analysis']['overview'] and reply['reply']),
              'synthetic_messages': True, 'real_chat_used': False,
              'summary_evidence_verified': bool(summary['evidence']),
              'reply_evidence_verified': bool(reply['evidence_uids'])}
    save_json(output / 'report.json', report)
    print(json.dumps(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
