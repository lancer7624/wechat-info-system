"""Independent, bounded DAT decoding. No process access, file writes or key discovery."""
from dataclasses import dataclass
import io
import struct
import warnings

from runtime_paths import enable_dependencies
enable_dependencies()
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from PIL import Image, ImageOps

MAX_BYTES = 40 * 1024 * 1024
MAX_PIXELS = 16_000_000
V1 = b'\x07\x08V1\x08\x07'
V2 = b'\x07\x08V2\x08\x07'
V1_KEY = b'cfcd208495d565ef'


class ImageError(ValueError):
    """Only static, safe error text may leave the decoder."""


@dataclass(frozen=True)
class Preview:
    data: bytes
    mime: str
    width: int
    height: int
    first_frame: bool


def require(condition, message):
    if not condition:
        raise ImageError(message)


def signature(data):
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return 'JPEG'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'\x00\x00\x00\x00IEND\xaeB`\x82'):
        return 'PNG'
    if data[:6] in (b'GIF87a', b'GIF89a') and data.endswith(b';'):
        return 'GIF'
    if data.startswith(b'BM') and len(data) >= 26 and struct.unpack_from('<I', data, 2)[0] == len(data):
        return 'BMP'
    if (len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP' and
            struct.unpack_from('<I', data, 4)[0] + 8 == len(data)):
        return 'WEBP'
    if data.startswith(b'wxgf'):
        return 'WXGF'
    return None


def decrypt_dat(data, *, aes_key=None, xor_key=None):
    """Return plaintext, validating the full image later rather than trusting two magic bytes."""
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_BYTES, '图片为空或超过 40 MB 限制。')
    magic = data[:6]
    if magic in (V1, V2):
        require(len(data) >= 31, '图片加密头不完整。')
        clear_size, tail_size = struct.unpack_from('<II', data, 6)
        cipher_size = (clear_size // 16 + 1) * 16
        require(clear_size <= MAX_BYTES and 15 + cipher_size + tail_size <= len(data),
                '图片分段长度与文件不一致。')
        key = V1_KEY if magic == V1 else aes_key
        require(isinstance(key, bytes) and len(key) == 16, '此图片需要本人账号已验证的图片密钥。')
        tail_key = 0x88 if magic == V1 else xor_key
        require(tail_size == 0 or (type(tail_key) is int and 0 <= tail_key <= 255),
                '此图片需要已验证的尾部解码参数。')
        try:
            prefix = unpad(AES.new(key, AES.MODE_ECB).decrypt(data[15:15 + cipher_size]), 16)
        except ValueError:
            raise ImageError('图片密钥或加密数据校验失败。') from None
        require(len(prefix) == clear_size, '图片解密长度与加密头不一致。')
        tail_at = len(data) - tail_size
        tail = bytes(byte ^ tail_key for byte in data[tail_at:]) if tail_size else b''
        return prefix + data[15 + cipher_size:tail_at] + tail
    if signature(data):
        return data
    # Old DAT is one-byte XOR. Infer only from complete magic patterns; validation
    # of the decoded container/pixels is still mandatory before exposing anything.
    for header in (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'BM', b'RIFF'):
        if len(data) < len(header):
            continue
        key = data[0] ^ header[0]
        if bytes(byte ^ key for byte in data[:len(header)]) != header:
            continue
        candidate = bytes(byte ^ key for byte in data)
        if signature(candidate) not in (None, 'WXGF'):
            return candidate
    raise ImageError('未识别的图片格式，原文件已保留。')


def make_preview(plain):
    """Decode pixels and re-encode a local first-frame preview, dropping metadata."""
    require(isinstance(plain, bytes) and 0 < len(plain) <= MAX_BYTES, '图片大小无效。')
    fmt = signature(plain)
    require(fmt is not None, '图片容器校验失败，无法预览。')
    if fmt == 'WXGF':
        raise ImageError('WXGF 动画暂不支持预览，原文件已保留。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(plain)) as image:
                width, height = image.size
                require(0 < width * height <= MAX_PIXELS and image.format == fmt,
                        '图片尺寸过大或格式不一致。')
                animated = getattr(image, 'n_frames', 1) > 1
                image.verify()
            with Image.open(io.BytesIO(plain)) as image:
                image.seek(0)
                image.load()
                output = ImageOps.exif_transpose(image).convert('RGBA' if 'A' in image.getbands() or
                                                               'transparency' in image.info else 'RGB')
                try:
                    output.info.clear()
                    output.thumbnail((2048, 2048))
                    buffer = io.BytesIO()
                    transparent = output.mode == 'RGBA'
                    output.save(buffer, format='PNG' if transparent else 'JPEG', **({} if transparent else {'quality': 88}))
                    data = buffer.getvalue()
                    require(len(data) <= 8 * 1024 * 1024, '图片预览超过大小限制。')
                finally:
                    output.close()
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageError('图片数据不完整或校验失败，无法预览。') from None
    return Preview(data, 'image/png' if transparent else 'image/jpeg', width, height, animated)


def decode_preview(data, **kwargs):
    return make_preview(decrypt_dat(data, **kwargs))
