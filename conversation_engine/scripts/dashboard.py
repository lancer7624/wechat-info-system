"""Build and optionally serve a local, evidence-bound chat analysis dashboard."""
import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import uuid
import webbrowser

from runtime_paths import DATA_ROOT, PROJECT_ROOT
from message_content import READABLE_KINDS

TEMPLATE = PROJECT_ROOT / 'dashboard' / 'index.html'
MARKER = '__WECHAT_DASHBOARD_DATA__'
FONT_MARKER = '__WECHAT_DASHBOARD_FONTS__'
FONT_PATH = PROJECT_ROOT / 'dashboard' / 'fonts' / 'NotoSerifSC.woff2'
STYLE_MARKER = '__WECHAT_DASHBOARD_STYLES__'
STYLE_PATH = PROJECT_ROOT / 'dashboard' / 'style.css'
ART_ASSETS = {
    '__WECHAT_DASHBOARD_PAPER__': PROJECT_ROOT / 'desktop' / 'assets' / 'ivory-paper.png',
    '__WECHAT_DASHBOARD_JADE__': PROJECT_ROOT / 'desktop' / 'assets' / 'jade-bamboo.png',
    '__WECHAT_DASHBOARD_MAGPIE__': PROJECT_ROOT / 'desktop' / 'assets' / 'courier-magpie.png',
}
HOST = '127.0.0.1'
STATUSES = {'待跟进', '待确认', '进行中', '已完成', '已取消'}
COUNT_FIELDS = (
    'raw_rows', 'all_messages', 'duplicate_messages', *READABLE_KINDS,
    'excluded_nontext', 'excluded_unparsed', 'excluded_empty_or_decode_failed',
    'redacted_messages', 'unresolved_senders',
)
DELTA_FIELDS = ('new', 'updated', 'unchanged', 'context_messages')


class ValidationError(ValueError):
    """An input is incomplete, mismatched, or unsafe to present as verified."""


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def read_bytes(path, label):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ValidationError(label + '无法读取，请检查文件是否存在及读取权限。') from exc


def parse_json(raw, label):
    try:
        def invalid_constant(_value):
            raise ValueError('Non-finite JSON value')
        return json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(label + '不是有效的 JSON。') from exc


def text_field(value, label):
    require(isinstance(value, str), label + '必须是文字。')
    return value


def integer(value, label):
    require(type(value) is int and value >= 0, label + '必须是非负整数。')
    return value


