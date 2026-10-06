"""Account-bound local image evidence and key import. Images never become model text."""
import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET

from image_codec import ImageError, MAX_BYTES, V2, decode_preview, decrypt_dat, make_preview, require
from runtime_paths import DATA_ROOT, PROJECT_ROOT

HEX32 = re.compile(r'[0-9a-f]{32}')
UID = re.compile(r'W_[0-9a-f]{32}')
FIELDS = {'uid', 'source_id', 'conversation_hash', 'image_md5', 'time', 'sender', 'is_self', 'kind'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def image_reference(xml):
    """Project only an exact image digest; never persist XML, URLs, AES keys or paths."""
    if not isinstance(xml, str) or len(xml) > 131072 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)', xml, re.I):
        return None
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError, RecursionError):
        return None
    matches = [node for node in root.iter() if node.tag == 'img']
    if len(matches) != 1:
        return None
    value = matches[0].get('md5', '').lower()
    return value if HEX32.fullmatch(value) else None


def binding_digest(summary):
    value = {key: summary.get(key) for key in ('account_id', 'snapshot_at', 'start', 'end', 'messages_sha256')}
    value['sources'] = sorted(source['id'] for source in summary['sources'])
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _protobuf_fields(data):
    """Read a bounded message, without scanning arbitrary byte strings for hashes."""
    if not isinstance(data, bytes) or len(data) > 131072:
        raise ValueError('Invalid protobuf length')
    position, fields, count = 0, {}, 0
    def varint():
        nonlocal position
        result = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError('Truncated varint')
            value = data[position]
            position += 1
            if shift == 63 and value > 1:
                raise ValueError('Overflow varint')
            result |= (value & 127) << shift
            if value < 128:
                return result
        raise ValueError('Invalid varint')
    while position < len(data):
        count += 1
        if count > 512:
            raise ValueError('Too many fields')
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2**29:
            raise ValueError('Invalid field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else 8 if wire == 1 else 4
            end = position + size
            if end > len(data):
                raise ValueError('Truncated field')
            value = data[position:end]
            position = end
        else:
            raise ValueError('Unsupported wire type')
        fields.setdefault(field, []).append((wire, value))
    return fields


def packed_image_reference(data):
    """Windows 4 image row's packed_info_data: one resource digest at field 3.4."""
    try:
        fields = _protobuf_fields(data)
        resource = fields.get(3, [])
        if len(resource) != 1 or resource[0][0] != 2:
            return None
        digest = _protobuf_fields(resource[0][1]).get(4, [])
        if len(digest) != 1 or digest[0][0] != 2 or not re.fullmatch(rb'[a-fA-F0-9]{32}', digest[0][1]):
            return None
        return digest[0][1].decode('ascii').lower()
    except (ValueError, TypeError):
        return None


def load_images(export_dir):
    """Validate both the base export and its optional image sidecar, including coverage."""
    import dashboard
    root = Path(export_dir).resolve()
    bound_summary = dashboard.read_bytes(root / 'summary.json', '图片导出摘要')
    meta, _text, by_uid, text_digest = dashboard.load_export(root)
    require(dashboard.read_bytes(root / 'summary.json', '图片导出摘要') == bound_summary,
            '图片导出读取期间发生变化，请重新加载。')
    summary = dashboard.parse_json(bound_summary, '图片导出摘要')
    require(summary.get('messages_sha256') == text_digest, '导出读取期间发生变化，请重新加载。')
    index = summary.get('images')
    if index is None:
        return summary, [], None
    require(isinstance(index, dict) and set(index) == {'schema_version', 'count', 'sha256', 'binding_sha256'} and
            index['schema_version'] == 1 and type(index['count']) is int and index['count'] >= 0 and
            index['binding_sha256'] == binding_digest(summary), '图片索引与当前导出不一致。')
    path = root / 'images.jsonl'
    require(path.resolve().is_relative_to(root) and path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
            '图片索引范围或大小无效。')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == index['sha256'], '图片索引校验失败。')
    try:
        rows = [dashboard.parse_json(line, '图片记录') for line in raw.splitlines()]
    except (ValueError, RecursionError):
        raise ImageError('图片记录格式无效。') from None
    require(len(rows) == index['count'] and len(rows) <= summary['counts']['excluded_nontext'],
            '图片索引数量与媒体统计不一致。')
    source_map = {source['id']: source for source in meta['sources']}
    seen, counts = set(by_uid), Counter()
    start, end = datetime.fromisoformat(meta['start']), datetime.fromisoformat(meta['end'])
    for row in rows:
        require(isinstance(row, dict) and set(row) == FIELDS, '图片记录字段无效。')
        sid, uid = row['source_id'], row['uid']
        require(isinstance(uid, str) and UID.fullmatch(uid) and uid not in seen and sid in source_map,
                '图片消息编号或会话无效。')
        require(isinstance(row['conversation_hash'], str) and HEX32.fullmatch(row['conversation_hash']) and
                sid == 'C_' + row['conversation_hash'][:8] and
                (row['image_md5'] is None or isinstance(row['image_md5'], str) and HEX32.fullmatch(row['image_md5'])),
                '图片文件关联无效。')
        require(row['kind'] == 'image' and type(row['is_self']) is bool and
                isinstance(row['sender'], str) and 0 < len(row['sender']) <= 4096, '图片发送者或类型无效。')
        try:
            stamp = datetime.fromisoformat(row['time'])
            require(stamp.tzinfo is not None and start <= stamp <= end, '图片消息不在本次快照范围。')
        except (TypeError, ValueError):
            raise ImageError('图片消息时间无效。') from None
        seen.add(uid)
        counts[sid] += 1
    require(all(count <= source_map[sid]['total_messages'] - source_map[sid]['exported_messages']
                for sid, count in counts.items()), '图片消息超出会话媒体统计。')
    rows.sort(key=lambda row: (datetime.fromisoformat(row['time']), row['uid']))
    return summary, rows, index['sha256']


