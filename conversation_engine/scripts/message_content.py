"""Read local share-card metadata without fetching links or media bodies."""
from html.parser import HTMLParser
import xml.etree.ElementTree as ET


READABLE_KINDS = ('text', 'reply', 'file', 'link', 'share')


class _CardText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif not self.hidden and tag in ('br', 'p', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _description(value):
    parser = _CardText()
    parser.feed(value)
    parser.close()
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def _bounded(value, limit):
    return value.strip() if len(value.strip()) <= limit else value.strip()[:limit] + '…[卡片文字已截取]'


def parse_app_message(xml, redact):
    """Return (kind, readable text, extras), or None for unsupported cards.

    Redact XML field values before stripping description markup or truncating.
    Forwarded summaries stay attached to the outer message's sender and UID.
    """
    if len(xml) > 2 * 1024 * 1024:
        raise ET.ParseError('Share card exceeds the local parsing limit.')
    begin = xml.find('<msg')
    root = ET.fromstring(xml[begin:] if begin >= 0 else xml)
    app = root if root.tag == 'appmsg' else root.find('appmsg') if root.tag == 'msg' else None
    if app is None:
        return None
    subtype = app.findtext('type', '').strip()
    title = redact(app.findtext('title', ''))
    if subtype == '57':
        quoted = app.findtext('refermsg/content', '')
        if app.findtext('refermsg/type', '1') != '1' or quoted.lstrip().startswith(('<?xml', '<msg', '<img')):
            quoted = '[引用的非文本消息，未解析]'
        quoted_id = app.findtext('refermsg/svrid', '')
        return 'reply', title, {
            'quoted_content': redact(quoted),
            'quoted_speaker': redact(app.findtext('refermsg/displayname', '')),
            'quoted_server_id': quoted_id if quoted_id.isdecimal() else '',
        }
    if subtype == '6':
        return 'file', title, {'url': ''}
    if subtype not in ('5', '19', '33', '51'):
        return None

    def field(path, limit=3000):
        return _bounded(_description(redact(app.findtext(path, ''))), limit)

    description = field('des')
    source = field('sourcedisplayname', 200) or field('appname', 200)
    title = _bounded(title, 512)
    if subtype == '5':
        # The URL is evidence metadata, never a fetched article or a model tool.
        url = redact(app.findtext('url', ''))
        if not title and not description and not url:
            return 'link', '', {'url': ''}
        lines = [title or '链接分享']
        if source:
            lines.append('卡片来源：' + source)
        if description:
            lines.append('卡片简介：' + description)
        lines.append('[网页正文未读取]')
        return 'link', '\n'.join(lines), {'url': url}
    if subtype == '51':
        # WeChat often puts an upgrade notice in the top-level title/URL.
        # Only finderFeed captions and names describe the actual share.
        description = field('finderFeed/desc') or field('finderFeed/description') or field('desc') or field('description')
        author = field('finderFeed/nickname', 200)
        lines = ['[视频号分享]']
        if author:
            lines.append('卡片作者：' + author)
        if description:
            lines.append('卡片文案：' + description)
        lines.append('[视频号正文与媒体未解析]')
        return 'share', '\n'.join(lines), {}
    lines = ['[合并转发]' if subtype == '19' else '[小程序分享]']
    if title:
        lines.append(title)
    if source:
        lines.append('卡片来源：' + source)
    if description:
        lines.append(('转发摘要：' if subtype == '19' else '卡片简介：') + description)
    lines.append('[摘要中的人物与发言不作为当前会话原话；完整转发未解析]' if subtype == '19' else
                 '[小程序内容未读取]')
    return 'share', '\n'.join(lines), {}
