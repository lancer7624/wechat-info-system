"""Audit the exact files to publish. Never print matching values or private config."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

BINARY_HASHES = {'conversation_engine/desktop/assets/courier-magpie.png': '27771a550a5e74a5e74faa2e7c4ae42045888a72124d85d508b8bf4c7ea56f8a', 'conversation_engine/desktop/assets/jade-bamboo.png': '28aa6e00218dbc7d6c76ac729a495774e177be7603186ae59f92f3e4658506bb', 'conversation_engine/desktop/assets/ivory-paper.png': 'ce2b930eb6cb74d912dc212159c0a341d76621354f8e1233d118433ebc1eac4f', 'conversation_engine/dashboard/fonts/NotoSerifSC.woff2': '81e6beb6443e63b7dde1524e5fc76ec28dad5e015ba472e137c3ecc0166e9c3d'}
BLOCKED_PARTS = {
    'private', 'captures', 'snapshots', 'author-workbench', 'author-workbench-qa',
    'assistant', 'assistant-csharp', 'wechat-chat-analyzer-data', 'node_modules',
    '.venv', 'venv', '__pycache__', 'bin', 'obj', 'desktop-build', 'desktop-dist',
    'dotnet-build', 'dotnet-dist', 'toolchains',
}
BLOCKED_NAMES = {
    'config.json', 'ai.json', 'db_keys.json', 'db_key.json', 'image_key.json',
    'image_keys.json', 'desktop-settings.json', 'python_path.txt', 'data.json',
    'latest.json', 'runtime.json', 'messages.json', 'chat_history.json',
}
BLOCKED_EXTENSIONS = {
    '.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.jsonl', '.log', '.exe',
    '.dll', '.pdb', '.zip', '.7z', '.dmp', '.pem', '.key', '.pfx', '.pyc',
}
RULES = {
    'private_absolute_path': re.compile(
        r'(?i)(?:[A-Z]:[\\/]+Users[\\/]+(?!Public\b|YOURNAME\b|<)[^\\/\s"\']+'
        r'|[A-Z]:[\\/]+(?:[^\\/\s"\']+[\\/]+){1,5}xwechat_files[\\/]'
        r'|/(?:Users|home)/[^/\s"\']+)'),
    'credential': re.compile(
        r'(?i)\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}'
        r'|github_pat_[a-z0-9_]{20,}|AIza[a-z0-9_-]{30,})'
        r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'wechat_identifier': re.compile(
        r'(?i)\bwxid_(?!YOURWXID\b|EXAMPLE\b|SYNTHETIC\b|TEST\b)[a-z0-9_-]+'
        r'|\bgh_[a-z0-9]{8,}|\b[0-9]{6,}@chatroom'),
    'webhook_or_url_credential': re.compile(
        r'https?://[^\s"\']*(?:webhook/[a-zA-Z0-9-]{8,}|[?&](?:token|api_key|access_token)=[a-zA-Z0-9_-]{8,})'),
}
ASSIGNMENT = re.compile(
    r"""(?i)(?:api_key|password|secret|token|webhook|db_key|aes_key)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']""")
SYNTHETIC_VALUES = {
    'fictional-key', 'must-not-leak', 'synthetic-key-never-printed', 'synthetic-only',
    'synthetic-token', 'synthetic-key',
}
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
PRIVATE_FIELD_NAMES = {
    'api_key', 'password', 'secret', 'token', 'webhook', 'aes_key', 'db_key',
    'account_id', 'db_root', 'data_root', 'base_url',
}


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.DEVNULL)


def paths(raw):
    return {p for p in raw.decode('utf-8').split('\0') if p}