def public_index(export_dir, source_id=None, limit=None):
    _summary, rows, version = load_images(export_dir)
    if source_id is not None:
        rows = [row for row in rows if row['source_id'] == source_id]
    total = len(rows)
    if limit is not None:
        rows = rows[-limit:]
    return {'version': version, 'total': total, 'messages': [
        {**{key: row[key] for key in ('uid', 'source_id', 'time', 'sender', 'is_self', 'kind')},
         'text': '[图片]', 'has_reference': row['image_md5'] is not None} for row in rows]}


def read_limited(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(0 < len(raw) <= MAX_BYTES, '图片为空或超过 40 MB 限制。')
        return raw
    except OSError:
        raise ImageError('本地图片暂时不可读取，请稍后重试。') from None


def configured_keys():
    from assistant_worker import _load_existing_keys
    return _load_existing_keys()


def account_media_root(keys, expected_account=None):
    from export_recent import account_identity
    root = Path(keys['db_root']).resolve()
    _self, account = account_identity(root)
    require(expected_account is None or expected_account == account, '图片与当前接入账号不一致，未读取文件。')
    media = root.parent / 'msg' / 'attach'
    require(media.resolve().is_relative_to(root.parent) and media.resolve() == media,
            '图片目录指向账号范围之外。')
    return media, account


def image_candidates(media, row):
    require(row['image_md5'] is not None, '这条图片缺少可核对的本地关联，未猜测文件。')
    conversation = media / row['conversation_hash']
    require(conversation.resolve() == conversation and conversation.is_dir(), '微信本地尚无这段会话的图片。')
    month = datetime.fromisoformat(row['time']).strftime('%Y-%m')
    months = [month]
    # Exact content hash and exact conversation only; no timestamp/fuzzy matching.
    for entry in conversation.iterdir():
        if re.fullmatch(r'\d{4}-\d{2}', entry.name) and entry.name != month and entry.is_dir():
            months.append(entry.name)
        require(len(months) <= 241, '会话图片目录过多，请缩小整理范围。')
    found = []
    for suffix in ('', '_h', '_t'):
        for item in months:
            path = conversation / item / 'Img' / (row['image_md5'] + suffix + '.dat')
            if path.is_file():
                require(path.resolve() == path and path.resolve().is_relative_to(media), '图片文件超出当前账号范围。')
                found.append((path, suffix == '_t'))
    require(bool(found), '微信尚未保存这张图片；可先在微信中打开原图，再重试。')
    return found[:6]


def load_image_key(root, account):
    path = Path(root) / 'private' / 'image_keys' / (account + '.json')
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        require(value['schema_version'] == 1 and value['account_id'] == account and
                isinstance(value['key_hex'], str) and HEX32.fullmatch(value['key_hex']) and
                (value['xor_key'] is None or type(value['xor_key']) is int and 0 <= value['xor_key'] <= 255),
                '图片密钥配置校验失败。')
        return {'aes_key': bytes.fromhex(value['key_hex']), 'xor_key': value['xor_key']}
    except (OSError, ValueError, KeyError, TypeError):
        raise ImageError('本人图片密钥配置无法读取，未使用其他密钥。') from None


class ImageService:
    def __init__(self, export_dir, *, keys_loader=configured_keys, data_root=DATA_ROOT):
        self.export_dir = Path(export_dir).resolve()
        self.keys_loader, self.root = keys_loader, Path(data_root).resolve()

    def preview(self, uid, version):
        require(isinstance(uid, str) and UID.fullmatch(uid) and isinstance(version, str) and
                re.fullmatch(r'[0-9a-f]{64}', version), '图片预览请求无效。')
        summary, rows, current = load_images(self.export_dir)
        require(version == current, '图片记录已变化，请刷新后重新打开。')
        row = next((item for item in rows if item['uid'] == uid), None)
        require(row is not None, '当前导出没有这条图片消息。')
        media, account = account_media_root(self.keys_loader(), summary['account_id'])
        credentials = load_image_key(self.root, account)
        error = None
        for path, thumbnail in image_candidates(media, row):
            try:
                cipher = read_limited(path)
                image = decode_preview(cipher, **credentials)
                cache_key = hashlib.sha256((uid + version).encode() + cipher + b'preview-v1').hexdigest()
                cache = self.root / 'assistant' / 'image_cache' / account
                require(not cache.resolve().is_relative_to(PROJECT_ROOT) and
                        cache.resolve().is_relative_to(self.root) and cache.resolve() == cache,
                        '图片缓存必须在本人数据目录内、源码之外。')
                cache.mkdir(parents=True, exist_ok=True)
                destination = cache / (cache_key + ('.png' if image.mime == 'image/png' else '.jpg'))
                if not destination.exists() or destination.read_bytes() != image.data:
                    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.pending')
                    try:
                        temporary.write_bytes(image.data)
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                return {'uid': uid, 'source_id': row['source_id'], 'version': version,
                        'data_url': 'data:' + image.mime + ';base64,' + base64.b64encode(image.data).decode('ascii'),
                        'width': image.width, 'height': image.height, 'thumbnail': thumbnail,
                        'first_frame': image.first_frame,
                        'note': '本地查看 · 图片未交给模型' + (' · 缩略图' if thumbnail else '') +
                                (' · 动画仅显示首帧' if image.first_frame else '')}
            except ImageError as exc:
                error = error or exc
        raise error or ImageError('这张图片暂时无法解码。')


def infer_tail_key(data, key):
    """Infer from a known complete JPEG/PNG trailer, then verify the decoded pixels."""
    plain = decrypt_dat(data, aes_key=key, xor_key=0)
    tail_size = int.from_bytes(data[10:14], 'little')
    if tail_size == 0:
        make_preview(plain)
        return None
    trailer = (b'\xff\xd9' if plain.startswith(b'\xff\xd8\xff') else
               b'\x00\x00\x00\x00IEND\xaeB`\x82' if plain.startswith(b'\x89PNG\r\n\x1a\n') else None)
    require(trailer is not None and tail_size >= len(trailer), '样本无法核对尾部参数，请提供 --xor-key。')
    candidates = {left ^ right for left, right in zip(data[-len(trailer):], trailer)}
    require(len(candidates) == 1, '样本尾部无法验证，未保存密钥。')
    value = candidates.pop()
    decode_preview(data, aes_key=key, xor_key=value)
    return value


def import_key(path, sample, *, xor_key=None, keys_loader=configured_keys, data_root=DATA_ROOT):
    keys = keys_loader()
    media, account = account_media_root(keys)
    sample = Path(sample).resolve()
    require(sample.is_relative_to(media) and sample.suffix.lower() == '.dat' and sample.is_file(),
            '样本必须是当前本人账号 msg/attach 内的 .dat 图片。')
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        key_hex = raw.get('key_hex')
        require(isinstance(key_hex, str) and HEX32.fullmatch(key_hex.lower()), '密钥文件需包含 16 字节 key_hex。')
        key = bytes.fromhex(key_hex)
    except (OSError, ValueError, TypeError, AttributeError):
        raise ImageError('密钥文件格式无效，未导入。') from None
    cipher = read_limited(sample)
    require(cipher.startswith(V2), '请选择本人账号的 V2 图片样本校验密钥。')
    if xor_key is None:
        xor_key = infer_tail_key(cipher, key)
    preview = decode_preview(cipher, aes_key=key, xor_key=xor_key)
    destination = Path(data_root).resolve() / 'private' / 'image_keys' / (account + '.json')
    require(not destination.resolve().is_relative_to(PROJECT_ROOT), '图片密钥必须在源码之外。')
    value = {'schema_version': 1, 'account_id': account, 'key_hex': key.hex(), 'xor_key': xor_key,
             'sample_sha256': hashlib.sha256(cipher).hexdigest()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = load_image_key(data_root, account)
        require(existing == {'aes_key': key, 'xor_key': xor_key}, '已存在另一份本人图片密钥，未覆盖配置。')
        return destination, preview
    pending = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        pending.write_text(canonical(value) + '\n', encoding='utf-8')
        # Hard-link publishing is atomic and refuses to replace a concurrent import.
        import os
        os.link(pending, destination)
    except FileExistsError:
        raise ImageError('密钥配置已变化，未覆盖，请重新检查。') from None
    finally:
        pending.unlink(missing_ok=True)
    return destination, preview


def main():
    parser = argparse.ArgumentParser(description='用本人当前账号的图片验证并导入密钥；不扫描进程，不覆盖原配置。')
    parser.add_argument('--from', dest='key_file', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--xor-key', type=lambda value: int(value, 0))
    args = parser.parse_args()
    try:
        _destination, preview = import_key(args.key_file, args.sample, xor_key=args.xor_key)
        print('本人图片密钥已验证并保存；样本尺寸：' + str(preview.width) + ' × ' + str(preview.height))
    except (ImageError, OSError):
        parser.exit(1, '图片密钥未导入：样本校验、账号范围或现有配置不一致。\n')


if __name__ == '__main__':
    main()
