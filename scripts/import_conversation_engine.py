"""Import reviewed engine and window-link sources; never copy personal data or build outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = [
    *['scripts/' + name + '.py' for name in (
        'ai_client', 'analyze_chats', 'dashboard', 'export_recent', 'message_content',
        'communication_skills', 'runtime_paths', 'image_media', 'image_codec',
        'desktop_assistant', 'desktop_instance', 'assistant_controller', 'assistant_worker',
        'reply_assistant', 'personal_style', 'contact_memory', 'auto_chat', 'today_summary',
        'information_assistant', 'desktop_probe', 'native_bridge', 'isolated_interaction', 'build_window_link')],
    'lib/db_crypto.py', 'PROVENANCE.md', 'docs/licenses/Pillow.txt',
    *['tests/' + name + '.py' for name in (
        'test_conversation_following', 'test_ai_client', 'test_analyze_chats', 'test_communication_skills', 'test_desktop_assistant', 'test_window_link_package')],
    'desktop_entry.py',
    'native/Directory.Build.props',
    *['native/WindowLink/' + name for name in ('WindowLink.csproj', 'Program.cs', 'packages.lock.json')],
    *['native/ConversationAssistant/' + name + '.cs' for name in
      ('WindowsProvider', 'Interaction', 'NativeService', 'WorkerClient', 'ProcessJob')],
    *['desktop/' + name for name in ('index.html', 'app.js', 'style.css', 'card-motion.js',
        'assets/courier-magpie.png', 'assets/jade-bamboo.png', 'assets/ivory-paper.png', 'assets/README.md')],
    *['dashboard/' + name for name in ('index.html', 'style.css', 'fonts/NotoSerifSC.woff2', 'fonts/OFL.txt', 'fonts/README.md')],
    *['skills/communication/' + name for name in (
        'SKILL.md', 'ORIGIN.md', 'LICENSE.MYNAH', 'LICENSE.Humanizer-zh',
        'LICENSE.anthropic-product-management', 'LICENSE.anthropic-customer-support')],
    'skills/chat-reply/ORIGIN.md', 'skills/chat-reply/LICENSE.goutoujunshi',
    'skills/chat-reply/LICENSE.jev-chat-windows', 'skills/chat-reply/NOTICE.jev-chat-windows',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--complete', action='store_true', help='Add newly whitelisted missing files, only if existing files are unchanged')
    parser.add_argument('--update', action='store_true', help='Update from reviewed source only if the previous imported files still match the recorded manifest')
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / 'conversation_engine'
    source = args.source.resolve()
    if target.exists() and not (args.check or args.complete or args.update):
        parser.error('The engine already exists; use --check to compare without overwriting edits.')
    originals = {name: (source / name).read_bytes() for name in FILES}
    if args.update:
        previous = json.loads((target / 'import-manifest.json').read_text(encoding='utf-8'))
        if any(not (target / name).is_file() or hashlib.sha256((target / name).read_bytes()).hexdigest() != digest
               for name, digest in previous.items()):
            parser.error('Imported files have local edits; no files were overwritten.')
    if args.complete and any((target / name).exists() and (target / name).read_bytes() != raw
                             for name, raw in originals.items()):
        parser.error('Existing engine edits must be reviewed; no files were overwritten.')
    if args.check:
        changed = [name for name, raw in originals.items() if not (target / name).is_file()
                   or (target / name).read_bytes() != raw]
        print(json.dumps({'ok': not changed, 'changed_files': changed}))
        return int(bool(changed))
    for name in FILES:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    manifest = {name: hashlib.sha256(raw).hexdigest() for name, raw in originals.items()}
    (target / 'import-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'files': len(FILES)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
