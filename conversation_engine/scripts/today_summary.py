"""Fresh, complete, single-conversation summaries for one Beijing calendar day."""
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path
import re

import assistant_worker as data
import dashboard
from analyze_chats import analyze_export
from runtime_paths import DATA_ROOT

CHINA = timezone(timedelta(hours=8))
MAX_REQUESTS = 100
CHAT_KINDS = ('私聊', '群聊', 'private', 'contact', 'friend', 'group')


def today():
    return datetime.now(CHINA).date().isoformat()


def _page(export, analysis, day, source_name):
    page = dashboard.render_dashboard(export, analysis)
    title = html.escape(f'{day} · {source_name} · 当天聊天总结')
    return page.replace('<title>会话笺 · 工作台</title>', '<title>' + title + '</title>', 1).replace(
        '<span class="local">工作台</span>', '<span class="local">当天总结 · ' + day + '</span>', 1).replace(
        "overview:'聊天总览'", "overview:'当天聊天总结'", 1).replace(
        "overview:'把聊天里的重点，安放在一张笺上。'", "overview:'只看今天，整理主要话题、重要信息与跟进事项。'", 1)


def _export(export_dir, source_id, account, day):
    data._source_id(source_id)
    data._require(isinstance(account, str) and re.fullmatch(r'[a-f0-9]{64}', account), '账号无效。')
    midnight = datetime.fromisoformat(day + 'T00:00:00+08:00')
    bound = (Path(export_dir) / 'summary.json').read_bytes()
    meta, rows, by_uid, digest = dashboard.load_export(export_dir)
    original = data._read_summary(export_dir, digest)
    data._require(original.get('analysis_only') is not True, '当天总结需要完整的当天导出。')
    data._require(data._fingerprint(original['account_id']) == account, '微信账号已变化，请重新选择会话。')
    data._require(len(meta['sources']) == 1 and meta['sources'][0]['id'] == source_id and
                  meta['sources'][0]['kind'] in CHAT_KINDS, '当天总结超出所选群聊或私聊范围。')
    start, end, snapshot = (datetime.fromisoformat(meta[k]).astimezone(CHINA)
                            for k in ('start', 'end', 'snapshot_at'))
    data._require(start == midnight and end == snapshot and midnight <= end < midnight + timedelta(days=1),
                  '读取日期与今天不一致，请重新总结今天。')
    data._require(all(midnight <= datetime.fromisoformat(row['time']).astimezone(CHINA) <= end for row in rows),
                  '当天总结中包含范围外消息。')
    data._require((Path(export_dir) / 'summary.json').read_bytes() == bound, '读取期间导出发生变化，请重试。')
    return meta, rows, by_uid, digest


def load_result(value, source_id, account, day, *, data_root=None):
    """Revalidate stored evidence before showing or reopening a daily result."""
    root = Path(data_root or DATA_ROOT).resolve() / 'assistant'
    export = Path(value['export_dir']).resolve()
    analysis = Path(value['analysis_path']).resolve()
    output = Path(value['html_path']).resolve()
    run = output.parent
    data._require(run.parent == root / 'today_summaries' and re.fullmatch(r'[a-f0-9]{32}', run.name) and
                  output.name == '当天聊天总结.html' and output.is_file() and
                  analysis == run / 'analysis' / 'analysis.json' and
                  export.name == 'export' and export.parent.parent == root / 'captures' and
                  re.fullmatch(r'[a-f0-9]{32}', export.parent.name), '当天总结文件范围无效。')
    meta, rows, by_uid, digest = _export(export, source_id, account, day)
    verified = dashboard.load_analysis(analysis, digest, {source_id}, by_uid)
    data._require(verified is not None, '当天总结缺少已验证的分析结果。')
    data._require(output.read_bytes() == _page(export, analysis, day, meta['sources'][0]['name']).encode('utf-8'),
                  '当天总结页面与已验证的分析结果不一致。')
    return {'date': day, 'source_id': source_id, 'account_fingerprint': account,
            'snapshot_at': meta['snapshot_at'], 'start': meta['start'], 'end': meta['end'],
            'messages': len(rows), 'excluded_messages': meta['sources'][0]['total_messages'] - len(rows),
            'overview': verified['overview'], 'export_dir': str(export),
            'analysis_path': str(analysis), 'html_path': str(output)}


def summarize(source_id, account, day, config):
    data._require(isinstance(config, dict) and config.get('allow_chat_upload') is True,
                  '尚未授权当前会话使用此模型，未总结今天。')
    data._require(day == today(), '日期已变化，请重新总结今天。')
    capture = data.refresh_export(source_id, days=1)
    export = capture['export_dir']
    meta, rows, by_uid, digest = _export(export, source_id, account, day)
    data._require(day == today(), '采集期间日期已变化，请重新总结今天。')
    run = data._new_run('today_summaries')
    result = analyze_export(export, config, run / 'analysis',
                            Path(DATA_ROOT) / 'assistant' / 'today_summary_cache' / account,
                            max_requests=MAX_REQUESTS, today=True)
    data._require(isinstance(result, dict) and all(type(result.get(k)) is int and result[k] >= 0 for k in
                  ('messages', 'sources', 'analyzed_sources', 'cached_sources', 'requests')) and
                  Path(result['analysis_path']).resolve() == run.resolve() / 'analysis' / 'analysis.json' and
                  result['messages'] == len(rows) and result['sources'] == 1 and
                  result['analyzed_sources'] + result['cached_sources'] == int(bool(rows)) and
                  type(result['requests']) is int and 0 <= result['requests'] <= MAX_REQUESTS,
                  '当天总结未覆盖本次全部可读消息，未发布结果。')
    dashboard.load_analysis(result['analysis_path'], digest, {source_id}, by_uid)
    output = run / '当天聊天总结.html'
    dashboard.save_html(output, _page(export, result['analysis_path'], day, meta['sources'][0]['name']))
    return load_result({'export_dir': export, 'analysis_path': result['analysis_path'], 'html_path': output},
                       source_id, account, day)
