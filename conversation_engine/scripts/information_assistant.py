"""On-demand, account-bound organization of group and official-account records."""
from collections import Counter, defaultdict
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid

import analyze_chats
import dashboard
from ai_client import AIError, load_config
import assistant_worker
from runtime_paths import DATA_ROOT, PROJECT_ROOT


CATEGORIES = {'notice', 'reference', 'ignore'}
SOURCE_KINDS = {'群聊', '公众号'}


class InformationError(ValueError):
    """Safe message suitable for a local dashboard."""


def require(condition, message):
    if not condition:
        raise InformationError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def verified_export(path):
    meta, messages, by_uid, checksum = dashboard.load_export(path)
    summary = assistant_worker._read_summary(path, checksum)
    return meta, messages, by_uid, checksum, summary


def project_export(export_dir, destination):
    """A readable-only projection; keep original coverage separately, never fake media totals."""
    meta, rows, _by_uid, _checksum, original = verified_export(export_dir)
    sources = [source for source in meta['sources'] if source['kind'] in SOURCE_KINDS]
    selected = {source['id'] for source in sources}
    rows = [row for row in rows if row['source_id'] in selected]
    raw = ''.join(canonical(row) + '\n' for row in rows).encode('utf-8')
    counts = {key: 0 for key in dashboard.COUNT_FIELDS}
    counts.update(raw_rows=len(rows), all_messages=len(rows))
    for kind in dashboard.READABLE_KINDS:
        counts[kind] = sum(row['kind'] == kind for row in rows)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['source_id']].append(row)
    china = timezone(timedelta(hours=8))
    by_day = Counter(datetime.fromisoformat(row['time']).astimezone(china).date().isoformat() for row in rows)
    coverage = {'readable_projection_only': True, 'sources': sources,
                'unexpanded_messages': sum(source['total_messages'] - source['exported_messages'] for source in sources),
                'missing_message_shards': meta['missing_message_shards'],
                'main_database_only': meta['main_database_only']}
    summary = {'schema_version': 2, 'status': 'complete', 'analysis_only': True,
               'account_id': original['account_id'], 'source_scope': None,
               'selected_source_ids': sorted(selected), 'snapshot_at': meta['snapshot_at'],
               'start': meta['start'], 'end': meta['end'],
               'messages_sha256': hashlib.sha256(raw).hexdigest(), 'exported_messages': len(rows),
               'counts': counts, 'by_day': dict(sorted(by_day.items())),
               'sources': [{**source, 'total_messages': len(grouped[source['id']]),
                            'self_messages': sum(row['is_self'] for row in grouped[source['id']])} for source in sources],
               'missing_message_shards': meta['missing_message_shards'],
               'main_database_only': meta['main_database_only'],
               'delta': {'new': len(rows), 'updated': 0, 'unchanged': 0, 'context_messages': len(rows)},
               'parent_coverage': coverage,
               'notes': ['仅用于群聊与公众号可读信息分析；不能作为增量导出基线。']}
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'messages.jsonl').write_bytes(raw)
    analyze_chats._atomic_json(destination / 'summary.json', summary)
    dashboard.load_export(destination)
    return assistant_worker._fingerprint(original['account_id'])


