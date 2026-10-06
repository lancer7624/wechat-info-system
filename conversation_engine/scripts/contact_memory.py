"""Bounded, evidence-backed memory and confirmed sent edits for one conversation."""
import copy
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import re
import unicodedata

VERSION = 'contact-memory/1.0.0'
MAX_ROWS, MAX_CHARS, MAX_PINS = 240, 24000, 12
MAX_EXAMPLES, MAX_SNIPPETS, SNIPPET_CHARS = 8, 12, 6000
FIELDS = {'schema_version', 'account_fingerprint', 'source_id', 'revision', 'settings_revision',
          'enabled', 'feedback_enabled', 'notes', 'messages', 'pins', 'hidden_uids',
          'clear_before', 'snapshot_at', 'pending', 'examples'}


class MemoryError(ValueError):
    """A finite public message; never includes chat text or private paths."""


def require(condition, message='会话记忆格式无效，已有内容仍保留。'):
    if not condition:
        raise MemoryError(message)


def instant(value):
    try:
        require(isinstance(value, str) and len(value) <= 64)
        result = datetime.fromisoformat(value)
        require(result.tzinfo is not None)
        return result
    except (ValueError, TypeError):
        raise MemoryError('会话记忆日期无效。') from None


def now():
    return datetime.now().astimezone()


def empty(account, source_id):
    require(isinstance(account, str) and re.fullmatch(r'[0-9a-f]{64}', account) and
            isinstance(source_id, str) and re.fullmatch(r'C_[0-9a-f]{8}', source_id), '请先选择本人账号的会话。')
    return {'schema_version': 1, 'account_fingerprint': account, 'source_id': source_id,
            'revision': 0, 'settings_revision': 0, 'enabled': True, 'feedback_enabled': True,
            'notes': '', 'messages': [], 'pins': [], 'hidden_uids': [], 'clear_before': None,
            'snapshot_at': None, 'pending': None, 'examples': []}


def rows_context(rows, account, source_id, snapshot_at, source=None):
    from reply_assistant import _context_hash
    snapshot = instant(snapshot_at)
    dates = [instant(row['time']) for row in rows]
    require(all(date <= snapshot for date in dates))
    context = {'account_fingerprint': account, 'source': source or {'id': source_id, 'name': '会话记忆', 'kind': '历史原文'},
               'messages': copy.deepcopy(rows), 'snapshot_at': snapshot_at,
               'start': min(dates).isoformat() if dates else snapshot_at, 'end': snapshot_at,
               'count': len(rows), 'total': len(rows), 'truncated': False}
    context['context_hash'] = _context_hash(context)
    return context


