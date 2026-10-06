"""Verified main-database decryption only; no WAL, hooks or debug dumps.

Adapted from the validated page layout in the source workspace's
wechat-export/decrypt_db.py. See PROVENANCE.md.
"""
import hashlib
import hmac
import os
from pathlib import Path
import struct
from Crypto.Cipher import AES

PAGE_SIZE = 4096
RESERVE = 80
USABLE = PAGE_SIZE - RESERVE


def derive_keys(key, salt, key_is_derived):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError('Database key must contain exactly 32 bytes.')
    enc = key if key_is_derived else hashlib.pbkdf2_hmac('sha512', key, salt, 256000, 32)
    mac = hashlib.pbkdf2_hmac('sha512', enc, bytes(b ^ 0x3a for b in salt), 2, 32)
    return enc, mac


def decrypt_db(db_path, key, out_path=None, *, key_is_derived=False):
    """Verify every page before publishing any decrypted file.

    Main database snapshots only. The caller performs SQLite quick_check.
    Existing outputs are never overwritten.
    """
    destination = Path(out_path) if out_path is not None else None
    if destination is not None and destination.exists():
        raise FileExistsError('Decrypted output already exists.')
    output = bytearray()
    with Path(db_path).open('rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size < PAGE_SIZE or before.st_size % PAGE_SIZE:
            raise ValueError('Database snapshot is incomplete or not page aligned.')
        page_count = before.st_size // PAGE_SIZE
        first = stream.read(PAGE_SIZE)
        enc, mac_key = derive_keys(key, first[:16], key_is_derived)
        stream.seek(0)
        for number in range(1, page_count + 1):
            page = stream.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE:
                raise ValueError('Database changed or a page is incomplete.')
            start = 16 if number == 1 else 0
            expected = hmac.new(mac_key, page[start:USABLE+16] + struct.pack('<I', number), 'sha512').digest()
            if not hmac.compare_digest(expected, page[USABLE+16:]):
                raise ValueError(f'Database page {number} failed its MAC check; no decrypted file was written.')
            clear = AES.new(enc, AES.MODE_CBC, page[USABLE:USABLE+16]).decrypt(page[start:USABLE])
            if number == 1:
                clear = b'SQLite format 3\x00' + clear
                if int.from_bytes(clear[16:18], 'big') != PAGE_SIZE or clear[20] != RESERVE:
                    raise ValueError('Unsupported database page layout.')
            output.extend(clear + page[USABLE:])
        after = os.fstat(stream.fileno())
        if stream.read(1) or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Database changed during decryption; retry with a stable snapshot.')
    if destination is not None:
        with destination.open('xb') as stream:
            stream.write(output)
    print(f'MAC verified: {page_count}/{page_count} pages')
    return bytes(output)
