"""Pickleable local worker functions for the desktop assistant controller."""
from collections import Counter
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import uuid

import dashboard
from ai_client import AIError, strict_json
from analyze_chats import AnalysisError, analyze_export
import reply_assistant
from runtime_paths import DATA_ROOT, KEY_FILE, PROJECT_ROOT


class WorkerError(ValueError):
    """Safe user-facing error without account paths, credentials or chat bodies."""


def _require(condition, message):
    if not condition:
        raise WorkerError(message)


def _source_id(value, optional=False):
    _require((optional and value is None) or
             (isinstance(value, str) and re.fullmatch(r'C_[0-9a-f]{8}', value)), '请选择有效的会话编号。')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _read_summary(export_dir, expected=None):
    value = dashboard.parse_json(dashboard.read_bytes(Path(export_dir) / 'summary.json', '导出摘要'), '导出摘要')
    _require(isinstance(value, dict), '导出摘要无效。')
    if expected is not None:
        _require(value.get('messages_sha256') == expected, '导出读取期间发生变化，请重新加载。')
    account = value.get('account_id')
    _require(isinstance(account, str) and bool(account), '导出缺少账号指纹，不能绑定助手会话。')
    return value


def _fingerprint(account_id):
    return hashlib.sha256(_canonical({'purpose': 'reply-context', 'account_id': account_id}).encode('utf-8')).hexdigest()


def read_sources(export_dir):
    """Return validated source metadata without exposing messages or account IDs."""
    export_dir = Path(export_dir).resolve()
    meta, _messages, _by_uid, digest = dashboard.load_export(export_dir)
    original = _read_summary(export_dir, digest)
    return {'export_dir': str(export_dir), 'account_fingerprint': _fingerprint(original['account_id']),
            **{key: meta[key] for key in ('sources', 'snapshot_at', 'start', 'end', 'exported_messages',
                                         'missing_message_shards', 'main_database_only')}}