def validate(value, account, source_id):
    from reply_assistant import _validate_context
    empty(account, source_id)
    require(isinstance(value, dict) and set(value) == FIELDS)
    require(value['schema_version'] == 1 and type(value['schema_version']) is int and
            (value['account_fingerprint'], value['source_id']) == (account, source_id), '记忆与当前账号或会话不一致。')
    require(all(type(value[k]) is int and value[k] >= 0 for k in ('revision', 'settings_revision')) and
            value['revision'] >= value['settings_revision'])
    require(all(type(value[k]) is bool for k in ('enabled', 'feedback_enabled')) and
            isinstance(value['notes'], str) and len(value['notes']) <= 500 and '\x00' not in value['notes'])
    for name in ('clear_before', 'snapshot_at'):
        if value[name] is not None:
            instant(value[name])
    rows = value['messages']
    require(isinstance(rows, list) and len(rows) <= MAX_ROWS and
            sum(len(r.get('text', '')) for r in rows if isinstance(r, dict) and isinstance(r.get('text'), str)) <= MAX_CHARS)
    try:
        if rows:
            require(value['snapshot_at'] is not None and all(r['kind'] == 'text' and
                    0 < len(r['text']) <= 1200 and not r.get('quoted_content') for r in rows))
            _validate_context(rows_context(rows, account, source_id, value['snapshot_at']))
    except (ValueError, TypeError, KeyError):
        raise MemoryError('记住的原文未通过校验，已有内容仍保留。') from None
    uids = {r['uid'] for r in rows}
    for name, limit in (('pins', MAX_PINS), ('hidden_uids', 512)):
        ids = value[name]
        require(isinstance(ids, list) and len(ids) <= limit and
                all(isinstance(uid, str) and re.fullmatch(r'W_[a-f0-9]{32}', uid) for uid in ids) and len(set(ids)) == len(ids))
    require(set(value['pins']) <= uids and not uids.intersection(value['hidden_uids']))
    examples = value['examples']
    require(isinstance(examples, list) and len(examples) <= MAX_EXAMPLES)
    seen = set()
    for entry in examples:
        require(isinstance(entry, dict) and set(entry) == {'message', 'proposal', 'snapshot_at', 'matched_at'})
        instant(entry['matched_at'])
        require(isinstance(entry['proposal'], str) and 0 < len(entry['proposal']) <= 500 and _safe_text(entry['proposal']))
        try:
            row = entry['message']
            require(_safe_sample(row) and row['uid'] not in seen)
            _validate_context(rows_context([row], account, source_id, entry['snapshot_at']))
            seen.add(row['uid'])
        except (ValueError, TypeError, KeyError):
            raise MemoryError('已发送改稿未通过校验，已有内容仍保留。') from None
    pending = value['pending']
    if pending is not None:
        require(isinstance(pending, dict) and set(pending) == {'id', 'placed_at', 'expires_at', 'snapshot_at',
                                                             'baseline_uids', 'proposal', 'original'})
        require(isinstance(pending['id'], str) and re.fullmatch(r'[a-f0-9]{32}', pending['id']))
        require(instant(pending['placed_at']) < instant(pending['expires_at']) <= instant(pending['placed_at']) + timedelta(minutes=15))
        instant(pending['snapshot_at'])
        ids = pending['baseline_uids']
        require(isinstance(ids, list) and len(ids) <= 500 and
                all(isinstance(uid, str) and re.fullmatch(r'W_[a-f0-9]{32}', uid) for uid in ids) and len(ids) == len(set(ids)))
        require(all(isinstance(pending[k], str) and 0 < len(pending[k]) <= 500 and _safe_text(pending[k])
                    for k in ('proposal', 'original')))
    return copy.deepcopy(value)


def _safe_text(text):
    from export_recent import redact
    return bool(text.strip() and '\x00' not in text and redact(text) == text and
                not re.search(r'https?://|www\.|\[已隐藏', text, re.I))


def _safe_sample(row):
    return (isinstance(row, dict) and row.get('is_self') is True and row.get('kind') == 'text' and
            not row.get('quoted_content') and isinstance(row.get('text'), str) and
            6 <= len(row['text'].strip()) <= 500 and _safe_text(row['text']))


def _normal(text):
    return ''.join(re.findall(r'[\w\u3400-\u9fff]', unicodedata.normalize('NFKC', text).lower()))


def preferences(value, changes, expected_revision):
    result = validate(value, value['account_fingerprint'], value['source_id'])
    require(type(expected_revision) is int and result['settings_revision'] == expected_revision,
            '会话偏好已变化，请重新打开后再保存。')
    require(isinstance(changes, dict) and set(changes) == {'enabled', 'feedback_enabled', 'notes'} and
            all(type(changes[k]) is bool for k in ('enabled', 'feedback_enabled')) and
            isinstance(changes['notes'], str) and len(changes['notes']) <= 500 and '\x00' not in changes['notes'],
            '这个人的口吻偏好不超过 500 字。')
    from export_recent import redact
    require(redact(changes['notes']) == changes['notes'], '口吻偏好中含凭据，请移除后再保存。')
    result.update(changes, notes=changes['notes'].strip(), revision=result['revision'] + 1,
                  settings_revision=result['settings_revision'] + 1)
    if not result['feedback_enabled']:
        result['pending'] = None
    return result


def _bounded(rows, pins):
    ordered = sorted(rows, key=lambda r: (instant(r['time']), r['uid']), reverse=True)
    selected, used = [], 0
    for row in sorted(ordered, key=lambda r: r['uid'] not in pins):
        if len(selected) < MAX_ROWS and used + len(row['text']) <= MAX_CHARS:
            selected.append(row)
            used += len(row['text'])
    return sorted(selected, key=lambda r: (instant(r['time']), r['uid']))