def timestamp(value, label):
    text_field(value, label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(label + '必须是 ISO 日期时间。') from exc
    require(parsed.tzinfo is not None, label + '必须包含时区。')
    return value


def identity_label(value, fallback):
    """Do not publish internal account IDs as display names."""
    value = text_field(value, '显示名称')
    if not value or re.fullmatch(r'(?:wxid_[\w-]+|gh_[\w-]+|Msg_[0-9a-f]{32}|[^\s]+@chatroom)', value):
        return fallback
    return value


def numeric_fields(value, fields, label):
    require(isinstance(value, dict), label + '必须是对象。')
    return {key: integer(value[key], label + '.' + key) for key in fields if key in value}


def load_export(directory):
    """Verify one selected v2 export and return only dashboard-visible fields."""
    directory = Path(directory)
    summary = parse_json(read_bytes(directory / 'summary.json', '导出摘要'), '导出摘要')
    require(isinstance(summary, dict), '导出摘要必须是对象。')
    require(type(summary.get('schema_version')) is int and summary['schema_version'] == 2,
            '只支持 schema_version=2 的导出，请重新生成 v2 导出。')
    require(summary.get('status') == 'complete', '该导出尚未完成，不能展示为有效结果。')
    expected = summary.get('messages_sha256')
    require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected), '导出摘要缺少有效消息校验和。')
    raw = read_bytes(directory / 'messages.jsonl', '消息导出')
    require(hashlib.sha256(raw).hexdigest() == expected, '消息校验和不匹配；文件可能发生变化，请重新导出。')
    try:
        lines = raw.decode('utf-8').split('\n')
        if lines[-1] == '':
            lines.pop()
    except UnicodeError as exc:
        raise ValidationError('消息导出不是有效的 UTF-8 文字。') from exc
    records = [parse_json(line, '消息记录') for line in lines]
    require(len(records) == integer(summary.get('exported_messages'), '导出消息数'), '消息数量与导出摘要不一致。')
    declared_sources = summary.get('sources')
    require(isinstance(declared_sources, list), '导出摘要缺少会话列表。')
    sources, source_ids = [], set()
    scope = summary.get('source_scope')
    require(scope is None or (isinstance(scope, list) and all(isinstance(s, str) and s for s in scope)),
            '导出会话范围格式无效。')
    allowed_ids = None if scope is None else {
        'C_' + (s[4:12] if re.fullmatch(r'Msg_[0-9a-f]{32}', s) else hashlib.md5(s.encode('utf-8')).hexdigest()[:8])
        for s in scope
    }
    for item in declared_sources:
        require(isinstance(item, dict), '会话摘要必须是对象。')
        sid = item.get('id')
        require(isinstance(sid, str) and re.fullmatch(r'C_[0-9a-f]{8}', sid), '会话编号格式无效。')
        require(sid not in source_ids, '会话摘要包含重复编号。')
        require(allowed_ids is None or sid in allowed_ids, '会话摘要超出声明的筛选范围。')
        source_ids.add(sid)
        counts = {key: integer(item.get(key), '会话.' + key)
                  for key in ('total_messages', 'exported_messages', 'self_messages')}
        require(counts['self_messages'] <= counts['exported_messages'] <= counts['total_messages'],
                '会话消息统计不一致。')
        sources.append({'id': sid, 'name': identity_label(item.get('name'), '未命名会话'),
                        'kind': text_field(item.get('kind'), '会话类型'), **counts})
    require(allowed_ids is None or source_ids == allowed_ids, '会话摘要未覆盖声明的筛选范围。')
    messages, by_uid = [], {}
    actual_counts, actual_self = Counter(), Counter()
    for item in records:
        require(isinstance(item, dict), '消息记录必须是对象。')
        uid, sid = item.get('uid'), item.get('source_id')
        require(isinstance(uid, str) and re.fullmatch(r'W_[0-9a-f]{32}', uid), '消息 UID 格式无效。')
        require(uid not in by_uid, '消息导出包含重复 UID。')
        require(isinstance(sid, str) and sid in source_ids, '消息不属于导出声明的会话范围。')
        require(type(item.get('is_self')) is bool, '消息发送者标记无效。')
        require(isinstance(item.get('kind'), str) and item['kind'] in READABLE_KINDS,
                '消息类型不属于支持的可读导出。')
        row = {'uid': uid, 'source_id': sid, 'time': timestamp(item.get('time'), '消息时间'),
               'sender': identity_label(item.get('sender'), '身份未解析'), 'is_self': item['is_self'],
               'kind': item['kind'], 'text': text_field(item.get('text'), '消息正文')}
        if 'quoted_content' in item:
            row['quoted_content'] = text_field(item['quoted_content'], '引用正文')
        messages.append(row)
        by_uid[uid] = row
        actual_counts[sid] += 1
        actual_self[sid] += item['is_self']
    for source in sources:
        require(source['exported_messages'] == actual_counts[source['id']] and
                source['self_messages'] == actual_self[source['id']], '会话统计与实际消息不一致。')
    counts = numeric_fields(summary.get('counts'), COUNT_FIELDS, '消息统计')
    counts.setdefault('share', 0)  # Earlier v2 exports predate readable share cards.
    require(counts.get('all_messages') == sum(s['total_messages'] for s in sources), '原始消息总数与会话统计不一致。')
    for kind in READABLE_KINDS:
        require(counts.get(kind) == sum(m['kind'] == kind for m in messages), '可读消息类型统计不一致。')
    excluded = ('excluded_nontext', 'excluded_unparsed', 'excluded_empty_or_decode_failed')
    require(all(k in counts for k in excluded), '缺少未解析消息统计，无法判断阅读覆盖。')
    require(counts['all_messages'] == len(messages) + sum(counts[k] for k in excluded), '阅读覆盖统计不一致。')
    by_day = summary.get('by_day')
    require(isinstance(by_day, dict), '每日统计必须是对象。')
    for day, count in by_day.items():
        require(isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day), '每日统计日期格式无效。')
        integer(count, '每日消息数')
    require(sum(by_day.values()) == counts['all_messages'], '每日统计与原始消息总数不一致。')
    missing = summary.get('missing_message_shards')
    require(missing is None or (isinstance(missing, list) and all(isinstance(v, str) and
            re.fullmatch(r'message_\d+\.db', v) for v in missing)), '缺失分库清单格式无效。')
    require(type(summary.get('main_database_only')) is bool, '缺少主数据库覆盖说明。')
    export = {key: timestamp(summary.get(key), '导出.' + key) for key in ('snapshot_at', 'start', 'end')}
    start, end = datetime.fromisoformat(export['start']), datetime.fromisoformat(export['end'])
    require(start <= end, '导出日期范围无效。')
    require(all(start <= datetime.fromisoformat(message['time']) <= end for message in messages),
            '消息时间超出导出声明的日期范围。')
    export.update({'exported_messages': len(messages), 'counts': counts, 'by_day': by_day, 'sources': sources,
                   'missing_message_shards': missing, 'main_database_only': summary['main_database_only'],
                   'delta': numeric_fields(summary.get('delta'), DELTA_FIELDS, '增量统计')})
    return export, messages, by_uid, expected