def _load_existing_keys():
    try:
        keys = strict_json(KEY_FILE.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise WorkerError('现有微信接入配置无法读取，请检查接入状态。') from None
    _require(isinstance(keys, dict) and keys.get('mode') == 'per_database_raw' and
             isinstance(keys.get('db_root'), str) and bool(keys['db_root']) and
             isinstance(keys.get('keys'), dict) and bool(keys['keys']), '现有微信接入配置无效。')
    _require(all(isinstance(relative, str) and
                 re.fullmatch(r'(?:contact/contact|session/session|message/message_\d+)\.db', relative)
                 for relative in keys['keys']), '现有微信接入配置包含不支持的数据库。')
    root = Path(keys['db_root']).resolve()
    _require(all((root / relative).resolve().is_relative_to(root) for relative in keys['keys']),
             '现有微信接入配置的数据库范围无效。')
    return keys


def _new_run(kind):
    root = Path(DATA_ROOT).resolve()
    _require(not root.is_relative_to(PROJECT_ROOT.resolve()), '助手数据必须保存在源码目录之外。')
    run = root / 'assistant' / kind / uuid.uuid4().hex
    run.mkdir(parents=True)
    return run


def refresh_export(source_id=None, *, days=7):
    """Capture a bounded calendar window using the existing key; no model."""
    _source_id(source_id, optional=True)
    _require(type(days) is int and days in (1, 7), '读取天数无效。')
    keys = _load_existing_keys()
    run = _new_run('captures')
    snapshot_parent = run / 'snapshots'
    snapshot_parent.mkdir()
    try:
        import export_recent as exporter
        with contextlib.redirect_stdout(io.StringIO()):
            snapshot = exporter.snapshot(keys, parent=snapshot_parent)
            export_dir, _ = exporter.export(snapshot, keys, days, output_dir=run / 'export',
                                            requested_sources=[source_id] if source_id else None)
        result = read_sources(export_dir)
        if source_id is not None:
            _require({item['id'] for item in result['sources']} == {source_id}, '刷新结果超出所选会话范围。')
        return result
    except Exception:
        raise WorkerError('刷新微信快照未完成；保留了原导出，请检查接入状态后重试。') from None
    finally:
        # Only this invocation's temporary copies are eligible for cleanup. Resolve
        # again before removal so a substituted link cannot reach the source DBs.
        try:
            resolved = snapshot_parent.resolve()
            _require(resolved == run.resolve() / 'snapshots' and
                     resolved.is_relative_to(run.resolve()) and
                     not resolved.is_relative_to(Path(keys['db_root']).resolve()),
                     '临时快照目录范围变化，未执行清理。')
            if snapshot_parent.exists():
                shutil.rmtree(snapshot_parent)
        except OSError:
            raise WorkerError('临时快照清理未完成；原始微信数据未改动，请检查目录权限。') from None


def _stat_token(path):
    try:
        value = Path(path).stat()
        return {'exists': True, 'bytes': value.st_size, 'mtime_ns': value.st_mtime_ns}
    except FileNotFoundError:
        return {'exists': False}
    except OSError:
        raise WorkerError('无法检查微信数据更新时间，请检查文件访问权限。') from None


def source_signature():
    """Stat configured main databases and the key file; snapshots do not read WAL/SHM."""
    before = _stat_token(KEY_FILE)
    keys = _load_existing_keys()
    account_root = Path(keys['db_root']).resolve()
    records = []
    for relative in sorted(keys['keys']):
        database = account_root / relative
        _require(database.resolve().is_relative_to(account_root), '微信文件范围无效。')
        records.append({'file': relative, 'state': _stat_token(database)})
    after = _stat_token(KEY_FILE)
    _require(before == after, '接入配置正在变化，请稍后重试。')
    return hashlib.sha256(_canonical({'key': after, 'files': records}).encode('utf-8')).hexdigest()


def _project_context(context, parent_meta, parent_summary, run):
    """Publish an analysis-only readable projection, never invented media counts."""
    rows = context['messages']
    raw = ''.join(_canonical(row) + '\n' for row in rows).encode('utf-8')
    counts = {key: 0 for key in dashboard.COUNT_FIELDS}
    counts.update(raw_rows=len(rows), all_messages=len(rows))
    for kind in dashboard.READABLE_KINDS:
        counts[kind] = sum(row['kind'] == kind for row in rows)
    china = timezone(timedelta(hours=8))
    by_day = Counter(datetime.fromisoformat(row['time']).astimezone(china).date().isoformat() for row in rows)
    source = {**context['source'], 'total_messages': len(rows), 'exported_messages': len(rows),
              'self_messages': sum(row['is_self'] for row in rows)}
    original_source = next(item for item in parent_meta['sources'] if item['id'] == source['id'])
    coverage = {'source_total_messages': original_source['total_messages'],
                'source_exported_messages': original_source['exported_messages'],
                'context_messages': len(rows), 'context_total_recent_messages': context['total'],
                'context_truncated': context['truncated'], 'readable_projection_only': True,
                'missing_message_shards': parent_meta['missing_message_shards'],
                'main_database_only': parent_meta['main_database_only']}
    summary = {
        'schema_version': 2, 'status': 'complete', 'analysis_only': True,
        'account_id': parent_summary['account_id'], 'source_scope': None,
        'selected_source_ids': [source['id']], 'context_hash': context['context_hash'],
        'snapshot_at': context['snapshot_at'], 'start': context['start'], 'end': context['end'],
        'messages_sha256': hashlib.sha256(raw).hexdigest(), 'exported_messages': len(rows),
        'counts': counts, 'sources': [source], 'by_day': dict(sorted(by_day.items())),
        'missing_message_shards': parent_meta['missing_message_shards'],
        'main_database_only': parent_meta['main_database_only'],
        'delta': {'new': len(rows), 'updated': 0, 'unchanged': 0, 'context_messages': len(rows)},
        'parent_coverage': coverage,
        'notes': ['仅用于选定会话的最近七天、最多80条可读消息分析；统计不是原始媒体覆盖统计。',
                  '原会话未解析消息信息见 parent_coverage；此投影不能作为增量导出基线。'],
    }
    staging = run / ('.export-' + uuid.uuid4().hex)
    staging.mkdir()
    (staging / 'messages.jsonl').write_bytes(raw)
    (staging / 'summary.json').write_text(_canonical(summary) + '\n', encoding='utf-8')
    dashboard.load_export(staging)
    destination = run / 'export'
    staging.rename(destination)
    return destination, coverage


def analyze_selected(export_dir, source_id, config, expected_context_hash=None):
    """Analyze only the same bounded source context declared by the desktop UI."""
    _source_id(source_id)
    _require(isinstance(config, dict) and config.get('allow_chat_upload') is True,
             '尚未允许将所选聊天交给模型服务，未更新摘要。')
    initial_summary = _read_summary(export_dir)
    context = reply_assistant.build_context(export_dir, source_id)
    _require(expected_context_hash is None or context['context_hash'] == expected_context_hash,
             '聊天上下文已更新，请确认当前内容后重新分析。')
    parent_meta = read_sources(export_dir)
    parent_summary = _read_summary(export_dir, initial_summary.get('messages_sha256'))
    _require(context['account_fingerprint'] == parent_meta['account_fingerprint'] and
             context['snapshot_at'] == parent_meta['snapshot_at'], '读取期间导出发生变化，请重新加载。')
    run = _new_run('analyses')
    selected_export, coverage = _project_context(context, parent_meta, parent_summary, run)
    try:
        result = analyze_export(selected_export, config, run / 'analysis',
                                Path(DATA_ROOT) / 'assistant' / 'summary_cache', max_requests=8)
        _require(isinstance(result, dict) and isinstance(result.get('analysis_path'), (str, Path)) and
                 all(type(result.get(key)) is int and result[key] >= 0 for key in
                     ('messages', 'sources', 'analyzed_sources', 'cached_sources', 'requests')),
                 '摘要分析返回了无效的结果。')
        _require(Path(result['analysis_path']).resolve().is_relative_to((run / 'analysis').resolve()),
                 '摘要分析文件未保存到本次运行目录。')
        meta, _messages, by_uid, digest = dashboard.load_export(selected_export)
        analysis = dashboard.load_analysis(result['analysis_path'], digest, {source_id}, by_uid)
        _require(result['messages'] == len(context['messages']) and result['sources'] == 1 and
                 result['analyzed_sources'] + result['cached_sources'] == int(bool(context['messages'])) and
                 type(result['requests']) is int and 0 <= result['requests'] <= 8,
                 '摘要分析未覆盖本次限定上下文，未发布结果。')
    except (AnalysisError, AIError) as exc:
        raise WorkerError(str(exc)) from None
    except WorkerError:
        raise
    except Exception:
        raise WorkerError('所选会话摘要更新失败，保留原有结果；可稍后重试。') from None
    return {'summary': {**{key: analysis[key] for key in ('overview', 'highlights', 'followups', 'analyzed_at')},
                        'context_hash': context['context_hash'], 'stale': False},
            'analysis_path': str(result['analysis_path']), 'export_dir': str(selected_export),
            'parent_coverage': coverage}
