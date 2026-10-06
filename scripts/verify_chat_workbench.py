"""Repeatable offline checks for the author-based conversation workbench."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.report and (args.report.exists() or args.report.resolve().is_relative_to(root)):
        parser.error('Choose a new report outside the source checkout')
    for path in [*root.joinpath('scripts').glob('*.py'), *root.joinpath('conversation_engine').rglob('*.py')]:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=path.name)
    commands = [
        [sys.executable, '-B', '-W', 'error::ResourceWarning', '-m', 'unittest', 'discover', '-s', 'conversation_engine/tests', '-q'],
        [sys.executable, '-B', '-W', 'error::ResourceWarning', '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
        ['node', '--test', 'tests/conversation_frontend.test.mjs'],
    ]
    results = []
    for command in commands:
        result = subprocess.run(command, cwd=root, text=True, encoding='utf-8', errors='replace', capture_output=True)
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        results.append({'suite': command[-2:], 'exit_code': result.returncode})
        if result.returncode:
            break
    report = {'ok': len(results) == len(commands) and all(r['exit_code'] == 0 for r in results),
              'real_chat_used': False, 'desktop_interaction': False, 'checks': results}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