class InformationService:
    def __init__(self, export_dir, *, data_root=None, config_loader=None,
                 refresher=assistant_worker.refresh_export, client_factory=None, max_requests=50):
        self.export_dir = Path(export_dir).resolve()
        _meta, _rows, _uids, _checksum, summary = verified_export(self.export_dir)
        self.account = assistant_worker._fingerprint(summary['account_id'])
        self.root = Path(data_root or DATA_ROOT).resolve() / 'information'
        require(not self.root.is_relative_to(PROJECT_ROOT.resolve()), '整理结果必须保存在个人数据目录。')
        self.root = self.root / self.account
        self.config_loader = config_loader or load_config
        self.refresher, self.client_factory = refresher, client_factory
        self.max_requests = max_requests
        self.lock = threading.RLock()
        self.thread = None
        self.closed = False
        self.status, self.message, self.model = 'idle', '', ''
        self.revision, self.current = 0, None
        self.corrections = self._load_corrections()
        self._load_latest()
        try:
            self.model = self.config_loader()['model']
        except Exception:
            pass

    def _load_corrections(self):
        try:
            path = self.root / 'corrections.json'
            require(path.stat().st_size <= 2_000_000, '分类记录过大。')
            value = dashboard.parse_json(path.read_bytes(), '分类记录')
            require(isinstance(value, dict) and value.get('account') == self.account and
                    isinstance(value.get('items'), list) and len(value['items']) <= 200, '分类记录无效。')
            fields = {'key', 'source_id', 'category', 'title', 'text'}
            result = []
            for item in value['items']:
                require(isinstance(item, dict) and set(item) == fields and
                        all(isinstance(item[key], str) for key in fields) and
                        item['category'] in CATEGORIES and len(item['key']) == 64 and
                        len(item['title']) <= 200 and len(item['text']) <= 400, '分类记录无效。')
                result.append(item)
            return result
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return []

    def _load_latest(self):
        try:
            pointer = dashboard.parse_json(dashboard.read_bytes(self.root / 'latest.json', '整理索引'), '整理索引')
            require(pointer.get('account') == self.account and isinstance(pointer.get('run'), str), '整理索引无效。')
            run = (self.root / 'runs' / pointer['run']).resolve()
            require(run.parent == (self.root / 'runs').resolve(), '整理索引范围无效。')
            self.current = self._read_result(run, pointer.get('stats', {}))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            self.current = None

    def _read_result(self, run, stats):
        meta, rows, by_uid, checksum, summary = verified_export(run / 'export')
        require(assistant_worker._fingerprint(summary['account_id']) == self.account,
                '整理结果属于其他账号，未混用。')
        require(all(source['kind'] in SOURCE_KINDS for source in meta['sources']), '整理结果超出群聊和公众号范围。')
        analysis = dashboard.load_analysis(run / 'analysis' / 'analysis.json', checksum,
                                           {source['id'] for source in meta['sources']}, by_uid)
        provenance = dashboard.parse_json(dashboard.read_bytes(run / 'provenance.json', '整理来源'), '整理来源')
        require(isinstance(provenance, dict) and isinstance(provenance.get('model'), str) and
                0 < len(provenance['model']) <= 200, '整理结果缺少模型来源。')
        require(all(item.get('category') in ('notice', 'reference') for item in analysis['highlights']),
                '整理结果缺少有效分类。')
        safe_stats = {key: value for key, value in stats.items() if key in
                      ('messages', 'sources', 'analyzed_sources', 'cached_sources', 'requests') and
                      type(value) is int and value >= 0}
        return {'meta': meta, 'messages': rows, 'by_uid': by_uid, 'analysis': analysis,
                'coverage': summary['parent_coverage'], 'stats': safe_stats,
                'model': provenance['model'], 'version': digest([checksum, analysis, provenance['model']])}

    def _item_key(self, item, current):
        evidence = [current['by_uid'][uid] for uid in sorted(item['evidence_uids'])]
        # Stable UIDs alone cannot validate a correction after a message is edited.
        return digest([item['source_id'], item['title'], 'text' in item, evidence])

    def _public_result(self):
        if not self.current:
            return None
        current = self.current
        corrections = {item['key']: item for item in self.corrections}
        items = []
        for group in ('highlights', 'followups'):
            for item in current['analysis'][group]:
                choice = corrections.get(self._item_key(item, current))
                items.append({**item, 'category': choice['category'] if choice else item.get('category', 'notice'),
                              'corrected': choice is not None, 'item_type': group})
        meta = current['meta']
        return {'version': current['version'], 'model': current['model'], 'analyzed_at': current['analysis']['analyzed_at'],
                'overview': current['analysis']['overview'], 'items': items,
                'messages': current['messages'], 'sources': meta['sources'],
                'snapshot_at': meta['snapshot_at'], 'start': meta['start'], 'end': meta['end'],
                'coverage': current['coverage'], 'stats': current['stats']}

    def get_state(self, *, include_result=True, only_if_ready=False):
        with self.lock:
            result = {'enabled': True, 'status': self.status, 'message': self.message,
                      'model': self.model, 'revision': self.revision,
                      'refreshes_wechat': self.refresher is not None}
            if include_result and (not only_if_ready or self.status != 'running'):
                result['result'] = self._public_result()
            return result

    def start(self):
        with self.lock:
            require(not self.closed, '看板服务已关闭。')
            if self.thread and self.thread.is_alive():
                return self.get_state(include_result=False)
            config = self.config_loader()
            require(config.get('allow_chat_upload') is True,
                    '现有模型配置尚未开启聊天分析授权，请在完整设置中开启；未读取新快照或调用模型。')
            self.model = config['model']
            preferences = defaultdict(list)
            for item in self.corrections:
                preferences[item['source_id']].append({key: item[key] for key in ('title', 'text', 'category')})
            preferences = {sid: examples[-8:] for sid, examples in preferences.items()}
            self.status, self.message = 'running', '正在整理，已有结果仍可查看。'
            self.revision += 1
            self.thread = threading.Thread(target=self._run, args=(copy.deepcopy(config), preferences), daemon=True)
            self.thread.start()
            return self.get_state(include_result=False)

    def _run(self, config, preferences):
        try:
            export_dir = self.export_dir
            if self.refresher is not None:
                capture = self.refresher(None)
                require(isinstance(capture, dict) and capture.get('account_fingerprint') == self.account,
                        '微信账号已变化，未分析其他账号。请重新打开该账号的看板。')
                export_dir = Path(capture['export_dir']).resolve()
            run = self.root / 'runs' / uuid.uuid4().hex
            account = project_export(export_dir, run / 'export')
            require(account == self.account, '导出账号不匹配，未调用模型。')
            stats = analyze_chats.analyze_export(run / 'export', config, run / 'analysis', self.root / 'cache',
                client=self.client_factory(config) if self.client_factory else None,
                max_requests=self.max_requests, information=True, preferences=preferences)
            analyze_chats._atomic_json(run / 'provenance.json', {'model': config['model']})
            value = self._read_result(run, stats)
            with self.lock:
                analyze_chats._atomic_json(self.root / 'latest.json',
                    {'schema_version': 1, 'account': self.account, 'run': run.name, 'stats': value['stats']})
                self.current = value
                self.status, self.message = 'idle', '整理完成。'
                self.revision += 1
        except Exception as exc:
            message = str(exc) if isinstance(exc, (InformationError, analyze_chats.AnalysisError, AIError,
                                                   dashboard.ValidationError, assistant_worker.WorkerError)) else '整理未完成，请检查本机接入和模型服务后重试。'
            with self.lock:
                self.status = 'error'
                self.message = message + ' 已有结果和缓存已保留。'
                self.revision += 1

    def correct(self, item_id, category, version):
        with self.lock:
            require(not self.closed, '看板服务已关闭。')
            require(self.status != 'running', '整理期间请稍等，完成后再修改分类。')
            require(isinstance(category, str) and category in CATEGORIES, '请选择有效的信息分类。')
            require(self.current is not None and version == self.current['version'],
                    '整理结果已经更新，请刷新后再修改分类。')
            item = next((item for group in ('highlights', 'followups') for item in self.current['analysis'][group]
                         if item['id'] == item_id), None)
            require(item is not None, '该条信息不在当前结果中。')
            key = self._item_key(item, self.current)
            text = item.get('text', item.get('next_action', ''))
            # Only server-verified text is remembered; the client cannot insert prompt instructions.
            example = {'key': key, 'source_id': item['source_id'], 'category': category,
                       'title': item['title'][:200], 'text': text[:400]}
            updated = [*[row for row in self.corrections if row['key'] != key], example][-200:]
            analyze_chats._atomic_json(self.root / 'corrections.json',
                                      {'schema_version': 1, 'account': self.account, 'items': updated})
            self.corrections = updated
            self.revision += 1
            return self.get_state()

    def close(self):
        with self.lock:
            self.closed = True