def load_analysis(path, export_sha256, sources, messages):
    if path is None:
        return None
    analysis = parse_json(read_bytes(path, '分析结果'), '分析结果')
    require(isinstance(analysis, dict), '分析结果必须是对象。')
    require(type(analysis.get('schema_version')) is int and analysis['schema_version'] == 1, '分析结果版本无效。')
    require(analysis.get('export_sha256') == export_sha256, '分析结果绑定了不同的消息导出，不能混用。')
    result = {'schema_version': 1, 'export_sha256': export_sha256,
              'analyzed_at': timestamp(analysis.get('analyzed_at'), '分析时间'),
              'overview': text_field(analysis.get('overview'), '分析概述')}
    ids = set()
    for group, fields in (('highlights', ('title', 'text')), ('followups', ('title', 'owner', 'due_text', 'next_action'))):
        items = analysis.get(group)
        require(isinstance(items, list), group + '必须是列表。')
        result[group] = []
        for item in items:
            require(isinstance(item, dict), '分析条目必须是对象。')
            item_id = text_field(item.get('id'), '分析条目编号')
            require(bool(item_id) and item_id not in ids, '分析条目编号为空或重复。')
            ids.add(item_id)
            sid = item.get('source_id')
            require(isinstance(sid, str) and sid in sources, '分析条目引用了范围外会话。')
            evidence = item.get('evidence_uids')
            require(isinstance(evidence, list) and len(evidence) > 0 and
                    all(isinstance(uid, str) for uid in evidence), '分析条目必须提供证据 UID。')
            require(len(evidence) == len(set(evidence)), '同一分析条目包含重复证据。')
            require(all(uid in messages for uid in evidence), '分析条目引用了不存在的证据。')
            require(all(messages[uid]['source_id'] == sid for uid in evidence), '分析证据来自不同会话，归因校验失败。')
            row = {'id': item_id, 'source_id': sid, 'evidence_uids': evidence,
                   **{key: text_field(item.get(key), '分析.' + key) for key in fields}}
            key, allowed = ('kind', {'fact', 'inference', 'uncertain'}) if group == 'highlights' else ('status', STATUSES)
            require(isinstance(item.get(key), str) and item[key] in allowed, '分析条目状态或类别无效。')
            row[key] = item[key]
            if group == 'highlights' and 'category' in item:
                require(isinstance(item['category'], str) and item['category'] in ('notice', 'reference'),
                        '信息分类无效。')
                row['category'] = item['category']
            result[group].append(row)
    return result


