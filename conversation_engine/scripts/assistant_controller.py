"""Thread-safe desktop state; all slow capture/model work runs in child processes."""
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
import copy
import hashlib
import json
import multiprocessing
import re
from pathlib import Path
import threading
import time
import uuid
import webbrowser

from ai_client import load_config
from runtime_paths import DATA_ROOT
import personal_style
import contact_memory
from auto_chat import AutoChat


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return copy.deepcopy(default)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        pending.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def worker(operation, *args):
    """Picklable entry point. Never echo exceptions containing paths or chat content."""
    try:
        import assistant_worker as data
        import reply_assistant as replies
        if operation == 'index':
            value = data.read_sources(*args)
        elif operation == 'capture':
            value = data.refresh_export(*args)
        elif operation == 'context':
            from image_media import public_index
            bound_summary = (Path(args[0]) / 'summary.json').read_bytes()
            value = {'context': replies.build_context(*args), 'images': public_index(args[0], args[1], limit=80),
                     'memory_context': contact_memory.export_context(args[0], args[1])}
            source = next(s for s in data.read_sources(args[0])['sources'] if s['id'] == args[1])
            value['excluded_count'] = max(0, source['total_messages'] - source['exported_messages'])
            if (Path(args[0]) / 'summary.json').read_bytes() != bound_summary:
                raise ValueError('Export changed')
        elif operation == 'image':
            from image_media import ImageService, ImageError
            try:
                value = ImageService(args[0]).preview(args[1], args[2])
            except ImageError as exc:
                return {'ok': False, 'error': str(exc)}
        elif operation in ('reply', 'auto-reply'):
            value = replies.generate_reply(*args[:4], style_examples=args[4] if len(args) > 4 else None,
                style_profile=args[5] if len(args) > 5 else None, scene=args[6] if len(args) > 6 else 'auto',
                conversation_memory=args[7] if len(args) > 7 else None, automatic=operation == 'auto-reply')
        elif operation == 'style-learn':
            value = personal_style.learn(*args)
        elif operation == 'analysis':
            value = data.analyze_selected(*args)
        elif operation == 'today-summary':
            import today_summary
            value = today_summary.summarize(*args)
        elif operation == 'dashboard':
            import dashboard
            export_dir, analysis_path, output = args
            dashboard.save_html(Path(output), dashboard.render_dashboard(export_dir, analysis_path))
            value = output
        else:
            raise ValueError('Unknown operation')
        return {'ok': True, 'value': value}
    except Exception:
        labels = {'index': '已有记录校验失败，请点击更新重新读取。',
                  'capture': '读取微信失败；请检查微信接入后重试，已有内容仍然保留。',
                  'context': '这段会话未能通过上下文校验，请更新记录后重试。',
                  'reply': '回复未生成：模型超时、连接失败或输出未通过校验。请稍后重试。',
                  'analysis': '分析未完成：请检查模型连接并重试。已有分析仍然保留。',
                  'today-summary': '当天总结未完成：请检查微信接入和模型连接后重试，已有总结仍然保留。',
                  'style-learn': '口吻学习未完成：请检查样本数量和模型连接，原档案仍然保留。',
                  'dashboard': '看板未能生成，请更新记录后重试。'}
        return {'ok': False, 'error': labels.get(operation, '操作未完成，请重试。')}