def entries(repo, *, staged=False, base=None, revision='HEAD'):
    if staged:
        names = paths(git(repo, 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'))
        for name in sorted(names):
            meta = git(repo, 'ls-files', '--stage', '-z', '--', name).decode().split()
            yield name, meta[0], git(repo, 'show', ':' + name)
    elif base:
        # Require commit IDs/refs, not arbitrary git options or object expressions.
        start = git(repo, 'rev-parse', '--verify', base + '^{commit}').decode().strip()
        end = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
        names = paths(git(repo, 'diff', '--name-only', '--diff-filter=ACMR', '-z', start, end))
        for name in sorted(names):
            mode = git(repo, 'ls-tree', end, '--', name).decode().split()[0]
            yield name, mode, git(repo, 'show', end + ':' + name)
    else:
        names = paths(git(repo, 'diff', 'HEAD', '--name-only', '--diff-filter=ACMR', '-z'))
        names |= paths(git(repo, 'ls-files', '--others', '--exclude-standard', '-z'))
        for name in sorted(names):
            path = repo / name
            if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
                yield name, '120000', b''
            elif path.is_file():
                yield name, '100644', path.read_bytes()


def known_private_values(config_files, repo):
    result = set()

    def walk(value, key=''):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k).lower())
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and len(value) >= 8:
            if (key in PRIVATE_FIELD_NAMES or re.fullmatch(r'[a-fA-F0-9]{32,}', value)
                    or value.startswith(('wxid_', 'sk-', 'ghp_'))
                    or re.match(r'^[A-Za-z]:[\\/]', value)):
                result.add(value)
                result.add(json.dumps(value, ensure_ascii=False)[1:-1])
    for file in config_files:
        if file.resolve().is_relative_to(repo.resolve()):
            raise ValueError('Private config must be outside the source repository')
        walk(json.loads(file.read_text(encoding='utf-8-sig')))
    return {v.encode('utf-8') for v in result}


def scan(name, mode, data, private_values=()):
    issues = []

    def flag(rule, line=None):
        item = {'path': name, 'rule': rule}
        if line is not None:
            item['line'] = line
        issues.append(item)

    path = PurePosixPath(name)
    if mode not in ('100644', '100755'):
        flag('non_regular_file')
        return issues
    if (set(path.parts) & BLOCKED_PARTS or path.name.lower() in BLOCKED_NAMES
            or path.suffix.lower() in BLOCKED_EXTENSIONS
            or path.name.startswith('.env') and path.name != '.env.example'):
        flag('personal_data_or_build_output')
    if any(v in data for v in private_values):
        flag('known_private_value')
    if name in BINARY_HASHES:
        if hashlib.sha256(data).hexdigest() != BINARY_HASHES[name]:
            flag('unreviewed_binary_asset')
        return issues
    try:
        content = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        flag('unreviewed_binary')
        return issues
    public_license = (path.name.startswith(('LICENSE', 'NOTICE', 'OFL'))
                      or '/docs/licenses/' in '/' + name)
    for n, line in enumerate(content.splitlines(), 1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                flag(rule, n)
        for match in ASSIGNMENT.finditer(line):
            if match[1] not in SYNTHETIC_VALUES and not match[1].startswith(('REPLACE_', 'YOUR_', '<')):
                flag('assigned_secret_candidate', n)
        if not public_license:
            for address in EMAIL.findall(line):
                if not (address.endswith(('@example.com', '@example.invalid', '@users.noreply.github.com'))
                        or address == 'noreply@github.com'):
                    flag('personal_email_candidate', n)
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--staged', action='store_true')
    group.add_argument('--base')
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--private-json', type=Path, action='append', default=[])
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        private = known_private_values(args.private_json, repo)
        files = list(entries(repo, staged=args.staged, base=args.base, revision=args.revision))
        findings = [issue for name, mode, data in files for issue in scan(name, mode, data, private)]
        report = {
            'ok': bool(files) and not findings,
            'scope': 'staged' if args.staged else 'commit' if args.base else 'working-tree',
            'file_count': len(files),
            'known_private_values_checked': len(private),
            'findings': findings,
            'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                      for name, mode, data in files],
            'limitation': 'Review prose and examples manually; pattern checks do not prove anonymization.',
        }
        if args.report:
            if args.report.exists() or args.report.resolve().is_relative_to(repo):
                raise ValueError('Choose a new report outside the source repository')
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k != 'files'}, ensure_ascii=False, indent=2))
        return 0 if report['ok'] else 1
    except (ValueError, OSError, subprocess.CalledProcessError):
        print(json.dumps({'ok': False, 'error': 'Audit could not verify all inputs; no private values are displayed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
