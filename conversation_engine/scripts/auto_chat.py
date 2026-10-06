"""Opt-in, foreground private-chat automation. No native input in this module."""
from datetime import datetime, timedelta
import copy
import json
import os
from pathlib import Path
import re
import time
import uuid

VERSION = 'auto-chat/1.0.1'
SETTLE_SECONDS = 5
READY_SECONDS = 2
MAX_AGE_SECONDS = 300
MAX_HOURLY = 12


def now():
    return datetime.now().astimezone()


def private(source):
    return source.get('kind') in ('私聊', 'private', 'contact', 'friend')


def instant(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('Missing timezone')
    return stamp


def guard_text(text):
    """A local veto, independent of a model's self-assessment."""
    from export_recent import redact
    if not isinstance(text, str) or not text.strip() or len(text) > 500 or redact(text) != text:
        return '文字长度或敏感内容需要你检查。'
    if re.search(r'https?://|www\.|\[已隐藏|验证码|密码|身份证|银行卡|转账|付款|支付|借钱|收款|合同|签约|保证|承诺|我答应|我负责|我会|一定会|必须去|住址|具体地址|自杀|自残|用药|诊断', text, re.I):
        return '这句涉及敏感内容、决定或承诺，请你接管。'
    if re.search(r'(明天|后天|周[一二三四五六日天]|\d+点).{0,12}(见|约|去|来|接|开会)|忽略.{0,8}(规则|指令)|自动.{0,6}(发送|转发)', text):
        return '这句涉及安排或要求改变助手规则，请你接管。'
    return None


class AutoChat:
    def __init__(self, controller):
        self.c = controller
        self.path = controller.root / 'automatic-chat.json'
        self.entries, self.attempts, self.observed = {}, [], {}
        self.job, self.revision, self.error, self.lock_file = None, 0, '', None
        self.loaded_bytes = None
        try:
            if self.path.exists():
                if self.path.stat().st_size > 1_000_000:
                    raise ValueError('Oversize')
                from ai_client import strict_json
                self.loaded_bytes = self.path.read_bytes()
                value = strict_json(self.loaded_bytes.decode('utf-8'))
                if set(value) != {'schema', 'contacts', 'attempts'} or value['schema'] != 1:
                    raise ValueError('Schema')
                entries, attempts = value['contacts'], value['attempts']
                if not isinstance(entries, dict) or len(entries) > 128 or not isinstance(attempts, list) or len(attempts) > 120:
                    raise ValueError('Bounds')
                for key, entry in entries.items():
                    if (not isinstance(entry, dict) or set(entry) != {'account', 'source_id', 'enabled', 'service', 'reason'} or
                            not re.fullmatch(r'[a-f0-9]{64}', entry['account']) or
                            not re.fullmatch(r'C_[a-f0-9]{8}', entry['source_id']) or
                            key != self.key((entry['account'], entry['source_id'])) or type(entry['enabled']) is not bool or
                            not re.fullmatch(r'[a-f0-9]{64}', entry['service']) or
                            not isinstance(entry['reason'], str) or len(entry['reason']) > 200):
                        raise ValueError('Contact')
                for entry in attempts:
                    if (not isinstance(entry, dict) or set(entry) != {'id', 'account', 'source_id', 'time', 'text', 'uids', 'status'} or
                            not re.fullmatch(r'[a-f0-9]{32}', entry['id']) or
                            not re.fullmatch(r'[a-f0-9]{64}', entry['account']) or
                            not re.fullmatch(r'C_[a-f0-9]{8}', entry['source_id']) or
                            not isinstance(entry['text'], str) or not 0 < len(entry['text']) <= 500 or
                            entry['status'] not in ('attempted', 'recorded', 'unknown', 'acknowledged') or
                            not isinstance(entry['uids'], list) or not 1 <= len(entry['uids']) <= 80 or
                            any(not isinstance(uid, str) or not re.fullmatch(r'W_[a-f0-9]{32}', uid) for uid in entry['uids'])):
                        raise ValueError('Attempt')
                    instant(entry['time'])
                self.entries, self.attempts = entries, attempts
        except (OSError, ValueError, TypeError, KeyError):
            self.error = '自动聊天记录未通过校验，请保留文件并检查。'

    def key(self, key=None):
        from assistant_controller import fingerprint
        return fingerprint(key or self.c._binding_key())

    def service(self):
        from assistant_controller import fingerprint
        return fingerprint([self.c.config['base_url'], self.c.config['model']]) if self.c.config else ''

    def _claim(self):
        if self.lock_file:
            return True
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Both desktop implementations share one send lock, even though
            # their drafts and opt-in settings live in separate directories.
            lock_root = self.c.root.parent / 'assistant'
            lock_root.mkdir(parents=True, exist_ok=True)
            handle = (lock_root / 'automatic-chat.lock').open('a+b')
            if handle.seek(0, 2) == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (self.path.read_bytes() if self.path.exists() else None) != self.loaded_bytes:
                raise OSError('Automatic chat settings changed in another window')
            self.lock_file = handle
            return True
        except (OSError, ImportError):
            if handle:
                handle.close()
            return False

    def _save(self):
        if self.error or not self._claim():
            raise OSError('Automatic chat unavailable')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.pending')
        try:
            with pending.open('w', encoding='utf-8') as handle:
                json.dump({'schema': 1, 'contacts': self.entries, 'attempts': self.attempts}, handle,
                          ensure_ascii=False, allow_nan=False)
                handle.flush(); os.fsync(handle.fileno())
            pending.replace(self.path)
            self.loaded_bytes = self.path.read_bytes()
        finally:
            pending.unlink(missing_ok=True)
        self.revision += 1
        self.c._changed()

    def public(self):
        key = self.c._binding_key()
        entry = self.entries.get(self.key(key), {})
        job = self.job if self.job and self.job['key'] == key else None
        source = next((s for s in self.c.state['sources'] if s['id'] == key[1]), {})
        return {'source_id': key[1], 'account_fingerprint': key[0], 'enabled': entry.get('enabled', False),
                'supported': private(source), 'status': job['stage'] if job else 'armed' if entry.get('enabled') else 'off',
                'reason': self.error or entry.get('reason', ''), 'revision': self.revision,
                'active_contacts': sum(e['enabled'] for e in self.entries.values() if e['account'] == key[0]),
                'history': [copy.deepcopy(a) for a in self.attempts if (a['account'], a['source_id']) == key][-8:]}

    def set_enabled(self, enabled, sid, account):
        if type(enabled) is not bool or account != self.c.account or not account or not self.c._expected(sid):
            return {'ok': False, 'error': '账号或会话已变化，请重新打开自动聊天。'}
        record = self.c._record()
        if enabled and (not record or not record.get('context') or not private(record['context']['source'])):
            return {'ok': False, 'error': '先打开已识别的私聊；群聊和公众号暂不自动发送。'}
        self.c._reload_config()
        if enabled and (not self.c.config or not self.c._has_consent()):
            return {'ok': False, 'error': '请先通过普通生成确认当前模型可用且已授权。'}
        if enabled and (self.c.ai_request or self.c.style_request or self.c.pending_placement):
            return {'ok': False, 'error': '当前操作还在处理，完成后再启用自动聊天。'}
        previous = copy.deepcopy(self.entries)
        if self.key() not in self.entries and len(self.entries) >= 128:
            return {'ok': False, 'error': '自动联系人设置已达到上限，请先整理已有设置。'}
        previous_attempts = copy.deepcopy(self.attempts)
        if enabled:
            for attempt in self.attempts:
                if (attempt['account'], attempt['source_id']) == (account, sid) and attempt['status'] in ('attempted', 'unknown'):
                    attempt['status'] = 'acknowledged'
        self.entries[self.key()] = {'account': account, 'source_id': sid, 'enabled': enabled,
                                    'service': self.service() or self.entries.get(self.key(), {}).get('service', '0' * 64),
                                    'reason': '' if enabled else '已暂停，由你回复。'}
        try:
            self._save()
        except OSError:
            self.entries = previous
            self.attempts = previous_attempts
            return {'ok': False, 'error': '自动聊天未能保存，或另一个窗口正在运行；未启用。'}
        if self.job and self.job['key'] == self.c._binding_key():
            self.job = None
        self.observed.pop(self.key(), None)  # Arm only messages arriving after this click.
        if record and record.get('context'):
            self.observe(record['context'], record.get('excluded_count', 0))
        self.c._changed()
        return {'ok': True, 'automatic': self.public()}

    def pause_all(self, account):
        if account != self.c.account or not account:
            return {'ok': False, 'error': '账号已变化，未修改其他账号。'}
        previous = copy.deepcopy(self.entries)
        for entry in self.entries.values():
            if entry['account'] == account:
                entry.update(enabled=False, reason='已暂停，由你回复。')
        if previous == self.entries:
            return {'ok': True}
        try:
            self._save()
        except OSError:
            # The in-memory stop still takes effect; it must not claim persistence.
            self.error = '已在本窗口暂停，但未能保存；请关闭助手后检查。'
        self.job = None
        self.c._changed()
        return {'ok': True, 'automatic': self.public()}

    def _hold(self, reason, key=None):
        key = key or self.c._binding_key()
        entry = self.entries.get(self.key(key))
        if entry:
            entry.update(enabled=False, reason=reason[:200])
        if self.job and self.job['key'] == key:
            self.job = None
        try:
            self._save()
        except OSError:
            self.error = '已暂停，但自动聊天状态未能保存。'
        self.c._changed()

    def observe(self, context, excluded_count=0):
        key = (context['account_fingerprint'], context['source']['id'])
        hashed = self.key(key)
        previous = self.observed.get(hashed)
        messages = context['messages']
        observed = {'uids': {r['uid'] for r in messages}, 'excluded': excluded_count,
                    'context_hash': context['context_hash']}
        entry = self.entries.get(hashed)
        if not entry or not entry['enabled'] or key != self.c._binding_key():
            self.observed[hashed] = observed
            return
        verifying = bool(self.job and self.job['key'] == key and self.job['stage'] == 'verifying')
        confirmed_uid = None
        if verifying:
            job = self.job
            matched = [r for r in messages if r['uid'] not in job['baseline'] and r['is_self'] is True and
                       r['kind'] == 'text' and r['text'] == job['text'] and
                       instant(job['attempt_time']) - timedelta(seconds=2) <= instant(r['time']) <= now()]
            if len(matched) == 1:
                next(a for a in self.attempts if a['id'] == job['id'])['status'] = 'recorded'
                try:
                    self._save()
                except OSError:
                    self._hold('微信出现回复，但核对记录未能保存，请你检查。'); return
                confirmed_uid = matched[0]['uid']
                self.job = None
                self.c._changed()
        if previous is not None and excluded_count > previous['excluded']:
            self.observed[hashed] = observed
            self._hold('出现图片、语音或其他未解析内容，请你接管。'); return
        if verifying and confirmed_uid is None:
            # Leave follow-ups unconsumed until delivery is confirmed. Media still
            # hands off immediately, and tick keeps the original no-retry timeout.
            if previous is not None:
                self.observed[hashed] = {**previous, 'excluded': excluded_count}
            return
        self.observed[hashed] = observed
        if any(a['status'] in ('attempted', 'unknown') for a in self.attempts if (a['account'], a['source_id']) == key):
            self._hold('有一次发送结果未确认，请先查看微信，再重新启用。'); return
        if previous is None:
            # On restart or first visit, old messages are a baseline, never catch-up sends.
            return
        # Only the confirmed automatic echo is exempt from manual-reply takeover.
        added = [r for r in messages if r['uid'] not in previous['uids'] and r['uid'] != confirmed_uid]
        if any(r['is_self'] is True for r in added):
            self._hold('你已在微信回复，自动聊天暂停，交还给你。')
            return
        incoming = [r for r in added if r['is_self'] is False]
        if not incoming:
            if self.job and self.job['context_hash'] != context['context_hash']:
                self.job = None
            return
        if not private(context['source']) or any(r['kind'] != 'text' or r.get('quoted_content') for r in incoming):
            self._hold('新消息包含卡片、引用或无法判断的内容，请你接管。'); return
        if any(not r['sender'].strip() or r['sender'] in ('身份未解析', '未知发送人', '未知') for r in incoming):
            self._hold('无法确认新消息的发送人，请你接管。'); return
        if any(guard_text(r['text']) for r in incoming):
            self._hold('对方这句涉及需要你确认的内容，请你接管。'); return
        age = (now() - instant(incoming[-1]['time'])).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            self._hold('这条消息已超过五分钟或时间异常，请你确认后回复。'); return
        self.job = {'id': uuid.uuid4().hex, 'key': key, 'epoch': self.c.epoch, 'stage': 'settling',
                    'due': time.monotonic() + SETTLE_SECONDS, 'context_hash': context['context_hash'],
                    'uids': [r['uid'] for r in incoming], 'incoming_time': incoming[-1]['time'],
                    'baseline': {r['uid'] for r in messages}}
        self.c._changed()

    def _valid(self, job):
        record = self.c._record()
        entry = self.entries.get(self.key(job['key']), {})
        return bool(self.job is job and not self.error and not self.c.stopped.is_set() and
                    entry.get('enabled') and job['key'] == self.c._binding_key() and job['epoch'] == self.c.epoch and
                    record and record.get('context') and (job['stage'] == 'verifying' or
                    record['context']['context_hash'] == job['context_hash']))

    def tick(self, sender, tracker):
        with self.c.lock:
            job = self.job
            if not job:
                return
            if not self._valid(job):
                self.job = None; self.c._changed(); return
            if job['stage'] == 'verifying':
                if time.monotonic() > job['verify_deadline']:
                    next(a for a in self.attempts if a['id'] == job['id'])['status'] = 'unknown'
                    self._hold('发送结果尚未从记录核对，不会重发；请你检查微信。')
                return
            if (now() - instant(job['incoming_time'])).total_seconds() > MAX_AGE_SECONDS:
                self._hold('消息已等待超过五分钟，请你确认后回复。'); return
            if job['stage'] == 'generating' or time.monotonic() < job['due']:
                return
            if self.c.capture_request or job['key'] in self.c.context_requests:
                return
            self.c._reload_config()
            if not self.c.config or not self.c._has_consent() or self.entries[self.key()]['service'] != self.service():
                self._hold('模型配置或授权已变化，请你确认后重新启用。'); return
            if self.c.native_binding != (tracker.last_wechat, job['key'][1]) or not tracker.last_wechat:
                return
            if not self._claim():
                self.error = '另一个窗口正在自动聊天，本窗口已停止。'; self.c._changed(); return
            if job['stage'] == 'settling':
                if self.c.ai_request or self.c.style_request or self.c.pending_placement:
                    return
                if not sender.available(tracker.last_wechat):
                    return
                recent = [a for a in self.attempts if a['account'] == self.c.account and
                          now() - timedelta(hours=1) < instant(a['time'])]
                if len(recent) >= 30 or sum(a['source_id'] == job['key'][1] for a in recent) >= MAX_HOURLY:
                    self._hold('已达到自动回复频率上限，请你接管。'); return
                self._generate(job)
                return
            if job['stage'] != 'ready' or not sender.available(tracker.last_wechat):
                return
            record = self.c._record()
            if record['edit_revision'] != job['edit_revision']:
                self._hold('你已经编辑了草稿，请你接管。'); return
            window_key, sources = tracker.last_wechat, copy.deepcopy(self.c.state['sources'])

        def current():
            with self.c.lock:
                self.c._reload_config()
                valid = (self._valid(job) and self.c._record()['edit_revision'] == job['edit_revision'] and
                        self.c.native_binding == (window_key, job['key'][1]) and self.service() == job['service'] and
                        self.c.native_epoch == job['native_epoch'] and self._settings_valid(job) and self.c._has_consent())
                if not valid:
                    return False
                try:
                    from assistant_worker import source_signature
                    expected = self.c._sync_record(job['key'])['signature']
                    fresh = (expected is not None and not self.c.capture_request and job['key'] not in self.c.context_requests and
                             (self.c.signature_reader or source_signature)() == expected)
                except Exception:
                    fresh = False
                if not fresh:
                    self.c._request_check()
                return fresh

        def record_attempt():
            with self.c.lock:
                if not current():
                    return False
                job['attempt_time'] = now().isoformat(timespec='seconds')
                self.attempts = [*self.attempts, {'id': job['id'], 'account': job['key'][0], 'source_id': job['key'][1],
                    'time': job['attempt_time'], 'text': job['text'], 'uids': job['uids'], 'status': 'attempted'}][-120:]
                try:
                    self._save()
                except OSError:
                    self._hold('发送前记录未能保存，未调用发送。'); return False
                job.update(stage='verifying', verify_deadline=time.monotonic() + 30)
                self.c._changed()
                return True

        result = sender.send(job['text'], job['key'][1], sources, window_key, current, record_attempt)
        with self.c.lock:
            if self.job is job and not result.get('ok'):
                self._hold(result.get('error', '无法确认发送，请你接管。'), job['key'])
            self.c._request_check()

    def _generate(self, job):
        c = self.c
        record = c._record()
        try:
            import contact_memory
            import personal_style
            memory = contact_memory.for_reply(record['memory'], record['context'])
            profile = personal_style.load_profile(c.root.parent, c.account)
            job.update(stage='generating', edit_revision=record['edit_revision'], service=self.service(),
                       memory_revision=record['memory']['settings_revision'], scene=record.get('scene', 'auto'))
            config = copy.deepcopy(c.config)
            config['allow_chat_upload'] = True  # The exact account/source/service grant was checked by tick.
            args = (copy.deepcopy(record['context']), config, '', '', [], profile, job['scene'], memory)
        except (ValueError, TypeError, KeyError):
            self._hold('口吻或会话记忆不可用，请你接管。'); return
        request = {'auto_chat': job['id']}
        from assistant_controller import fingerprint
        job.update(profile_key=fingerprint(profile), native_epoch=c.native_epoch)
        c.ai_request = request
        c.state['status'].update(ai='running', message='正在自动拟回复…', last_error='')
        c._changed()

        def done(result):
            if c.ai_request is request:
                c.ai_request = None; c.ai_future = None
                c.state['status']['ai'] = 'idle'; c._changed()
            if not self._valid(job):
                return
            latest = c._record()
            if (latest['edit_revision'] != job['edit_revision'] or not self._settings_valid(job) or
                    c.native_epoch != job['native_epoch'] or self.service() != job['service']):
                self._hold('期间草稿、口吻或模型发生变化，请你接管。'); return
            if not result.get('ok'):
                c.state['model']['connected'] = False
                self._hold('模型未完成自动回复，请你接管或稍后重启自动聊。'); return
            try:
                from reply_assistant import _validate_reply, validate_memory_packet
                value = result['value']
                if value['source_id'] != job['key'][1] or value['context_hash'] != job['context_hash']:
                    raise ValueError('Identity')
                evidence = {r['uid'] for r in args[0]['messages']} | validate_memory_packet(memory, args[0])
                _validate_reply({k: value[k] for k in ('facts', 'inferences', 'questions', 'drafts')}, evidence)
                decision = value['automatic']
                if set(decision) != {'action', 'reason'} or decision['action'] not in ('reply', 'handoff', 'wait') or not isinstance(decision['reason'], str) or not 0 < len(decision['reason']) <= 200:
                    raise ValueError('Decision')
            except (KeyError, TypeError, ValueError):
                self._hold('模型输出未通过自动回复校验，请你接管。'); return
            c.state['model']['connected'] = True
            if decision['action'] == 'wait':
                self.job = None; c._changed(); return
            latest.update(reply=value, reply_context=args[0], reply_key=None, drafts=copy.deepcopy(value['drafts']))
            if not c._save_record(job['key']):
                self._hold('回复未能保存，未发送。'); return
            c._publish_record()
            veto = guard_text(value['drafts']['short'])
            if decision['action'] != 'reply' or value['questions'] or veto:
                self._hold(veto or decision['reason']); return
            job.update(stage='ready', due=time.monotonic() + READY_SECONDS, text=value['drafts']['short'])
            c._changed()

        try:
            c.ai_future = c._submit('auto-reply', args, done)
        except Exception:
            c.ai_request = None; c.ai_future = None; c.state['status']['ai'] = 'idle'
            self._hold('自动回复任务未能启动，请你接管。')

    def _settings_valid(self, job):
        from assistant_controller import fingerprint
        import personal_style
        try:
            record = self.c._record()
            return (record['memory']['settings_revision'] == job['memory_revision'] and
                    record.get('scene', 'auto') == job['scene'] and
                    fingerprint(personal_style.load_profile(self.c.root.parent, self.c.account)) == job['profile_key'])
        except (ValueError, TypeError, KeyError):
            return False

    def close(self):
        self.job = None
        if self.lock_file:
            self.lock_file.close(); self.lock_file = None