def build_payload(export_dir, analysis_path=None):
    export, messages, by_uid, digest = load_export(export_dir)
    analysis = load_analysis(analysis_path, digest, {s['id'] for s in export['sources']}, by_uid)
    return {'schema_version': 1, 'export': export, 'analysis': analysis, 'messages': messages}


def embedded_json(value):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return text.translate(str.maketrans({'<': '\\u003c', '>': '\\u003e', '&': '\\u0026',
                                        '\u2028': '\\u2028', '\u2029': '\\u2029'}))


def render_dashboard(export_dir, analysis_path=None, *, template=TEMPLATE, font_path=FONT_PATH,
                     style_path=STYLE_PATH, information=None, image_api=None):
    bound_summary = read_bytes(Path(export_dir) / 'summary.json', '导出摘要')
    payload = build_payload(export_dir, analysis_path)
    from image_media import public_index, ImageError
    try:
        payload['images'] = public_index(export_dir)
    except ImageError as exc:
        raise ValidationError(str(exc)) from None
    require(read_bytes(Path(export_dir) / 'summary.json', '导出摘要') == bound_summary,
            '导出读取期间发生变化，请重新加载。')
    if image_api is not None:
        payload['image_api'] = image_api
    if information is not None:
        payload['information'] = information
    try:
        page = read_bytes(template, '看板模板').decode('utf-8')
    except UnicodeError as exc:
        raise ValidationError('看板模板编码无效。') from exc
    require(page.count(MARKER) == 1, '看板模板必须包含唯一的数据标记。')
    style_markers = page.count(STYLE_MARKER)
    require(style_markers <= 1, '看板模板只能包含一个样式标记。')
    if style_markers:
        try:
            style = read_bytes(style_path, '看板样式').decode('utf-8')
        except UnicodeError as exc:
            raise ValidationError('看板样式编码无效。') from exc
        require(not re.search(r'</style\b', style, re.IGNORECASE), '看板样式不能包含 HTML 标签。')
        require(not any(marker in style for marker in (MARKER, FONT_MARKER, STYLE_MARKER)),
                '看板样式包含无效模板标记。')
        page = page.replace(STYLE_MARKER, style)
    for marker, asset_path in ART_ASSETS.items():
        if marker not in page:
            continue
        asset = read_bytes(asset_path, '看板插画')
        require(asset.startswith(b'\x89PNG\r\n\x1a\n'), '看板插画不是 PNG 文件，请恢复本地图片资源。')
        page = page.replace(marker, 'data:image/png;base64,' + base64.b64encode(asset).decode('ascii'))
    font_markers = page.count(FONT_MARKER)
    require(font_markers <= 1, '看板模板只能包含一个字体标记。')
    if font_markers:
        font = read_bytes(font_path, '看板字体')
        require(font.startswith(b'wOF2'), '看板字体不是 WOFF2 文件，请恢复本地字体资源。')
        encoded_font = base64.b64encode(font).decode('ascii')
        font_css = ("@font-face{font-family:'Wechat Song';src:url(data:font/woff2;base64," + encoded_font +
                    ") format('woff2');font-weight:200 900;font-style:normal;font-display:swap;}")
        # Substitute trusted template markers before data, so chat text containing a marker stays untouched.
        page = page.replace(FONT_MARKER, font_css)
    return page.replace(MARKER, embedded_json(payload))


def save_html(output, page):
    output = Path(output)
    require(output.suffix.lower() == '.html', '看板输出必须使用 .html 扩展名。')
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(output.name + '.pending_' + uuid.uuid4().hex)
    try:
        with pending.open('x', encoding='utf-8', newline='') as stream:
            stream.write(page)
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)