def ingest(value, context):
    from reply_assistant import _validate_context
    result = validate(value, value['account_fingerprint'], value['source_id'])
    try:
        _validate_context(context)
        require((context['account_fingerprint'], context['source']['id']) ==
                (result['account_fingerprint'], result['source_id']), '记忆读取到了其他账号或会话，未保存。')
    except (ValueError, TypeError, KeyError):
        raise MemoryError('会话原文未通过校验，未更新记忆。') from None
    if result['snapshot_at'] and instant(context['snapshot_at']) < instant(result['snapshot_at']):
        return result
    previous = copy.deepcopy(result)
    current = {row['uid']: row for row in context['messages']}
    result['examples'] = [entry for entry in result['examples'] if entry['message']['uid'] not in current or
                          current[entry['message']['uid']] == entry['message']]
    pending = result['pending']
    if result['feedback_enabled'] and pending:
        after = max(instant(pending['snapshot_at']), instant(pending['placed_at']) - timedelta(seconds=2))
        sent = [row for row in context['messages'] if row['is_self'] is True and
                row['uid'] not in pending['baseline_uids'] and after < instant(row['time']) <= instant(pending['expires_at'])]
        if sent:
            # More than one new self message may be a split reply or an unrelated message.
            # Only one clear, sufficiently similar final message establishes an example.
            if len(sent) == 1 and _safe_sample(sent[0]):
                row = sent[0]
                before, after_text = _normal(pending['proposal']), _normal(row['text'])
                if (row['uid'] not in result['hidden_uids'] and len(before) >= 6 and len(after_text) >= 6 and
                        SequenceMatcher(None, before, after_text, autojunk=False).ratio() >= .80 and
                        row['text'].strip() != pending['original'].strip()):
                    example = {'message': copy.deepcopy(row), 'proposal': pending['original'],
                               'snapshot_at': context['snapshot_at'], 'matched_at': now().isoformat(timespec='seconds')}
                    result['examples'] = [e for e in result['examples'] if e['message']['uid'] != row['uid']]
                    result['examples'] = [*result['examples'], example][-MAX_EXAMPLES:]
            result['pending'] = None
        elif instant(context['snapshot_at']) >= instant(pending['expires_at']):
            result['pending'] = None
    if result['enabled']:
        rows = {row['uid']: row for row in result['messages']}
        hidden = set(result['hidden_uids'])
        for row in context['messages']:
            if row['uid'] in rows and rows[row['uid']] != row:
                rows.pop(row['uid'])
                result['examples'] = [e for e in result['examples'] if e['message']['uid'] != row['uid']]
            if (row['kind'] == 'text' and 0 < len(row['text']) <= 1200 and not row.get('quoted_content') and
                    row['uid'] not in hidden and (result['clear_before'] is None or instant(row['time']) > instant(result['clear_before']))):
                rows[row['uid']] = copy.deepcopy(row)
        result['messages'] = _bounded(list(rows.values()), set(result['pins']))
        result['pins'] = [uid for uid in result['pins'] if any(row['uid'] == uid for row in result['messages'])]
    if result['messages'] or result['pending'] or result['examples']:
        result['snapshot_at'] = context['snapshot_at']
    # A capture time changing by itself does not change reply input or the public revision.
    if any(result[k] != previous[k] for k in ('messages', 'pins', 'examples', 'pending')):
        result['revision'] += 1
    return validate(result, result['account_fingerprint'], result['source_id'])


def observe_placement(value, context, job_id, proposal, original, placed_at=None):
    result = validate(value, value['account_fingerprint'], value['source_id'])
    if not result['feedback_enabled'] or not all(isinstance(t, str) and 6 <= len(t) <= 500 and _safe_text(t)
                                                for t in (proposal, original)):
        return result
    from reply_assistant import _validate_context
    _validate_context(context)
    require((context['account_fingerprint'], context['source']['id']) ==
            (result['account_fingerprint'], result['source_id']))
    placed = instant(placed_at) if placed_at else now()
    result['pending'] = {'id': job_id, 'placed_at': placed.isoformat(timespec='seconds'),
        'expires_at': (placed + timedelta(minutes=15)).isoformat(timespec='seconds'),
        'snapshot_at': context['snapshot_at'], 'baseline_uids': [r['uid'] for r in context['messages']],
        'proposal': proposal, 'original': original}
    result['revision'] += 1
    return validate(result, result['account_fingerprint'], result['source_id'])


