"""Lossless private captures for the new public comparison, Windows only.

The caller owns the fixed stage and must have reaped its writers before calling
archive_capture. Any failure stops the batch; occupied artifacts are never
retried, overwritten or cleaned up here. File fsync and no-replace rename do
not establish power-loss/directory-metadata durability.
"""
from contextlib import ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zlib

from .full_decode import _protected_reader

VERSION = 'capture-archive-v1'
ROOT = Path(__file__).resolve().parents[1] / 'work/local-arranger-public8-001'
MAX_CAPTURE_BYTES = 1024**2
MAX_ARCHIVE_BYTES = MAX_CAPTURE_BYTES + 1024
MAX_RECEIPT_BYTES = 4096
CHUNK_BYTES = 65536
SHA = re.compile(r'[0-9a-f]{64}')


def _check(cancel):
    if cancel():
        raise InterruptedError('Capture archival cancelled')


def _plain(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked capture paths are unsupported')
    return path


def _scope(stage, name, freeze_sha256):
    if os.name != 'nt':
        raise RuntimeError('Capture archival requires Windows protected file sharing')
    if type(freeze_sha256) is not str or not SHA.fullmatch(freeze_sha256):
        raise ValueError('Expected exact batch freeze SHA256')
    stage = Path(stage)
    if not stage.is_absolute() or '..' in stage.parts:
        raise ValueError('Expected absolute owned capture stage')
    try:
        relative = stage.relative_to(ROOT)
    except ValueError:
        raise ValueError('Capture stage is outside the new public comparison') from None
    if (len(relative.parts) != 2 or not re.fullmatch(r'development-0[2-8]', relative.parts[0])
            or relative.parts[1] not in ('count', 'generate')):
        raise ValueError('Unsupported public comparison stage')
    operation = relative.parts[1]
    if type(name) is not str or name not in (operation + '.stdout', operation + '.stderr'):
        raise ValueError('Capture name does not belong to this stage')
    _plain(stage)
    if not stage.is_dir():
        raise ValueError('Expected existing owned capture stage')
    return stage, {'freeze_sha256': freeze_sha256, 'ordinal': int(relative.parts[0][-2:]), 'stage': operation}


def _pin(row, maximum, *, name=None, minimum=0):
    keys = {'bytes', 'sha256'} if name is None else {'name', 'bytes', 'sha256'}
    if (type(row) is not dict or set(row) != keys or type(row['bytes']) is not int
            or not minimum <= row['bytes'] <= maximum or type(row['sha256']) is not str
            or not SHA.fullmatch(row['sha256']) or (name is not None and row['name'] != name)):
        raise ValueError('Invalid capture identity')
    return dict(row)


def _ordinary(stream, path, maximum):
    info = os.fstat(stream.fileno())
    _plain(path)
    named = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        raise ValueError('Expected one ordinary, unlinked capture file')
    return info.st_size


def _owned_source(path):
    """Read and later delete this exact file handle using ordinary DELETE rights."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    _plain(path)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ | DELETE; FILE_SHARE_READ; OPEN_EXISTING; OPEN_REPARSE_POINT.
    # Never use DELETE_ON_CLOSE before the verified archive/receipt commit.
    handle = kernel.CreateFileW(str(path), 0x80010000, 1, None, 3, 0x00200080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _mark_delete(stream):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class Disposition(ctypes.Structure):
        _fields_ = [('DeleteFile', ctypes.c_ubyte)]  # Windows BOOLEAN, not BOOL.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    value = Disposition(1)
    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(stream.fileno()), 4,
            ctypes.byref(value), ctypes.sizeof(value)):
        raise ctypes.WinError(ctypes.get_last_error())


def _hash(stream, maximum, cancel):
    stream.seek(0)
    result, length = hashlib.sha256(), 0
    while data := stream.read(CHUNK_BYTES):
        _check(cancel)
        length += len(data)
        if length > maximum:
            raise ValueError('Capture exceeds its byte bound')
        result.update(data)
    return {'bytes': length, 'sha256': result.hexdigest()}


def _consume_archive(stream, archive, raw, cancel, *, collect):
    """One gzip member, exact two identities; bounded output without a raw file."""
    stream.seek(0)
    decoder, compressed, expanded = zlib.decompressobj(31), hashlib.sha256(), hashlib.sha256()
    compressed_length = expanded_length = 0
    result = bytearray() if collect else None
    try:
        while block := stream.read(CHUNK_BYTES):
            _check(cancel)
            compressed_length += len(block)
            if compressed_length > archive['bytes'] or decoder.eof:
                raise ValueError('Oversized or trailing gzip capture data')
            compressed.update(block)
            pending = block
            while pending:
                _check(cancel)
                output = decoder.decompress(pending, min(CHUNK_BYTES, raw['bytes'] - expanded_length + 1))
                expanded_length += len(output)
                if expanded_length > raw['bytes']:
                    raise ValueError('Expanded capture exceeds its exact bound')
                expanded.update(output)
                if collect:
                    result.extend(output)
                if decoder.eof:
                    if decoder.unused_data or decoder.unconsumed_tail:
                        raise ValueError('Concatenated or trailing gzip capture data')
                    break
                pending = decoder.unconsumed_tail
    except zlib.error as error:
        raise ValueError('Corrupt gzip capture') from error
    if (not decoder.eof or compressed_length != archive['bytes'] or compressed.hexdigest() != archive['sha256']
            or expanded_length != raw['bytes'] or expanded.hexdigest() != raw['sha256']):
        raise ValueError('Archived capture failed full byte verification')
    return bytes(result) if collect else None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _binding(value, name, expected, ownership):
    if (type(value) is not dict or set(value) != {'version', 'ownership', 'raw', 'archive', 'receipt'}
            or value['version'] != VERSION or type(value['ownership']) is not dict
            or value['ownership'] != ownership or type(value['ownership'].get('ordinal')) is not int):
        raise ValueError('Capture archive ownership changed')
    raw = _pin(value['raw'], MAX_CAPTURE_BYTES, name=name)
    if {key: raw[key] for key in ('bytes', 'sha256')} != expected:
        raise ValueError('Archived capture does not match the original receipt')
    _pin(value['archive'], MAX_ARCHIVE_BYTES, name=name + '.gz', minimum=20)
    _pin(value['receipt'], MAX_RECEIPT_BYTES, name=name + '.archive.json', minimum=1)
    return {key: value[key] for key in ('version', 'ownership', 'raw', 'archive')}


def _protected(stack, path, maximum):
    _plain(path)
    stream = stack.enter_context(_protected_reader(path))
    _ordinary(stream, path, maximum)
    return stream


def _verify_receipt(stack, stage, binding, payload, cancel):
    row = binding['receipt']
    stream = _protected(stack, stage / row['name'], MAX_RECEIPT_BYTES)
    _check(cancel)
    data = stream.read(MAX_RECEIPT_BYTES + 1)
    if (len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']
            or data != _canonical(payload)):
        raise ValueError('Capture binding receipt changed')


def archive_capture(stage, name, *, expected, freeze_sha256, cancel=lambda: False):
    """Verify/archive one reaped new capture, commit binding, delete exact raw.

    Caller exclusively owns the stage. Return only after the source handle has
    closed successfully. On failure preserve every existing artifact and stop;
    a committed receipt attests verified bytes, never that deletion completed.
    """
    _check(cancel)
    stage, ownership = _scope(stage, name, freeze_sha256)
    expected = _pin(expected, MAX_CAPTURE_BYTES)
    raw_path = stage / name
    archive_path = stage / (name + '.gz')
    archive_partial = stage / (name + '.gz.pending')
    receipt_path = stage / (name + '.archive.json')
    receipt_partial = stage / (name + '.archive.json.pending')
    for path in (archive_path, archive_partial, receipt_path, receipt_partial):
        if path.exists() or path.is_symlink():
            raise FileExistsError('Preserve occupied capture archival transaction: ' + path.name)
    with ExitStack() as stack:
        with _owned_source(raw_path) as source:
            if _ordinary(source, raw_path, MAX_CAPTURE_BYTES) != expected['bytes'] or _hash(source, MAX_CAPTURE_BYTES, cancel) != expected:
                raise ValueError('Raw capture changed before archival')
            source.seek(0)
            with archive_partial.open('xb') as target:
                with gzip.GzipFile(filename='', mode='wb', fileobj=target, compresslevel=9, mtime=0) as compressor:
                    while data := source.read(CHUNK_BYTES):
                        _check(cancel)
                        compressor.write(data)
                target.flush()
                os.fsync(target.fileno())
            with ExitStack() as temporary:
                stream = _protected(temporary, archive_partial, MAX_ARCHIVE_BYTES)
                archive = {'name': archive_path.name, **_hash(stream, MAX_ARCHIVE_BYTES, cancel)}
                _consume_archive(stream, archive, expected, cancel, collect=False)
            _check(cancel)
            _plain(stage)
            os.rename(archive_partial, archive_path)  # Windows no-replace.
            stream = _protected(stack, archive_path, MAX_ARCHIVE_BYTES)
            _consume_archive(stream, archive, expected, cancel, collect=False)
            payload = {'version': VERSION, 'ownership': ownership, 'raw': {'name': name, **expected}, 'archive': archive}
            data = _canonical(payload)
            if len(data) > MAX_RECEIPT_BYTES:
                raise ValueError('Capture binding exceeds its bound')
            with receipt_partial.open('xb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            _check(cancel)
            _plain(stage)
            os.rename(receipt_partial, receipt_path)
            binding = {**payload, 'receipt': {'name': receipt_path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}
            _verify_receipt(stack, stage, binding, payload, cancel)
            if _hash(source, MAX_CAPTURE_BYTES, cancel) != expected:
                raise ValueError('Raw capture changed before exact-handle deletion')
            _check(cancel)  # Last cancellation point, before the deletion commit.
            _mark_delete(source)
        if raw_path.exists() or raw_path.is_symlink():
            raise OSError('Capture deletion was not observed; preserve artifacts and stop')
        return binding


def read_capture(stack, stage, name, *, expected, binding, freeze_sha256, cancel=lambda: False):
    """Return exact bounded raw bytes; keep archive/receipt protected in stack.

    The caller authenticates the outer manifest that contains binding and must
    keep stack alive through its downstream native cleanup. No raw file is made.
    """
    _check(cancel)
    stage, ownership = _scope(stage, name, freeze_sha256)
    expected = _pin(expected, MAX_CAPTURE_BYTES)
    _binding(binding, name, expected, ownership)
    # The caller retains its manifest object; cancellation callbacks must not
    # be able to change validated paths or identities during protected reads.
    binding = json.loads(_canonical(binding))
    payload = _binding(binding, name, expected, ownership)
    if (stage / name).exists() or (stage / name).is_symlink():
        raise ValueError('Raw spool remains; archival transaction did not finish')
    _verify_receipt(stack, stage, binding, payload, cancel)
    stream = _protected(stack, stage / binding['archive']['name'], MAX_ARCHIVE_BYTES)
    return _consume_archive(stream, binding['archive'], expected, cancel, collect=True)