def create_server(export_dir, analysis_path=None, *, port=8767, output=None, template=TEMPLATE, font_path=FONT_PATH,
                  information_service=None, image_service=None):
    """No static file handler: only the explicitly bound document is accessible."""
    token = secrets.token_urlsafe(32)
    from image_media import ImageService, ImageError
    image_service = image_service or ImageService(export_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass  # Request paths and personal data do not belong in terminal logs.

        def reply(self, code, body=b'', content_type='text/plain; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            connect = "'self'"
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src " + connect + "; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def valid_host(self):
            hosts = self.headers.get_all('Host', [])
            port = self.server.server_address[1]
            allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if port == 80:
                allowed_hosts.update({'127.0.0.1', 'localhost'})
            if len(hosts) != 1 or hosts[0].strip().lower() not in allowed_hosts:
                self.reply(403, '不允许的本地服务地址。'.encode('utf-8'))
                return False
            return True

        def information_json(self, value, code=200):
            self.reply(code, embedded_json(value).encode('utf-8'), 'application/json; charset=utf-8')

        def do_GET(self):
            if not self.valid_host():
                return
            if information_service and self.path == '/api/information':
                self.information_json(information_service.get_state(only_if_ready=True))
                return
            if self.path == '/favicon.ico':
                self.reply(204)
                return
            if self.path not in ('/', '/index.html'):
                self.reply(404, '此地址不可访问。'.encode('utf-8'))
                return
            try:
                info = {**information_service.get_state(), 'token': token} if information_service else None
                page = render_dashboard(export_dir, analysis_path, template=template, font_path=font_path,
                                        information=info, image_api={'token': token})
                if output is not None:
                    # Saved HTML has no ephemeral server token or dead API dependency.
                    save_html(output, render_dashboard(export_dir, analysis_path, template=template, font_path=font_path))
            except (ValidationError, OSError) as exc:
                message = str(exc) if isinstance(exc, ValidationError) else '无法写入看板，请检查输出目录权限。'
                self.reply(422, ('看板更新失败：' + message + '\n未展示旧结果，请修复后刷新。').encode('utf-8'))
                return
            self.reply(200, page.encode('utf-8'), 'text/html; charset=utf-8')

        def reject_method(self):
            self.reply(405, '只允许读取看板。'.encode('utf-8'))

        def do_POST(self):
            if self.path == '/api/image':
                if not self.valid_host():
                    return
                expected_origin = 'http://' + self.headers['Host'].strip().lower()
                tokens = self.headers.get_all('X-Image-Token', [])
                if (self.headers.get_all('Origin', []) != [expected_origin] or len(tokens) != 1 or
                        not secrets.compare_digest(tokens[0].encode('utf-8'), token.encode('utf-8'))):
                    self.reply(403, '请从当前本地看板打开图片。'.encode('utf-8'))
                    return
                try:
                    lengths = self.headers.get_all('Content-Length', [])
                    require(len(lengths) == 1 and lengths[0].isdigit() and 0 < int(lengths[0]) <= 512 and
                            self.headers.get_content_type() == 'application/json' and
                            not self.headers.get_all('Transfer-Encoding'), '图片请求格式无效。')
                    self.connection.settimeout(5)
                    value = parse_json(self.rfile.read(int(lengths[0])), '图片请求')
                    require(isinstance(value, dict) and set(value) == {'uid', 'version'}, '图片请求字段无效。')
                    self.information_json(image_service.preview(value['uid'], value['version']))
                except (ValidationError, ImageError) as exc:
                    self.information_json({'error': str(exc)}, 422)
                except Exception:
                    self.information_json({'error': '本地图片暂时无法打开，请重试。'}, 422)
                return
            if information_service is None:
                return self.reject_method()
            if not self.valid_host():
                return
            origins = self.headers.get_all('Origin', [])
            tokens = self.headers.get_all('X-Information-Token', [])
            expected_origin = 'http://' + self.headers['Host'].strip().lower()
            if ((origins and origins != [expected_origin]) or len(tokens) != 1 or
                    not secrets.compare_digest(tokens[0].encode('utf-8'), token.encode('utf-8'))):
                self.reply(403, '请从当前本地看板操作。'.encode('utf-8'))
                return
            if self.path not in ('/api/information/run', '/api/information/correct'):
                self.reply(404)
                return
            lengths = self.headers.get_all('Content-Length', [])
            try:
                require(len(lengths) == 1 and lengths[0].isdigit() and 0 < int(lengths[0]) <= 4096 and
                        self.headers.get_content_type() == 'application/json' and
                        not self.headers.get_all('Transfer-Encoding'), '操作请求格式无效。')
                self.connection.settimeout(5)
                raw = self.rfile.read(int(lengths[0]))
                require(len(raw) == int(lengths[0]), '操作请求不完整。')
                value = parse_json(raw, '操作请求')
                require(isinstance(value, dict), '操作请求必须是对象。')
                if self.path.endswith('/run'):
                    require(not value, '整理请求不接受路径或模型参数。')
                    state = information_service.start()
                else:
                    require(set(value) == {'item_id', 'category', 'version'} and
                            all(isinstance(v, str) and len(v) <= 100 for v in value.values()), '分类请求格式无效。')
                    state = information_service.correct(value['item_id'], value['category'], value['version'])
                self.information_json(state)
            except Exception as exc:
                from information_assistant import InformationError
                from ai_client import AIError
                message = str(exc) if isinstance(exc, (ValidationError, InformationError, AIError)) else '操作未完成，请重试。'
                self.information_json({'error': message}, 422)

        do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = reject_method

    return ThreadingHTTPServer((HOST, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, dest='export_dir', help='已完成的 v2 导出目录')
    parser.add_argument('--analysis', type=Path, help='绑定该导出的结构化 AI 分析 JSON；省略则展示待分析')
    parser.add_argument('--output', type=Path, default=DATA_ROOT / 'dashboard' / '微信聊天看板.html')
    parser.add_argument('--serve', action='store_true', help='仅在 127.0.0.1 提供看板，刷新时重新校验数据')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--information', action='store_true', help='开启群聊与公众号的按需整理入口')
    parser.add_argument('--existing-only', action='store_true', help='整理已绑定的记录，不提取新快照')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port 必须在 1 至 65535 之间')
    if args.export_dir is None:
        if not args.information:
            parser.error('请指定 --export')
        try:
            pointer = parse_json(read_bytes(DATA_ROOT / 'assistant' / 'latest.json', '助手索引'), '助手索引')
            args.export_dir = Path(pointer['export_dir'])
        except (ValidationError, KeyError, TypeError):
            parser.error('尚无本机聊天导出，请先在会话笺中读取记录，或指定 --export')
    args.export_dir, args.output = args.export_dir.resolve(), args.output.resolve()
    if args.analysis:
        args.analysis = args.analysis.resolve()
    try:
        require(args.output != TEMPLATE.resolve(), '输出路径不能覆盖看板模板。')
        if args.information:
            from information_assistant import InformationService
            kwargs = {'refresher': None} if args.existing_only else {}
            service = InformationService(args.export_dir, **kwargs)
            with create_server(args.export_dir, args.analysis, port=args.port, information_service=service) as server:
                url = f'http://{HOST}:{server.server_address[1]}/#information'
                print('信息整理看板：' + url + '（点击整理才调用现有模型；按 Ctrl+C 结束）')
                if not args.no_open:
                    webbrowser.open(url)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    service.close()
            return
        page = render_dashboard(args.export_dir, args.analysis)
        save_html(args.output, page)
        print('看板已生成：' + str(args.output))
        if not args.serve:
            if not args.no_open:
                webbrowser.open(args.output.as_uri())
            return
        with create_server(args.export_dir, args.analysis, port=args.port, output=args.output) as server:
            url = f'http://{HOST}:{server.server_address[1]}/'
            print('本地看板：' + url + '（刷新会重新校验；按 Ctrl+C 结束）')
            if not args.no_open:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print('\n看板服务已结束。')
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValidationError) else '无法保存看板或启动本地服务，请检查目录权限和端口。'
        parser.exit(1, '看板生成失败：' + message + '\n')


if __name__ == '__main__':
    main()