def change_item(value, uid, action, expected_revision):
    result = validate(value, value['account_fingerprint'], value['source_id'])
    require(type(expected_revision) is int and result['settings_revision'] == expected_revision, '会话记忆已变化，请重新打开。')
    require(isinstance(uid, str) and re.fullmatch(r'W_[a-f0-9]{32}', uid) and action in ('pin', 'unpin', 'forget', 'forget_example'))
    if action == 'forget_example':
        require(any(e['message']['uid'] == uid for e in result['examples']), '这条改稿已变化，请重新打开。')
        result['examples'] = [e for e in result['examples'] if e['message']['uid'] != uid]
    else:
        require(any(row['uid'] == uid for row in result['messages']), '这条原文已变化，请重新打开。')
        result['pins'] = [key for key in result['pins'] if key != uid]
        if action == 'pin':
            require(len(result['pins']) < MAX_PINS, '最多固定 12 条前情，请先取消一条。')
            result['pins'].append(uid)
        elif action == 'forget':
            result['messages'] = [row for row in result['messages'] if row['uid'] != uid]
            result['hidden_uids'] = [*result['hidden_uids'], uid][-512:]
            result['examples'] = [entry for entry in result['examples'] if entry['message']['uid'] != uid]
    result['revision'] += 1
    result['settings_revision'] += 1
    return result


def clear(value, expected_revision, snapshot_at):
    result = validate(value, value['account_fingerprint'], value['source_id'])
    require(type(expected_revision) is int and result['settings_revision'] == expected_revision, '会话记忆已变化，请重新打开。')
    if snapshot_at is not None:
        instant(snapshot_at)
    cutoff = max((t for t in (snapshot_at, result['snapshot_at']) if t is not None), key=instant, default=None)
    result.update(messages=[], pins=[], hidden_uids=[], examples=[], pending=None,
                  clear_before=cutoff, revision=result['revision'] + 1,
                  settings_revision=result['settings_revision'] + 1)
    return result


def _terms(text):
    words = set(re.findall(r'[a-z0-9]{3,}', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words - {'这个', '那个', '一下', '什么', '可以', '我们', '你们', '他们', '就是', '不是', '怎么', '已经', '好的', '收到'}


def for_reply(value, context, goal=''):
    result = validate(value, context['account_fingerprint'], context['source']['id'])
    current = {row['uid'] for row in context['messages']}
    selected, used = [], 0
    if result['enabled']:
        query = _terms(goal + ' '.join(row['text'] for row in context['messages'][-6:]))
        options = [row for row in result['messages'] if row['uid'] not in current]
        options.sort(key=lambda row: (row['uid'] in result['pins'], len(_terms(row['text']) & query), instant(row['time'])), reverse=True)
        for row in options:
            if row['uid'] not in result['pins'] and len(_terms(row['text']) & query) < 2:
                continue
            if len(selected) < MAX_SNIPPETS and used + len(row['text']) <= SNIPPET_CHARS:
                selected.append(row)
                used += len(row['text'])
    selected.sort(key=lambda row: (instant(row['time']), row['uid']))
    sent_edits = sorted((e['message'] for e in result['examples']),
                        key=lambda row: (instant(row['time']), row['uid'])) if result['feedback_enabled'] else []
    return {'account_fingerprint': result['account_fingerprint'], 'source_id': result['source_id'],
            'messages': copy.deepcopy(selected), 'notes': result['notes'], 'snapshot_at': result['snapshot_at'],
            'sent_edits': copy.deepcopy(sent_edits)}


def export_context(export_dir, source_id):
    import dashboard
    from assistant_worker import _fingerprint, _read_summary
    meta, rows, _by_uid, digest = dashboard.load_export(export_dir)
    summary = _read_summary(export_dir, digest)
    source = next((s for s in meta['sources'] if s['id'] == source_id), None)
    require(source is not None, '会话不在当前导出中。')
    rows = [row for row in rows if row['source_id'] == source_id and row['kind'] == 'text' and
            0 < len(row['text']) <= 1200 and not row.get('quoted_content')]
    return rows_context(_bounded(rows, set()), _fingerprint(summary['account_id']), source_id,
                        meta['snapshot_at'], {k: source[k] for k in ('id', 'name', 'kind')})