class AssistantController:
    WINDOW_INTERACTION_PAUSED = '新版已暂停微信窗口联动，请手动选择会话，复制草稿后发送。'

    def __init__(self, export_dir=None, config_path=None, data_root=None, *, executor=None,
                 config_loader=None, tracker_factory=None, signature_reader=None,
                 native_factory=None, state_directory='assistant', window_interaction=True,
                 automatic_chat=True):
        if state_directory not in ('assistant', 'assistant-csharp'):
            raise ValueError('Invalid assistant state directory')
        self.root = Path(data_root or DATA_ROOT) / state_directory
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.pool = executor or ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn'))
        self.config_path = config_path
        self.config_loader = config_loader or load_config
        self.tracker_factory = tracker_factory
        self.native_factory = native_factory
        self.window_interaction = window_interaction is True
        self.automatic_chat = automatic_chat is True
        self.signature_reader = signature_reader
        self.config = None
        self.account = None
        self.epoch = 0
        self.context_sequence = 0
        self.records = {}
        self.capture_future = None
        self.capture_request = None
        self.ai_future = None
        self.ai_request = None
        self.today_request = None
        self.style_future = None
        self.style_request = None
        self.dashboard_future = None
        self.dashboard_opening = False
        self.dashboard_servers = {}
        self.sync_records = {}
        self.checking = False
        self.context_exports = {}
        self.context_requests = {}
        self.export_dir = None
        self.index_export = None
        self.source_exports = {}
        self.consents = read_json(self.root / 'consents.json', [])
        self.pending_consents = {}
        self.pending_placement = None
        self.native_binding = None
        self.native_epoch = 0
        if not isinstance(self.consents, list):
            self.consents = []
        pointer = read_json(self.root / 'latest.json', {})
        self.initial_export = str(export_dir) if export_dir else pointer.get('export_dir')
        self.state = {
            'revision': 0, 'account_fingerprint': None,
            'binding': {'source_id': None, 'name': '', 'kind': '', 'mode': 'auto',
                        'status': 'unmatched', 'reason': '打开微信会话，或手动选择联系人。'},
            'sources': [], 'context': None, 'summary': None, 'reply': None, 'today_summary': None,
            'drafts': {'short': '', 'full': ''}, 'evidence_messages': [],
            'placement': {'id': None, 'status': 'idle', 'message': ''},
            'images': {'messages': [], 'version': None, 'total': 0}, 'image_preview': None,
            'status': {'capture': 'idle', 'ai': 'idle', 'message': '', 'last_error': ''},
            'model': {'model': '', 'base_url': '', 'consented': False,
                      'global_consent': False, 'connected': None},
            'settings': {'startup': False, 'collapsed': False},
            'reply_scene': 'auto',
            'style': {'status': 'idle', 'message': '', 'revision': 0, 'sample_count': 0,
                      'account_fingerprint': None},
            'memory': None,
        }
        self.state['capabilities'] = {'window_interaction': self.window_interaction,
                                      'window_interaction_reason': '' if self.window_interaction else self.WINDOW_INTERACTION_PAUSED}
        if not self.window_interaction:
            self.state['binding'].update(mode='pinned', reason=self.WINDOW_INTERACTION_PAUSED)
        self._publish_sync()
        self._reload_config()
        self.auto_chat = AutoChat(self)

    def _changed(self):
        self.state['revision'] += 1

    def _error(self, message, field='capture'):
        self.state['status'].update({field: 'error', 'last_error': message})
        self._changed()
        return {'ok': False, 'error': message}

    def _reload_config(self):
        try:
            config = self.config_loader(self.config_path)
            previous = self.config
            self.config = config
            self.state['model'].update(model=config['model'], base_url=config['base_url'],
                                       global_consent=config.get('allow_chat_upload') is True)
            if previous and (previous['model'], previous['base_url']) != (config['model'], config['base_url']):
                self.state['model']['connected'] = None
        except Exception:
            self.config = None
            self.state['model'].update(model='', base_url='', connected=False, global_consent=False)
        self.state['model']['consented'] = self._has_consent()

    def _consent_key(self):
        sid = self.state['binding']['source_id']
        if not self.config or not self.account or not sid:
            return None
        return fingerprint([1, self.account, sid, self.config['base_url'], self.config['model']])

    def _has_consent(self):
        # A desktop grant is limited to this account, conversation, endpoint and model.
        return bool(self.config and (self.config.get('allow_chat_upload') is True or
                                    self._consent_key() in self.consents))

    def _today_consent_key(self):
        return fingerprint(['today-summary/1', self._consent_key()]) if self._consent_key() else None

    def _has_today_consent(self):
        return bool(self.config and (self.config.get('allow_chat_upload') is True or
                                     self._today_consent_key() in self.consents))

    def _binding_key(self):
        return (self.account, self.state['binding']['source_id'])

    def _sync_record(self, key=None):
        key = self._binding_key() if key is None else key
        return self.sync_records.setdefault(key, {
            'signature': None, 'checked_at': None, 'last_checked_at': None,
            'last_refreshed_at': None, 'check_due': True, 'failures': 0,
            'retry_at': 0, 'recover_index': False, 'force': 0, 'completed_force': 0,
            'context_retry': False, 'status': 'idle',
        })

    def _publish_sync(self):
        record = self._sync_record()
        collapsed = self.state['settings']['collapsed']
        self.state['sync'] = {
            'source_id': self.state['binding']['source_id'],
            'mode': 'background' if collapsed else 'active',
            'interval_seconds': 10 if collapsed else 5,
            'status': record['status'],
            'last_checked_at': record['last_checked_at'],
            'last_refreshed_at': record['last_refreshed_at'],
            'retry_count': record['failures'],
        }

    def _request_check(self):
        self._sync_record()['check_due'] = True
        self._publish_sync()

    def _sync_failed(self, key, message, *, recover_index=False):
        record = self._sync_record(key)
        record['failures'] += 1
        delay = (5, 10, 20, 30)[min(record['failures'] - 1, 3)]
        record.update(retry_at=time.monotonic() + delay, status='retrying')
        if recover_index:
            record['recover_index'] = True
        if key == self._binding_key():
            self._publish_sync()
            self._error(message)
        else:
            self._changed()

    def _check_updates(self, signature_reader):
        """One watcher tick. Stat outside the lock; never invoke AI or overlap captures."""
        with self.lock:
            if self.stopped.is_set() or self.today_request or self.checking or self.capture_request or (
                    self.capture_future and not self.capture_future.done()):
                return
            key, epoch = self._binding_key(), self.epoch
            record = self._sync_record(key)
            if record['context_retry'] and key in self.context_requests:
                return
            now = time.monotonic()
            interval = 10 if self.state['settings']['collapsed'] else 5
            if not record['check_due'] and record['checked_at'] is not None and now - record['checked_at'] < interval:
                return
            # Backoff applies to the whole local attempt, including signature errors.
            if now < record['retry_at']:
                return
            record.update(check_due=False, checked_at=now, status='checking')
            self.checking = True
            self._publish_sync()
            self._changed()
        try:
            signature = signature_reader()
            if not isinstance(signature, str) or not signature:
                raise ValueError('Invalid source signature')
        except Exception:
            with self.lock:
                self.checking = False
                if key == self._binding_key() and epoch == self.epoch:
                    self._sync_failed(key, '暂时无法检查新消息；会自动重试，已有记录仍可查看。')
            return
        with self.lock:
            self.checking = False
            if key != self._binding_key() or epoch != self.epoch or self.stopped.is_set():
                return
            record['last_checked_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            if record['signature'] == signature and record['force'] == record['completed_force'] and not record['recover_index']:
                if record['context_retry']:
                    record['status'] = 'updating'
                    self._publish_sync()
                    self._changed()
                    self._load_context(key[1])
                    return
                record.update(status='current', failures=0, retry_at=0)
                if self.state['status']['capture'] == 'error':
                    self.state['status'].update(capture='idle', last_error='')
                self._publish_sync()
                self._changed()
                return
            self._capture_changed(signature)

    def _record(self):
        return self.records.get(self._binding_key())

    def _record_path(self, key):
        return self.root / 'conversations' / (fingerprint(key) + '.json')

    def _save_record(self, key):
        try:
            write_json(self._record_path(key), self.records[key])
            return True
        except OSError:
            self._error('内容仍保留在窗口中，但暂时无法保存到本机。')
            return False

    def _load_record(self, key):
        record = read_json(self._record_path(key), {})
        clean = {'account': key[0], 'source_id': key[1], 'context': None, 'reply': None,
                 'summary': None, 'reply_context': None, 'summary_context': None,
                 'drafts': {'short': '', 'full': ''}, 'edit_revision': 0,
                 'reply_key': None, 'analysis_path': None, 'analysis_export': None, 'style_examples': [], 'scene': 'auto',
                 'memory': contact_memory.empty(*key), 'memory_error': '', 'today_summary': None}
        if not isinstance(record, dict) or (record.get('account'), record.get('source_id')) != key:
            return clean
        if 'memory' in record:
            clean['memory'] = copy.deepcopy(record['memory'])
            try:
                contact_memory.validate(clean['memory'], *key)
            except (ValueError, TypeError, KeyError):
                clean['memory_error'] = '会话记忆未通过校验，已有内容仍保留。'
        try:
            from communication_skills import SCENES
            if record.get('scene') in SCENES:
                clean['scene'] = record['scene']
            from reply_assistant import _validate_context, _validate_reply
            for field in ('context', 'reply_context', 'summary_context'):
                context = record.get(field)
                if context is not None:
                    _validate_context(context)
                    if (context['account_fingerprint'], context['source']['id']) != key:
                        raise ValueError('Wrong account')
                    clean[field] = context
            reply = record.get('reply')
            if reply and clean['reply_context']:
                ctx = clean['reply_context']
                evidence = {m['uid'] for m in ctx['messages']}
                if reply.get('memory') is not None:
                    from reply_assistant import validate_memory_packet
                    evidence |= validate_memory_packet(reply['memory'], ctx)
                _validate_reply({k: reply[k] for k in ('facts', 'inferences', 'questions', 'drafts')}, evidence)
                if reply['source_id'] != key[1] or reply['context_hash'] != ctx['context_hash']:
                    raise ValueError('Wrong reply')
                clean['reply'] = reply
                clean['reply_key'] = record.get('reply_key')
            summary = record.get('summary')
            if summary and clean['summary_context'] and record.get('analysis_path') and record.get('analysis_export'):
                import dashboard
                meta, rows, by_uid, digest = dashboard.load_export(record['analysis_export'])
                from assistant_worker import read_sources
                if (read_sources(record['analysis_export'])['account_fingerprint'] != key[0] or
                        rows != clean['summary_context']['messages']):
                    raise ValueError('Wrong cached analysis context')
                verified = dashboard.load_analysis(record['analysis_path'], digest, {key[1]}, by_uid)
                if summary['context_hash'] != clean['summary_context']['context_hash']:
                    raise ValueError('Wrong summary')
                clean.update(summary={**verified, 'context_hash': summary['context_hash'], 'stale': False},
                             analysis_path=record['analysis_path'], analysis_export=record['analysis_export'])
            drafts = record.get('drafts')
            if isinstance(drafts, dict) and all(isinstance(drafts.get(k), str) and len(drafts[k]) <= 20000
                                              for k in ('short', 'full')):
                clean['drafts'] = {k: drafts[k] for k in ('short', 'full')}
            examples = record.get('style_examples', [])
            if isinstance(examples, list) and len(examples) <= 8 and all(
                    isinstance(value, str) and 0 < len(value) <= 500 for value in examples):
                from reply_assistant import safe_style_examples
                clean['style_examples'] = safe_style_examples(examples)
        except Exception:
            # Corrupt caches are ignored, never relabelled as verified conversation data.
            return {**clean, 'reply': None, 'summary': None, 'context': None,
                    'reply_context': None, 'summary_context': None, 'drafts': {'short': '', 'full': ''}}
        if record.get('today_summary'):
            try:
                import today_summary
                value = record['today_summary']
                if value['date'] == today_summary.today():
                    clean['today_summary'] = today_summary.load_result(value, key[1], key[0], value['date'],
                                                                       data_root=self.root.parent)
            except Exception:
                pass  # A daily cache cannot invalidate editable reply drafts.
        return clean

    def _publish_today(self):
        import today_summary
        value = (self._record() or {}).get('today_summary')
        self.state['today_summary'] = ({k: copy.deepcopy(value[k]) for k in
            ('date', 'source_id', 'account_fingerprint', 'snapshot_at', 'messages', 'excluded_messages')}
            if value and value['date'] == today_summary.today() else None)

    def _publish_record(self):
        record = self._record()
        for field in ('context', 'summary', 'reply'):
            self.state[field] = copy.deepcopy(record.get(field)) if record else None
        self.state['drafts'] = copy.deepcopy(record['drafts']) if record else {'short': '', 'full': ''}
        self.state['reply_scene'] = record.get('scene', 'auto') if record else 'auto'
        from communication_skills import scene_label
        self.state['scene_label'] = scene_label(self.state['context'], self.state['reply_scene'])
        self._publish_today()
        try:
            memory = contact_memory.validate(record['memory'], *self._binding_key()) if record else None
            self.state['memory'] = {'account_fingerprint': self.account, 'source_id': self.state['binding']['source_id'],
                'revision': memory['revision'], 'settings_revision': memory['settings_revision'],
                'message_count': len(memory['messages']), 'example_count': len(memory['examples']),
                'error': record.get('memory_error', '')} if memory else None
        except (ValueError, TypeError, KeyError):
            self.state['memory'] = {'source_id': self.state['binding']['source_id'], 'error': '会话记忆未通过校验，已有内容仍保留。'}
        if self.state['summary']:
            self.state['summary']['stale'] = (not self.state['context'] or
                self.state['summary']['context_hash'] != self.state['context']['context_hash'])
        if self.state['reply']:
            self.state['reply']['stale'] = (not self.state['context'] or
                self.state['reply']['context_hash'] != self.state['context']['context_hash'])
        evidence = {}
        for field in ('reply_context', 'summary_context', 'context'):
            for row in (record or {}).get(field, {}).get('messages', []) if (record or {}).get(field) else []:
                evidence[row['uid']] = row
        for row in ((record or {}).get('reply') or {}).get('memory', {}).get('messages', []):
            evidence[row['uid']] = row
        self.state['evidence_messages'] = sorted(evidence.values(), key=lambda row: (datetime.fromisoformat(row['time']), row['uid']))
        self.state['model']['consented'] = self._has_consent()
        self._changed()

    def get_state(self, since_revision=None):
        with self.lock:
            previous = self.state['today_summary']
            self._publish_today()
            if previous != self.state['today_summary']:
                self._changed()
            if type(since_revision) is int and since_revision == self.state['revision']:
                return {'revision': since_revision, 'unchanged': True}
            automatic = self.auto_chat.public()
            if not self.window_interaction or not self.automatic_chat:
                automatic.update(supported=False, enabled=False, active_contacts=0, status='off',
                    reason=self.WINDOW_INTERACTION_PAUSED if not self.window_interaction else '窗口联动已开启，自动发送待单独验收。')
            return {**copy.deepcopy(self.state), 'automatic': automatic}

    def set_auto_chat(self, enabled, expected_source_id=None, expected_account=None):
        with self.lock:
            if enabled and not self.window_interaction:
                return {'ok': False, 'error': self.WINDOW_INTERACTION_PAUSED}
            if enabled and not self.automatic_chat:
                return {'ok': False, 'error': '窗口联动已开启，自动发送待单独验收。'}
            return self.auto_chat.set_enabled(enabled, expected_source_id, expected_account)

    def pause_auto_chat(self, expected_account=None):
        with self.lock:
            return self.auto_chat.pause_all(expected_account)

    def _publish_style(self, profile=None, *, status='idle', message=''):
        try:
            profile = profile or (personal_style.load_profile(self.root.parent, self.account) if self.account else None)
        except personal_style.StyleError as exc:
            profile, status, message = None, 'error', str(exc)
        self.state['style'] = {'status': status, 'message': message,
            'revision': profile['revision'] if profile else 0,
            'sample_count': profile['sample_count'] if profile else 0, 'account_fingerprint': self.account}
        self._changed()

    def get_style_profile(self):
        with self.lock:
            if not self.account:
                return {'ok': False, 'error': '请先接入本人账号。'}
            try:
                profile = personal_style.load_profile(self.root.parent, self.account)
            except personal_style.StyleError as exc:
                return {'ok': False, 'error': str(exc)}
            return {'ok': True, 'account_fingerprint': self.account, 'profile': profile,
                    'scene': self.state['reply_scene'],
                    'learning': {k: self.state['style'][k] for k in ('status', 'message')}}

    def _memory_binding(self, expected_source_id, expected_account):
        if not expected_account or expected_account != self.account or not self._expected(expected_source_id):
            raise contact_memory.MemoryError('账号或会话已变化，请重新打开会话记忆。')
        record = self._record()
        contact_memory.require(record is not None)
        return record, contact_memory.validate(record['memory'], self.account, expected_source_id)

    def get_contact_memory(self, expected_source_id=None, expected_account=None):
        with self.lock:
            try:
                _record, memory = self._memory_binding(expected_source_id, expected_account)
                public = {k: copy.deepcopy(memory[k]) for k in ('revision', 'settings_revision', 'enabled', 'feedback_enabled',
                                                                'notes', 'snapshot_at', 'messages', 'pins', 'examples')}
                return {'ok': True, 'account_fingerprint': self.account, 'source_id': expected_source_id,
                        'source_name': self.state['binding']['name'], 'memory': public}
            except (ValueError, TypeError, KeyError):
                return {'ok': False, 'error': '会话记忆暂不可用，原内容已保留；请在当前会话重新打开。'}

    def _save_memory_change(self, record, memory):
        previous = record['memory']
        record['memory'] = memory
        if not self._save_record(self._binding_key()):
            record['memory'] = previous
            return {'ok': False, 'error': '会话记忆暂时无法保存，已保留原设置。'}
        record['memory_error'] = ''
        self._cancel_style_placement()
        self._publish_record()
        return self.get_contact_memory(self.state['binding']['source_id'], self.account)

    def save_contact_memory(self, changes, expected_source_id=None, expected_account=None, expected_revision=None):
        with self.lock:
            try:
                record, memory = self._memory_binding(expected_source_id, expected_account)
                updated = contact_memory.preferences(memory, changes, expected_revision)
                return self._save_memory_change(record, updated)
            except contact_memory.MemoryError as exc:
                return {'ok': False, 'error': str(exc)}
            except (ValueError, TypeError, KeyError):
                return {'ok': False, 'error': '会话偏好未通过校验，原内容已保留。'}

    def update_memory_item(self, uid, action, expected_source_id=None, expected_account=None, expected_revision=None):
        with self.lock:
            try:
                record, memory = self._memory_binding(expected_source_id, expected_account)
                updated = contact_memory.change_item(memory, uid, action, expected_revision)
                return self._save_memory_change(record, updated)
            except contact_memory.MemoryError as exc:
                return {'ok': False, 'error': str(exc)}
            except (ValueError, TypeError, KeyError):
                return {'ok': False, 'error': '这条记忆未通过校验，原内容已保留。'}

    def clear_contact_memory(self, expected_source_id=None, expected_account=None, expected_revision=None):
        with self.lock:
            try:
                record, memory = self._memory_binding(expected_source_id, expected_account)
                latest = (record.get('context') or {}).get('snapshot_at')
                updated = contact_memory.clear(memory, expected_revision, latest)
                return self._save_memory_change(record, updated)
            except contact_memory.MemoryError as exc:
                return {'ok': False, 'error': str(exc)}
            except (ValueError, TypeError, KeyError):
                return {'ok': False, 'error': '记忆未通过校验，原内容已保留。'}

    def _cancel_style_placement(self):
        self.pending_placement = None
        self.state['placement'] = {'id': None, 'status': 'idle', 'message': ''}

    def save_style_profile(self, preferences, expected_account=None, expected_revision=None):
        with self.lock:
            if not expected_account or expected_account != self.account:
                return {'ok': False, 'error': '账号已变化，请重新打开我的口吻。'}
            try:
                profile = personal_style.save_preferences(self.root.parent, self.account, expected_revision, preferences)
            except (personal_style.StyleError, OSError) as exc:
                return {'ok': False, 'error': str(exc) if isinstance(exc, personal_style.StyleError) else '口吻暂时无法保存，原档案已保留。'}
            self._cancel_style_placement()
            self._publish_style(profile, message='口吻偏好已保存。')
            return self.get_style_profile()

    def set_reply_scene(self, scene, expected_source_id=None, expected_account=None):
        from communication_skills import SCENES
        with self.lock:
            if not expected_account or expected_account != self.account or not self._expected(expected_source_id):
                return {'ok': False, 'error': '账号或会话已变化，请在当前会话重新选择。'}
            if scene not in SCENES:
                return {'ok': False, 'error': '请选择有效的回复场景。'}
            record = self._record()
            previous_scene, previous_revision = record.get('scene', 'auto'), record.get('scene_revision', 0)
            record['scene'] = scene
            record['scene_revision'] = previous_revision + 1
            if not self._save_record(self._binding_key()):
                record.update(scene=previous_scene, scene_revision=previous_revision)
                return {'ok': False, 'error': '场景暂时无法保存，已保留原选择，请重试。'}
            self._cancel_style_placement()
            self._publish_record()
            return {'ok': True, 'scene': scene}

    def learn_style(self, expected_account=None, expected_revision=None):
        with self.lock:
            if not expected_account or expected_account != self.account or not self.index_export:
                return {'ok': False, 'error': '账号或记录已变化，请重新打开我的口吻。'}
            self._reload_config()
            if not self.config or self.config.get('allow_chat_upload') is not True:
                return {'ok': False, 'error': '跨会话学习需要在模型配置中允许处理本人文字。'}
            if self.today_request or any(future and not future.done() for future in (self.ai_future, self.style_future)):
                return {'ok': False, 'error': '上一项模型请求仍在处理，请稍后重试。'}
            try:
                profile = personal_style.load_profile(self.root.parent, self.account)
                personal_style.require(type(expected_revision) is int and profile['revision'] == expected_revision,
                                       '口吻设置已变化，请重新打开后再学习。')
            except personal_style.StyleError as exc:
                return {'ok': False, 'error': str(exc)}
            token, account, config = object(), self.account, copy.deepcopy(self.config)
            self.style_request = token
            self._publish_style(profile, status='running', message='正在学习本人已发送文字…')
            def done(result):
                if self.style_request is not token or self.account != account:
                    return
                self.style_future = None
                self.style_request = None
                self._reload_config()
                if not self.config or self.config.get('allow_chat_upload') is not True or any(
                        self.config[k] != config[k] for k in ('model', 'base_url')):
                    self._publish_style(status='error', message='模型设置已变化，本次学习未保存。')
                    return
                if not result['ok']:
                    self._publish_style(status='error', message=result['error'])
                    return
                try:
                    updated = personal_style.commit_learning(self.root.parent, account, expected_revision, result['value'])
                except (personal_style.StyleError, OSError) as exc:
                    self._publish_style(status='error', message=str(exc) if isinstance(exc, personal_style.StyleError)
                                        else '口吻暂时无法保存，原档案已保留。')
                    return
                self._cancel_style_placement()
                self._publish_style(updated, message='口吻已学习，可查看和修改。')
            self.style_future = self._submit('style-learn', (self.index_export, config, account), done)
            return {'ok': True, 'learning': True}

    def _submit(self, operation, args, callback):
        future = self.pool.submit(worker, operation, *args)
        def completed(done):
            try:
                result = done.result()
            except Exception:
                result = {'ok': False, 'error': '后台任务未能完成，请重试。'}
            with self.lock:
                if not self.stopped.is_set():
                    callback(result)
        future.add_done_callback(completed)
        return future

    def start(self):
        with self.lock:
            self.state['status']['capture'] = 'running' if self.initial_export else 'idle'
            self._changed()
            if self.initial_export:
                self.capture_future = self._submit('index', (self.initial_export,), self._index_done)
        self.watch_thread = threading.Thread(target=self._watch, name='conversation-watch', daemon=True)
        self.watch_thread.start()

    def _index_done(self, result, *, load_context=True):
        self.capture_future = None
        if not result['ok']:
            self._error(result['error'])
            return
        value = result['value']
        account = value['account_fingerprint']
        account_changed = self.account != account
        if self.account and self.account != account:
            self._switch(None)
            self.source_exports.clear()
            self.state['binding']['mode'] = 'auto'
        self.account = account
        if account_changed:
            self.style_request = None
            self._publish_style()
        self.state['account_fingerprint'] = account
        self.state['sources'] = value['sources']
        self.index_export = value['export_dir']
        self.initial_export = self.index_export
        self.export_dir = self.index_export
        for source in value['sources']:
            self.source_exports[source['id']] = self.index_export
        self.state['status'].update(capture='idle', message='记录在本机 · 点击后才调用模型', last_error='')
        try:
            write_json(self.root / 'latest.json', {'export_dir': self.index_export, 'account_fingerprint': account})
        except OSError:
            self._error('记录已读取，但启动缓存暂时无法保存。')
        sid = self.state['binding']['source_id']
        if sid and not any(source['id'] == sid for source in value['sources']):
            self._switch(None, reason='该会话不在当前记录中，请重新选择。')
        elif sid and load_context:
            self._load_context(sid)
        self._request_check()
        self._changed()

    def _switch(self, source, *, mode=None, reason=''):
        self.epoch += 1
        self.pending_consents.clear()
        self.pending_placement = None
        self.state['images'] = {'messages': [], 'version': None, 'total': 0}
        self.state['image_preview'] = None
        self.state['placement'] = {'id': None, 'status': 'idle', 'message': ''}
        binding = self.state['binding']
        binding.update(source_id=source['id'] if source else None, name=source['name'] if source else '',
                       kind=source['kind'] if source else '', status='matched' if source else 'unmatched', reason=reason)
        if mode:
            binding['mode'] = mode
        self.state['status'].update(last_error='', ai='idle')
        if source:
            key = self._binding_key()
            if key not in self.records:
                self.records[key] = self._load_record(key)
        self._request_check()
        self._publish_record()
        if source:
            self._load_context(source['id'])

    def _load_context(self, sid):
        export_dir = self.source_exports.get(sid)
        if not export_dir:
            return
        self.context_sequence += 1
        sequence = self.context_sequence
        epoch, key = self.epoch, self._binding_key()
        token = object()
        self.context_requests[key] = token
        def done(result):
            if self.context_requests.get(key) is token:
                self.context_requests.pop(key)
            if key != self._binding_key() or epoch != self.epoch or sequence != self.context_sequence:
                return
            if not result['ok']:
                self._sync_record(key)['context_retry'] = True
                self._sync_failed(key, result['error'])
                return
            bundle = result['value']
            context = bundle.get('context', bundle)
            if (context['account_fingerprint'], context['source']['id']) != key:
                self._switch(None, reason='记录账号发生变化，请重新选择会话。')
                return
            previous = self.records[key].get('context')
            try:
                self.records[key]['memory'] = contact_memory.ingest(self.records[key]['memory'], bundle.get('memory_context', context))
                self.records[key]['memory_error'] = ''
            except (ValueError, TypeError, KeyError):
                self.records[key]['memory_error'] = '本次原文未通过记忆校验，保留了已有记忆。'
            images = bundle.get('images', {'messages': [], 'version': None, 'total': 0})
            if self.state['images'].get('version') != images.get('version'):
                self.state['image_preview'] = None
            self.state['images'] = copy.deepcopy(images)
            self.records[key]['image_export'] = export_dir
            same_input = bool(previous and key in self.context_exports and all(
                previous.get(field) == context.get(field) for field in
                ('account_fingerprint', 'source', 'messages', 'start', 'count', 'total', 'truncated')))
            # Keep the verified snapshot (and its export) when only capture time moved.
            # Otherwise unrelated DB writes invalidate both draft freshness and AI cache.
            if not same_input:
                self.records[key]['context'] = context
                self.context_exports[key] = export_dir
            self.export_dir = self.context_exports[key]
            sync = self._sync_record(key)
            if sync['context_retry']:
                sync.update(context_retry=False, failures=0, retry_at=0,
                            status='current' if sync['signature'] else 'idle')
                self.state['status'].update(capture='idle', last_error='')
                self._publish_sync()
            record = self.records[key]
            excluded = bundle.get('excluded_count', record.get('excluded_count', 0))
            if type(excluded) is not int or excluded < 0:
                excluded = record.get('excluded_count', 0)
            record['excluded_count'] = excluded
            # Auto jobs share the retained reply context when only capture time
            # changes; fresh media counts must still be checked independently.
            self.auto_chat.observe(record['context'], excluded)
            self._save_record(key)
            self._publish_record()
        try:
            self._submit('context', (export_dir, sid), done)
        except Exception:
            done({'ok': False, 'error': '上下文暂时未能读取，会自动重试。'})

    def select_conversation(self, source_id):
        with self.lock:
            source = next((s for s in self.state['sources'] if s['id'] == source_id), None)
            if not source:
                return {'ok': False, 'error': '请选择当前账号中的会话。'}
            self._switch(source, mode='pinned', reason='已固定此会话；切换微信不会改变这里的内容。')
            return {'ok': True}

    def open_image(self, uid, version, expected_source_id):
        with self.lock:
            if not self._expected(expected_source_id) or version != self.state['images'].get('version') or not any(
                    row['uid'] == uid for row in self.state['images']['messages']):
                return {'ok': False, 'error': '会话或图片记录已变化，请重新打开。'}
            if (self.state.get('image_preview') or {}).get('status') == 'loading':
                return {'ok': False, 'error': '图片正在打开，请稍候。'}
            epoch, key, request = self.epoch, self._binding_key(), uuid.uuid4().hex
            self.state['image_preview'] = {'request': request, 'status': 'loading', 'uid': uid}
            self._changed()
            def done(result):
                if (self.epoch != epoch or key != self._binding_key() or
                        self.state['images'].get('version') != version or
                        (self.state.get('image_preview') or {}).get('request') != request):
                    return
                value = result.get('value')
                valid = bool(result['ok'] and value and value.get('uid') == uid and
                             value.get('source_id') == key[1] and value.get('version') == version)
                self.state['image_preview'] = {'request': request, 'uid': uid,
                    'status': 'ready' if valid else 'error', 'image': value if valid else None,
                    'error': '' if valid else result.get('error', '图片校验失败，请重新打开。')}
                self._changed()
            try:
                self._submit('image', (self.records[key]['image_export'], uid, version), done)
            except Exception:
                done({'ok': False, 'error': '图片暂时无法打开，请重试。'})
            return {'ok': True}

    def set_following(self, enabled):
        with self.lock:
            if type(enabled) is not bool:
                return {'ok': False, 'error': '跟随设置无效。'}
            if enabled and not self.window_interaction:
                return {'ok': False, 'error': self.WINDOW_INTERACTION_PAUSED}
            if not enabled and not self.state['binding']['source_id']:
                return {'ok': False, 'error': '先选择一个会话，再固定。'}
            self.state['binding']['mode'] = 'auto' if enabled else 'pinned'
            self._changed()
            return {'ok': True}

    def apply_tracking(self, result):
        with self.lock:
            binding = self.state['binding']
            if binding['mode'] != 'auto':
                return
            rect = result.get('window_rect') if result.get('status') == 'matched' else None
            if self.state.get('window_rect') != rect:
                self.state['window_rect'] = rect
                self._changed()
            sid = result.get('source_id') if result.get('status') == 'matched' else None
            matches = [s for s in self.state['sources'] if sid and s['id'] == sid]
            source = matches[0] if len(matches) == 1 else None
            # Losing foreground focus or a title sample is not a conversation
            # switch. Keep the display, drafts and pending analysis until another
            # known conversation is positively identified. The watcher still
            # invalidates native_epoch independently before any window write.
            if source and sid != binding['source_id']:
                self._switch(source, reason=result.get('reason', ''))
            status, reason = result.get('status', 'unsupported'), result.get('reason', '')
            if source is None:
                if binding['source_id']:
                    status = 'retained'
                    reason = reason or '当前会话已保留，识别到新会话后再切换。'
                elif status == 'matched':
                    status = 'unmatched'
                    reason = '当前会话不在已读取的列表中，等待识别。'
            if (binding['status'], binding['reason']) != (status, reason):
                binding.update(status=status, reason=reason)
                self._changed()

    def _expected(self, expected):
        return bool(expected and self.state['binding']['source_id'] == expected and self.account)

    def refresh(self, expected_source_id=None):
        with self.lock:
            if expected_source_id and not self._expected(expected_source_id):
                return {'ok': False, 'error': '会话已切换，请在当前会话重新操作。'}
            record = self._sync_record()
            record['force'] += 1
            record['retry_at'] = 0
            self._request_check()  # Watcher checks immediately; no model request.
            self._changed()
            return {'ok': True}

    def _capture_changed(self, signature=None):
        with self.lock:
            if self.today_request or self.capture_request or (self.capture_future and not self.capture_future.done()):
                return
            key = self._binding_key()
            record = self._sync_record(key)
            sid = None if record['recover_index'] else key[1]
            request = {'key': key, 'signature': signature, 'force': record['force'], 'source_id': sid}
            self.capture_request = request
            record['status'] = 'updating'
            self.state['status'].update(capture='running', last_error='')
            self._publish_sync()
            self._changed()
            def done(result):
                if self.capture_request is not request:
                    return
                self.capture_request = None
                self.capture_future = None
                self.state['status']['capture'] = 'idle'
                # An independently loaded account must never be replaced by a late capture.
                if key[0] != self.account:
                    self._request_check()
                    self._changed()
                    return
                if not result['ok']:
                    self._sync_failed(key, result['error'], recover_index=sid is not None)
                    return
                value = result['value']
                if sid is not None and value['account_fingerprint'] != self.account:
                    # Validate a complete list before changing the account or its binding.
                    self._sync_failed(key, '微信账号可能已变化，正在等待重新读取会话列表。', recover_index=True)
                    return
                refreshed_at = datetime.now().astimezone().isoformat(timespec='seconds')
                keys = [key]
                if sid is None:
                    self._index_done(result, load_context=False)
                    keys = [(self.account, None), *((self.account, s['id']) for s in value['sources'])]
                else:
                    self.source_exports[sid] = value['export_dir']
                for completed_key in keys:
                    completed = self._sync_record(completed_key)
                    completed.update(signature=signature, last_refreshed_at=refreshed_at,
                                     failures=0, retry_at=0, recover_index=False, status='current')
                    if completed_key == key:
                        completed['completed_force'] = request['force']
                    # Other sources' explicit refresh requests remain pending.
                self._publish_sync()
                self._changed()
                current_sid = self.state['binding']['source_id']
                if current_sid and (sid is None or self._binding_key() == key):
                    self._load_context(current_sid)
            try:
                self.capture_future = self._submit('capture', (sid,), done)
            except Exception:
                self.capture_request = None
                self.capture_future = None
                self._sync_failed(key, '后台读取暂时不可用，会自动重试。', recover_index=sid is not None)

    def grant_consent(self, expected_source_id=None, token=None):
        with self.lock:
            if not self._expected(expected_source_id):
                return {'ok': False, 'error': '会话已切换，未保存授权。'}
            self._reload_config()
            pending = self.pending_consents.pop(token, None) if isinstance(token, str) else None
            key = self._today_consent_key() if pending and len(pending) == 4 and pending[3] == 'today' else self._consent_key()
            if not key:
                return {'ok': False, 'error': '模型配置不可用，请先配置模型服务。'}
            if not pending or pending[:2] != (key, self.epoch) or time.monotonic() > pending[2]:
                return {'ok': False, 'error': '会话或模型配置已变化，请重新查看处理范围后授权。'}
            if key not in self.consents:
                granted = [*self.consents, key]
                try:
                    write_json(self.root / 'consents.json', granted)
                except OSError:
                    return {'ok': False, 'error': '授权无法保存，未发送聊天。'}
                self.consents = granted
            self.state['model']['consented'] = self._has_consent()
            self._changed()
            return {'ok': True}

    def _model_action(self, operation, expected_source_id, goal='', supplement='',
                      *, place_in_wechat=False, expected_account=None):
        with self.lock:
            if place_in_wechat and (expected_account != self.account or not expected_account):
                return {'ok': False, 'error': '账号已变化，请在当前会话重新操作。'}
            if not self._expected(expected_source_id):
                return {'ok': False, 'error': '会话已切换，请在当前会话重新操作。'}
            if self.auto_chat.public()['active_contacts']:
                self.auto_chat.pause_all(self.account)
            self._reload_config()
            if self.config is None:
                return self._error('模型配置不可用，请检查本机模型配置。', 'ai')
            record = self._record()
            context = record.get('context') if record else None
            if not context or not context['messages']:
                return {'ok': False, 'error': '这段会话还没有可读上下文，请先更新记录。'}
            if not all(isinstance(s, str) and len(s) <= 2000 for s in (goal, supplement)):
                return {'ok': False, 'error': '想法和补充内容分别不能超过 2000 字。'}
            if not self._has_consent():
                token = uuid.uuid4().hex
                self.pending_consents = {token: (self._consent_key(), self.epoch, time.monotonic() + 300)}
                return {'ok': False, 'consent_required': True, 'consent': {
                    'token': token, 'model': self.config['model'], 'base_url': self.config['base_url'],
                    'source_name': self.state['binding']['name']}}
            if self.today_request or any(future and not future.done() for future in (self.ai_future, self.style_future)):
                return {'ok': False, 'error': '上一项模型请求仍在处理，请稍后重试。'}
            from reply_assistant import SYSTEM_PROMPT
            try:
                profile = personal_style.load_profile(self.root.parent, self.account) if operation == 'reply' else None
            except personal_style.StyleError as exc:
                return self._error(str(exc), 'ai')
            scene = record.get('scene', 'auto')
            style_key, scene_revision = fingerprint(profile), record.get('scene_revision', 0)
            try:
                memory_packet = contact_memory.for_reply(record['memory'], context, goal) if operation == 'reply' else None
                memory_settings_revision = record['memory']['settings_revision'] if operation == 'reply' else None
            except (ValueError, TypeError, KeyError):
                return self._error('会话记忆未通过校验，原内容已保留。请检查会话记忆。', 'ai')
            memory_key = fingerprint({k: memory_packet[k] for k in ('messages', 'notes', 'sent_edits')}) if memory_packet else None
            request_key = fingerprint([operation, context['context_hash'], self.config['base_url'],
                                       self.config['model'], goal, supplement, SYSTEM_PROMPT, style_key, scene, memory_key, contact_memory.VERSION])
            if operation == 'reply' and record.get('reply_key') == request_key and record.get('reply'):
                self.state['status'].update(ai='idle', message='已复用本次输入的已有建议。', last_error='')
                self._publish_record()
                if place_in_wechat:
                    placement = self._queue_placement(record['drafts']['short'], context['context_hash'])
                    if not placement['ok']:
                        return placement
                return {'ok': True, 'cached': True}
            key, epoch, edit_revision = self._binding_key(), self.epoch, record['edit_revision']
            native_epoch = self.native_epoch
            if place_in_wechat:
                self.pending_placement = None
                self.state['placement'] = {'id': None, 'status': 'idle', 'message': ''}
            captured_context = copy.deepcopy(context)
            config = {**self.config, 'allow_chat_upload': True}
            args = (captured_context, config, goal, supplement, [], profile, scene, memory_packet) if operation == 'reply' else (
                self.context_exports.get(key, self.source_exports[expected_source_id]),
                expected_source_id, config, captured_context['context_hash'])
            self.state['status'].update(ai='running', last_error='', message='正在理解这段会话…')
            token = object()
            self.ai_request = token
            self._changed()
            def done(result):
                if self.ai_request is not token:
                    return
                self.ai_future = None
                self.ai_request = None
                same = self._binding_key() == key and self.epoch == epoch
                latest = self.records.get(key)
                if not result['ok']:
                    if same:
                        self.state['model']['connected'] = False
                        self._error(result['error'], 'ai')
                    return
                if latest is None:
                    return
                if operation == 'reply':
                    try:
                        unchanged_style = (fingerprint(personal_style.load_profile(self.root.parent, key[0])) == style_key and
                            latest.get('scene_revision', 0) == scene_revision and
                            latest['memory']['settings_revision'] == memory_settings_revision)
                    except personal_style.StyleError:
                        unchanged_style = False
                    if not unchanged_style:
                        if same:
                            self.state['status'].update(ai='idle', message='口吻或场景已变化，请重新生成建议。')
                            self._changed()
                        return
                value = result['value']
                unchanged_input = bool(latest.get('context') and
                    latest['context']['context_hash'] == captured_context['context_hash'])
                if operation == 'reply':
                    if value['source_id'] != key[1] or value['context_hash'] != captured_context['context_hash']:
                        if same:
                            self._error('返回结果与所选上下文不一致，未展示。', 'ai')
                        return
                    value['input'] = {'goal': goal, 'supplement': supplement}
                    value['model'] = {'model': config['model'], 'base_url': config['base_url']}
                    latest.update(reply=value, reply_context=captured_context, reply_key=request_key)
                    if unchanged_input and latest['edit_revision'] == edit_revision:
                        latest['drafts'] = copy.deepcopy(value['drafts'])
                else:
                    if value['summary']['context_hash'] != captured_context['context_hash']:
                        if same:
                            self._error('分析输入已经变化，请重新分析。', 'ai')
                        return
                    latest.update(summary=value['summary'], summary_context=captured_context,
                                  analysis_path=str(value['analysis_path']), analysis_export=str(value['export_dir']))
                self._save_record(key)
                # Old results can be retained in their own cache, never under a new binding.
                if same:
                    same_model = bool(self.config and all(self.config.get(k) == config[k]
                                                         for k in ('model', 'base_url')))
                    self.state['model']['connected'] = True if same_model else None
                    self.state['status'].update(ai='idle', last_error='', message=(
                        '建议已生成，请确认后再发送。' if unchanged_input else '期间消息已更新，保留了较早建议的依据；可重新生成。'))
                    self._publish_record()
                    if operation == 'reply' and place_in_wechat:
                        if unchanged_input and latest['edit_revision'] == edit_revision and native_epoch == self.native_epoch:
                            self._queue_placement(latest['drafts']['short'], captured_context['context_hash'])
                        else:
                            self.state['placement'] = {'id': uuid.uuid4().hex, 'status': 'blocked',
                                'message': '生成期间会话或草稿有变化，未自动填入。检查后可点「放入微信」。'}
                            self._changed()
            self.ai_future = self._submit(operation, args, done)
            return {'ok': True}

    def suggest_reply(self, goal='', supplement='', expected_source_id=None):
        return self._model_action('reply', expected_source_id, goal, supplement)

    def take_suggestion(self, goal='', supplement='', expected_source_id=None, expected_account=None):
        if not self.window_interaction:
            with self.lock:
                if not expected_account or expected_account != self.account:
                    return {'ok': False, 'error': '账号已变化，请在当前会话重新操作。'}
                return self._model_action('reply', expected_source_id, goal, supplement)
        return self._model_action('reply', expected_source_id, goal, supplement,
                                  place_in_wechat=True, expected_account=expected_account)

    def _queue_placement(self, value, context_hash=None):
        if not self.window_interaction:
            self.pending_placement = None
            return {'ok': False, 'error': self.WINDOW_INTERACTION_PAUSED}
        record = self._record()
        if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
            return {'ok': False, 'error': '这份草稿为空或过长。'}
        reply, context = record.get('reply'), record.get('context')
        version = 'short' if value == record['drafts']['short'] else 'full'
        original = (reply or {}).get('drafts', {}).get(version, value)
        job = {'id': uuid.uuid4().hex, 'key': self._binding_key(), 'epoch': self.epoch,
               'native_epoch': self.native_epoch, 'edit_revision': record['edit_revision'],
               'context_hash': context_hash, 'text': value, 'original': original, 'deadline': time.monotonic() + 5}
        self.pending_placement = job
        self.state['placement'] = {'id': job['id'], 'status': 'queued', 'message': '正在放入微信输入框…'}
        self._changed()
        return {'ok': True, 'queued': True}

    def put_draft(self, value, expected_source_id=None, expected_account=None):
        with self.lock:
            if not expected_account or expected_account != self.account or not self._expected(expected_source_id):
                return {'ok': False, 'error': '账号或会话已变化，请在当前会话重新操作。'}
            record = self._record()
            if value not in record['drafts'].values():
                return {'ok': False, 'error': '草稿刚刚变化，请保存修改后重新操作。'}
            return self._queue_placement(value)

    def _place_pending_draft(self, writer, tracker):
        with self.lock:
            job = self.pending_placement
            if job is None:
                return
            sources = copy.deepcopy(self.state['sources'])
            window_key = tracker.last_wechat

        def still_current():
            with self.lock:
                record = self._record()
                context = record.get('context') if record else None
                return bool(not self.stopped.is_set() and self.pending_placement is job and
                    self._binding_key() == job['key'] and self.epoch == job['epoch'] and
                    self.native_epoch == job['native_epoch'] and record and
                    record['edit_revision'] == job['edit_revision'] and
                    job['text'] in record['drafts'].values() and time.monotonic() <= job['deadline'] and
                    (job['context_hash'] is None or context and context['context_hash'] == job['context_hash']))

        result = (writer.place(job['text'], job['key'][1], sources, window_key, still_current)
                  if still_current() else {'ok': False, 'error': '会话或草稿已变化，未填入微信；请重新操作。'})
        with self.lock:
            if self.pending_placement is not job:
                return
            self.pending_placement = None
            self.state['placement'] = {'id': job['id'], 'status': 'filled' if result.get('ok') else 'blocked',
                                       'message': result.get('message') or result.get('error') or '未能填入，请重试。'}
            if result.get('ok'):
                record = self.records.get(job['key'])
                if record and record.get('context'):
                    try:
                        record['memory'] = contact_memory.observe_placement(record['memory'], record['context'],
                            job['id'], job['text'], job['original'])
                        self._save_record(job['key'])
                        self._publish_record()
                    except (ValueError, TypeError, KeyError):
                        record['memory_error'] = '填入已完成，但改稿学习记录未通过校验。'
            self._changed()

    def update_analysis(self, expected_source_id=None):
        return self._model_action('analysis', expected_source_id)

    def summarize_today(self, expected_source_id=None, expected_account=None):
        import today_summary
        with self.lock:
            if not expected_account or expected_account != self.account or not self._expected(expected_source_id):
                return {'ok': False, 'error': '账号或会话已切换，请重新总结今天。'}
            if self.state['binding']['kind'] not in today_summary.CHAT_KINDS:
                return {'ok': False, 'error': '请先选择需要总结的群聊或联系人。'}
            if self.auto_chat.public()['active_contacts']:
                self.auto_chat.pause_all(self.account)
            self._reload_config()
            if self.config is None:
                return self._error('模型配置不可用，请检查本机模型配置。', 'ai')
            day = today_summary.today()
            if not self._has_today_consent():
                token = uuid.uuid4().hex
                self.pending_consents = {token: (self._today_consent_key(), self.epoch, time.monotonic() + 300, 'today')}
                return {'ok': False, 'consent_required': True, 'consent': {
                    'token': token, 'model': self.config['model'], 'base_url': self.config['base_url'],
                    'source_name': self.state['binding']['name'],
                    'scope': f'北京时间 {day} 00:00 至本次读取时的全部可读消息；消息多时分批整理。不使用往日聊天、会话记忆或口吻样本。'}}
            if self.today_request or self.ai_request or self.style_request or self.capture_request or self.checking or any(
                    future and not future.done() for future in (self.ai_future, self.style_future, self.capture_future)):
                return {'ok': False, 'error': '上一项读取或模型请求仍在处理，请稍后重试。'}
            if self.pending_placement:
                return {'ok': False, 'error': '草稿正在填入微信，请稍后重试。'}
            key, epoch = self._binding_key(), self.epoch
            config = {**self.config, 'allow_chat_upload': True}
            token = object()
            self.today_request = self.ai_request = token
            self.state['status'].update(ai='running', last_error='', message='正在读取并总结今天的聊天…')
            self._changed()
            def done(result):
                if self.ai_request is not token:
                    return
                self.today_request = self.ai_request = self.ai_future = None
                same = self._binding_key() == key and self.epoch == epoch
                if not result['ok']:
                    if same:
                        self._error(result['error'], 'ai')
                    return
                try:
                    value = today_summary.load_result(result['value'], key[1], key[0], day, data_root=self.root.parent)
                except Exception:
                    if same:
                        self._error('当天总结未通过日期、会话或原文校验，已有总结仍保留。', 'ai')
                    return
                latest = self.records.get(key)
                if latest is None:
                    return
                previous = latest.get('today_summary')
                latest['today_summary'] = value
                if not self._save_record(key):
                    latest['today_summary'] = previous
                    if same:
                        self._error('当天总结暂时无法保存，已有成功结果仍保留，请重试。', 'ai')
                    return
                if same:
                    self._reload_config()
                    current_day = today_summary.today() == day
                    self.state['status'].update(ai='idle', last_error='', message=(
                        '当天总结已保存，可在菜单再次查看。' if current_day else '日期已变化，请重新总结今天。'))
                    self._publish_record()
                    same_model = bool(self.config and all(self.config.get(k) == config[k] for k in ('model', 'base_url')))
                    if current_day and same_model:
                        self.open_today_summary(key[1], key[0])
            try:
                self.ai_future = self._submit('today-summary', (key[1], key[0], day, config), done)
            except Exception:
                self.today_request = self.ai_request = self.ai_future = None
                return self._error('后台任务暂时不可用，请重新总结今天。', 'ai')
            return {'ok': True}

    def open_today_summary(self, expected_source_id=None, expected_account=None):
        import today_summary
        with self.lock:
            if not expected_account or expected_account != self.account or not self._expected(expected_source_id):
                return {'ok': False, 'error': '账号或会话已切换，请在当前会话查看。'}
            value = (self._record() or {}).get('today_summary')
            if not value or value['date'] != today_summary.today():
                return {'ok': False, 'error': '今天还没有总结，请先点「总结今天」。'}
            try:
                value = today_summary.load_result(value, expected_source_id, expected_account, value['date'],
                                                   data_root=self.root.parent)
                if not webbrowser.open(Path(value['html_path']).as_uri()):
                    raise OSError('Browser unavailable')
            except Exception:
                return self._error('当天总结暂时无法打开，请重新总结今天。', 'ai')
            return {'ok': True}

    def save_drafts(self, short, full, expected_source_id=None, expected_account=None):
        with self.lock:
            if expected_account is not None and expected_account != self.account:
                return {'ok': False, 'error': '账号已变化，未写入其他账号的草稿。'}
            key = (expected_account or self.account, expected_source_id)
            known_account_record = bool(expected_account and expected_account == self.account and key in self.records)
            if not known_account_record and not self._expected(expected_source_id):
                return {'ok': False, 'error': '会话已切换，未覆盖新会话草稿。'}
            if not all(isinstance(s, str) and len(s) <= 20000 for s in (short, full)):
                return {'ok': False, 'error': '草稿内容过长或格式无效。'}
            record = self.records[key]
            if key == self._binding_key() and self.auto_chat.public()['enabled']:
                self.auto_chat.pause_all(self.account)
            record['drafts'] = {'short': short, 'full': full}
            record['edit_revision'] += 1
            persisted = self._save_record(key)
            if key == self._binding_key():
                self._publish_record()
            return {'ok': True} if persisted else {'ok': False, 'error': '草稿暂未保存，请保留窗口并重试。'}

    def set_collapsed(self, collapsed):
        with self.lock:
            self.state['settings']['collapsed'] = bool(collapsed)
            if not collapsed:
                self._request_check()
            self._publish_sync()
            self._changed()
            return {'ok': True}

    def set_startup_state(self, enabled):
        with self.lock:
            self.state['settings']['startup'] = bool(enabled)
            self._changed()

    def open_dashboard(self):
        with self.lock:
            if self.dashboard_opening or self.dashboard_future and not self.dashboard_future.done():
                return {'ok': True}
            if not self.index_export:
                return {'ok': False, 'error': '请先读取本机记录。'}
            existing = self.dashboard_servers.get(self.account)
            if existing:
                webbrowser.open(f'http://127.0.0.1:{existing[0].server_address[1]}/#information')
                return {'ok': True}
            export_dir, account = self.index_export, self.account
            output = str(self.root / '微信聊天看板.html')
            # The full dashboard uses its own complete export, not reply-draft storage.
            self.dashboard_opening = True
            def ready(result):
                if not result['ok']:
                    self.dashboard_opening = False
                    return self._error(result['error'])
                threading.Thread(target=self._launch_dashboard, args=(export_dir, account), daemon=True).start()
            self.dashboard_future = self._submit('dashboard', (export_dir, None, output), ready)
            return {'ok': True}

    def _launch_dashboard(self, export_dir, account):
        server = service = None
        try:
            import dashboard
            from information_assistant import InformationService
            service = InformationService(export_dir, data_root=self.root.parent,
                config_loader=lambda: self.config_loader(self.config_path))
            if service.account != account:
                raise ValueError('Account changed')
            server = dashboard.create_server(export_dir, port=0, information_service=service)
            with self.lock:
                if self.stopped.is_set() or self.account != account:
                    server.server_close()
                    service.close()
                    return
                self.dashboard_servers[account] = (server, service)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                webbrowser.open(f'http://127.0.0.1:{server.server_address[1]}/#information')
        except Exception:
            if server:
                server.server_close()
            if service:
                service.close()
            with self.lock:
                self._error('信息看板未能打开，请更新本机记录后重试。')
        finally:
            with self.lock:
                self.dashboard_opening = False

    def _watch(self):
        if not self.window_interaction:
            # Preserve file-based synchronization; never import/construct a
            # window tracker, place a draft, or tick persisted auto-chat jobs.
            from assistant_worker import source_signature
            signature_reader = self.signature_reader or source_signature
            while not self.stopped.wait(1):
                self._check_updates(signature_reader)
            return
        tracker = None
        try:
            from assistant_worker import source_signature
            if self.native_factory:
                tracker, writer, automatic_sender = self.native_factory()
            else:
                from conversation_tracker import Tracker
                from wechat_draft import WeChatDraftWriter
                from wechat_auto_send import WeChatAutoSender
                tracker = self.tracker_factory() if self.tracker_factory else Tracker()
                writer = WeChatDraftWriter()
                automatic_sender = WeChatAutoSender(writer)
            signature_reader = self.signature_reader or source_signature
            while not self.stopped.wait(1 if self.native_factory else .2):
                with self.lock:
                    sources = copy.deepcopy(self.state['sources'])
                    following = self.state['binding']['mode'] == 'auto'
                if sources and (following or self.pending_placement is not None or self.automatic_chat):
                    result = tracker.sample(sources)
                    with self.lock:
                        native_binding = (tracker.last_wechat, result.get('source_id') if result.get('status') == 'matched' else None)
                        if native_binding != self.native_binding:
                            self.native_binding = native_binding
                            self.native_epoch += 1
                    if following:
                        self.apply_tracking(result)
                self._place_pending_draft(writer, tracker)
                self._check_updates(signature_reader)
                if self.automatic_chat:
                    self.auto_chat.tick(automatic_sender, tracker)
        finally:
            if tracker and hasattr(tracker, 'close'):
                tracker.close()

    def close(self):
        self.stopped.set()
        self.auto_chat.close()
        for server, service in tuple(self.dashboard_servers.values()):
            service.close()
            server.shutdown()
            server.server_close()
        self.dashboard_servers.clear()
        # Python 3.12 has no public ProcessPoolExecutor.terminate_workers; ensure
        # closing an interactive window does not wait for a 120 s model timeout.
        processes = tuple((getattr(self.pool, '_processes', None) or {}).values())
        self.pool.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            if process.is_alive():
                process.terminate()
