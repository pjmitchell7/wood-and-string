"""Read-only model evidence for phrase boundaries, never a musical decision.

The pure API reads copied validated arrays. The saved-job loader protects the
original and exact NPZ while checking bounded ZIP/NPY headers and payloads.
Neither API edits notes, identifies an instrument, chooses focus or renders.
"""
import ast
from bisect import bisect_left, bisect_right
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import uuid
import zipfile
import zlib

import numpy as np

from audio.full_decode import _protected_reader
from instrumental_preview import (validate_bundle, source_anchors, canonical as parent_canonical,
                                  MAX_RESULT, MODEL_SHA, PIPELINE_VERSION)

VERSION = 'instrumental-phrase-evidence-v1'
MAX_REPORT = 1024**2
MAX_ARCHIVE = 16*1024**2
MAX_SOURCE = 250*1024**2
MAX_HEADER = 4096
WIDTHS = {'note': 88, 'onset': 88, 'contour': 264}
SHA = re.compile('[0-9a-f]{64}')


class PhraseEvidenceError(ValueError):
    """A bounded refusal; saved jobs and evidence are never changed."""


def _need(ok, message):
    if not ok:
        raise PhraseEvidenceError(message)


def _check(cancel):
    if cancel():
        raise InterruptedError('Phrase evidence cancelled.')


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _request(request, origin, end):
    _need(type(request) is dict and set(request) ==
          {'start_sample', 'end_sample', 'pitches', 'context_frames'}, 'Exact phrase request required')
    _need(_int(request['start_sample'], origin, end-1)
          and _int(request['end_sample'], request['start_sample']+1, end)
          and _int(request['context_frames'], 0, 8), 'Phrase span or context exceeds source bounds')
    pitches = request['pitches']
    _need(type(pitches) is list and 1 <= len(pitches) <= 8
          and all(_int(p, 21, 108) for p in pitches) and len(set(pitches)) == len(pitches),
          'Expected one to eight unique MIDI pitches')
    return deepcopy(request)


def _bundle(bundle, cancel):
    _check(cancel)
    _need(type(bundle) is dict and _sha(bundle.get('source_sha256'))
          and type(bundle.get('source_id')) is str and 1 <= len(bundle['source_id']) <= 256
          and _int(bundle.get('origin'), 0, 2**53-1)
          and _int(bundle.get('end'), bundle['origin']+1, 2**53-1), 'Invalid phrase source identity')
    try:
        value = validate_bundle(bundle, source_id=bundle['source_id'], source_sha256=bundle['source_sha256'],
                                origin=bundle['origin'], end=bundle['end'])
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise PhraseEvidenceError('Invalid saved candidate bundle') from None
    _check(cancel)
    for report in (*value['interpretations'].values(), {'source_clock': value['raw_activations']}):
        _need(all(type(sample) is int for sample in report['source_clock']['frame_samples']),
              'Exact integer frame anchors required')
    return value


def _arrays(arrays, frames, cancel):
    _need(type(arrays) is dict and set(arrays) == set(WIDTHS), 'Exact activation matrices required')
    copied, hashes = {}, {}
    for key, width in WIDTHS.items():
        _check(cancel)
        source = arrays[key]
        _need(type(source) is np.ndarray and source.shape == (frames, width)
              and source.dtype.str == '<f4', 'Activation shape or float32 byte order differs')
        target = np.empty((frames, width), dtype='<f4')
        digest = hashlib.sha256()
        for start in range(0, frames, 64):
            _check(cancel)
            target[start:start+64] = source[start:start+64]
            row = target[start:start+64]
            _need(bool(np.isfinite(row).all()) and not bool((row < 0).any())
                  and not bool((row > 1).any()), 'Nonfinite or out-of-range activation')
            digest.update(target[start:start+64].tobytes(order='C'))
        copied[key], hashes[key] = target, digest.hexdigest()
    return copied, hashes


def _bounded(report, cancel):
    _check(cancel)
    try:
        raw = _canonical(report)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise PhraseEvidenceError('Phrase report is not bounded finite JSON') from None
    _need(len(raw) <= MAX_REPORT, 'Phrase report exceeds one MiB; narrow the request')
    _check(cancel)
    return report


def phrase_boundary_evidence(bundle, arrays, request, *, cancel=lambda: False):
    """Detached evidence on exact source anchors; no I/O or input mutation.

    Raw-array hashes bind this call's supplied copies. Only the saved-job loader
    additionally establishes their relationship to the declared NPZ identity.
    """
    _need(callable(cancel), 'Cancellation callback required')
    bundle = _bundle(bundle, cancel)
    origin, end = bundle['origin'], bundle['end']
    request = _request(request, origin, end)
    anchors = source_anchors(origin, end)
    count = len(anchors)-1
    arrays, hashes = _arrays(arrays, count, cancel)
    start, stop, context = (request[k] for k in ('start_sample', 'end_sample', 'context_frames'))
    first = min(count, bisect_right(anchors, start)-1)
    last = min(count, bisect_left(anchors, stop))
    covered = first < last and start < anchors[-1]
    low, high = max(0, first-context), min(count, last+context)
    indices = list(range(low, high))
    span = [anchors[low], anchors[high]]
    report = {'version': VERSION,
        'binding': {'authority': 'supplied_bundle_and_arrays', 'parent': None,
            'bundle_sha256': _digest(bundle), 'array_sha256': hashes,
            'source': {'id': bundle['source_id'], 'sha256': bundle['source_sha256']},
            'npz': {'sha256': bundle['raw_activations']['sha256'],
                    'bytes': bundle['raw_activations']['bytes'], 'format': 'numpy-npz-v1'},
            'model_sha256': MODEL_SHA, 'pipeline_version': PIPELINE_VERSION,
            'policies': {name: {'interpretation_sha256': _digest(value),
                'policy_sha256': _digest({k: value[k] for k in ('version', 'policy', 'settings', 'model_sha256') if k in value}),
                'declaration': deepcopy({k: value[k] for k in ('version', 'policy', 'settings', 'model_sha256') if k in value})}
                for name, value in bundle['interpretations'].items()}},
        'request': request, 'source_part': deepcopy(bundle['source_part']),
        'clock': {'sample_rate': 44100, 'origin_sample': origin, 'end_sample': end,
            'model_end_sample': anchors[-1], 'model_frames': count,
            'bracket': {'first_frame': first if covered else None,
                'end_boundary_index': last if covered else None,
                'samples': [anchors[first], anchors[last]] if covered else None},
            'context': {'first_frame': low, 'end_frame_exclusive': high, 'samples': span,
                'left_missing_frames': max(0, context-first),
                'right_missing_frames': max(0, last+context-count)},
            'censoring': {'query_starts_inside_frame': covered and start != anchors[first],
                'query_ends_inside_frame': covered and stop < anchors[last],
                'uncovered_query_samples': [max(start, anchors[-1]), stop] if stop > anchors[-1] else None,
                'at_parent_start': start == origin, 'at_parent_end': stop == end}},
        'frames': {'indices': indices, 'samples': anchors[low:high], 'end_samples': anchors[low+1:high+1],
            'in_query': [anchors[i] < stop and anchors[i+1] > start for i in indices]},
        'traces': {}, 'interpretations': {},
        'semantics': {'observed': 'Uncalibrated model cells and unchanged decoder proposals, not source ground truth.',
            'pending': ['attack versus continuing pitch', 'rest versus estimator or recording dropout',
                        'harmonic or competing voice attribution', 'physical duration and phrase focus'],
            'decisions': 'No events merged, retimed, ranked, omitted or assigned a new velocity/role/focus.',
            'contour': 'Three raw model bins, not physical frequency, bend, technique or instrument identity.',
            'peak_rule': 'Existing onset-only head >=0.5, >previous and >=next; model endpoints use its boundary rule.',
            'competition': 'All events overlapping returned context or query; neighbor=within two semitones, octave=exactly twelve.'}}
    for pitch in request['pitches']:
        _check(cancel)
        column = pitch-21
        center = column*3
        bins = list(range(max(0, min(261, center-1)), max(0, min(261, center-1))+3))
        note = arrays['note'][low:high, column].tolist()
        onset = arrays['onset'][low:high, column].tolist()
        head = arrays['onset'][:, column]
        peaks = []
        for i in indices:
            _check(cancel)
            peaks.append(bool(head[i] >= .5 and (i == 0 or head[i] > head[i-1])
                              and (i == count-1 or head[i] >= head[i+1])))
        report['traces'][str(pitch)] = {'note': note, 'onset': onset,
            'contour': {'bins': bins, 'midi_coordinates': [21+b/3 for b in bins],
                'center_bin': center, 'edge_adjusted': bins != [center-1, center, center+1],
                'values': arrays['contour'][low:high, bins].tolist()},
            'model_predicates': {'note_ge_0_3': [v >= .3 for v in note],
                'raw_onset_ge_0_5': [v >= .5 for v in onset], 'onset_only_peak': peaks}}
    for name, interpretation in bundle['interpretations'].items():
        events = []
        for event in interpretation['events']:
            _check(cancel)
            if not event['start'] < max(stop, span[1]) or not event['end'] > min(start, span[0]):
                continue
            pitch = event['pitch']
            relations = []
            for label, predicate in (
                    ('requested', lambda p: p == pitch),
                    ('within_two_semitones', lambda p: 0 < abs(p-pitch) <= 2),
                    ('one_octave', lambda p: abs(p-pitch) == 12)):
                if any(predicate(p) for p in request['pitches']): relations.append(label)
            events.append({'event': deepcopy(event), 'relations': relations or ['other_overlapping_pitch'],
                'overlaps_query': event['start'] < stop and event['end'] > start,
                'source_boundary_flags': {'starts_at_parent_origin': event['start'] == origin,
                    'ends_at_model_boundary': event['end'] == anchors[-1],
                    'extends_left_of_query': event['start'] < start, 'extends_right_of_query': event['end'] > stop}})
        report['interpretations'][name] = {'events': events}
    return _bounded(report, cancel)


def _plain(path, directory=False):
    path = Path(path)
    _need(path.is_absolute() and '..' not in path.parts and not str(path).startswith(('\\\\', '//')),
          'Expected ordinary local Library path')
    for part in (path, *path.parents):
        info = part.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
              'Linked phrase evidence path refused')
        if part == path:
            _need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                  'Ordinary phrase evidence file/directory required')
    return path


@contextmanager
def _directory(path):
    """Protect parent namespace in place; no acquisition helper dependency."""
    import ctypes as C
    from ctypes import wintypes as W
    _need(os.name == 'nt', 'Protected phrase loading requires Windows')
    path = _plain(path, True)
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]; kernel.CloseHandle.restype = W.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    kernel.GetFileInformationByHandleEx.restype = W.BOOL
    handle = kernel.CreateFileW(str(path), 1, 3, None, 3, 0x02200000, None)
    if handle == C.c_void_p(-1).value: raise C.WinError(C.get_last_error())
    try:
        class FileId(C.Structure):
            _fields_ = [('volume', C.c_uint64), ('identifier', C.c_ubyte*16)]
        value = FileId()
        if not kernel.GetFileInformationByHandleEx(handle, 18, C.byref(value), C.sizeof(value)):
            raise C.WinError(C.get_last_error())
        named = path.stat()
        _need((value.volume, int.from_bytes(value.identifier, 'little')) == (named.st_dev, named.st_ino),
              'Held phrase directory differs')
        yield
    finally:
        if not kernel.CloseHandle(handle): raise C.WinError(C.get_last_error())


def _hold(stack, path, maximum, expected, cancel):
    path = _plain(path)
    seen = getattr(stack, '_phrase_parents', set())
    stack._phrase_parents = seen
    for parent in reversed(path.parents):
        if parent not in seen:
            stack.enter_context(_directory(parent)); seen.add(parent)
    before = path.stat()
    stream = stack.enter_context(_protected_reader(path))
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
    _need(0 < before.st_size <= maximum and signature(before) == signature(os.fstat(stream.fileno())),
          'Protected phrase file geometry differs')
    observed = hashlib.sha256()
    while raw := stream.read(65536):
        _check(cancel); observed.update(raw)
    _need({'bytes': before.st_size, 'sha256': observed.hexdigest()} == expected,
          'Protected phrase file hash/length differs')
    def unchanged():
        _check(cancel)
        _need(signature(path.stat()) == signature(before)
              and signature(os.fstat(stream.fileno())) == signature(before), 'Held phrase bytes changed')
    unchanged(); stream.seek(0)
    return stream, unchanged


def _zip(stream, size, frames, cancel):
    """Three bounded ZIP entries before decompressing any member prefix."""
    _check(cancel)
    _need(size >= 22, 'Incomplete activation archive')
    stream.seek(size-22)
    end = stream.read(22)
    signature, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack('<4s4H2LH', end)
    _need(signature == b'PK\x05\x06' and (disk, cd_disk, disk_count, count, comment) == (0, 0, 3, 3, 0)
          and 0 < cd_size <= 4096 and cd_offset+cd_size == size-22,
          'Unsupported activation archive envelope')
    archive = zipfile.ZipFile(stream)
    try:
        entries = archive.infolist()
        _need(len(entries) == 3 and {e.filename for e in entries} == {k+'.npy' for k in WIDTHS},
              'Exact activation archive members required')
        entries.sort(key=lambda e: e.header_offset)
        cursor, offsets = 0, {}
        for entry in entries:
            _check(cancel)
            _need(entry.header_offset == cursor and entry.flag_bits in (0, 0x800)
                  and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                  and not entry.is_dir() and len(entry.extra) <= 128 and not entry.comment,
                  'Unsupported or overlapping activation member')
            key = entry.filename[:-4]; payload = frames*WIDTHS[key]*4
            _need(payload+10 <= entry.file_size <= payload+MAX_HEADER
                  and 0 < entry.compress_size <= MAX_ARCHIVE, 'Activation member exceeds declared shape')
            stream.seek(cursor); local = stream.read(30)
            _need(len(local) == 30, 'Incomplete activation local header')
            sig, version, flags, method, stamp, date, crc, compressed, expanded, name_len, extra_len = struct.unpack('<4s5H3L2H', local)
            _need(sig == b'PK\x03\x04' and flags == entry.flag_bits and method == entry.compress_type
                  and 0 < name_len <= 64 and extra_len <= 128 and crc == entry.CRC,
                  'Activation local/central headers differ')
            name, extra = stream.read(name_len), stream.read(extra_len)
            _need(name == entry.filename.encode('ascii'), 'Activation local name differs')
            if (expanded, compressed) == (0xffffffff, 0xffffffff):
                _need(len(extra) == 20 and extra[:4] == b'\x01\x00\x10\x00'
                      and struct.unpack('<QQ', extra[4:]) == (entry.file_size, entry.compress_size),
                      'Invalid bounded ZIP64 local sizes')
            else:
                _need((expanded, compressed) == (entry.file_size, entry.compress_size) and not extra,
                      'Activation local sizes differ')
            offsets[entry.filename] = cursor+30+name_len+extra_len
            cursor = offsets[entry.filename]+entry.compress_size
            _need(cursor <= cd_offset, 'Activation member overlaps central directory')
        _need(cursor == cd_offset, 'Unreferenced activation archive bytes')
        return archive, offsets
    except BaseException:
        archive.close(); raise


def _header(member, shape, size, cancel):
    _check(cancel)
    magic = member.read(8)
    _need(magic[:6] == b'\x93NUMPY' and magic[6:] in (b'\x01\x00', b'\x02\x00'), 'Unsupported NPY version')
    width = 2 if magic[6] == 1 else 4
    raw_length = member.read(width)
    _need(len(raw_length) == width, 'Incomplete NPY header length')
    length = int.from_bytes(raw_length, 'little')
    _need(0 < length <= MAX_HEADER-8-width, 'NPY header exceeds bound')
    raw = member.read(length)
    _need(len(raw) == length and raw.endswith(b'\n'), 'Incomplete NPY header')
    tree = ast.parse(raw.decode('latin1').strip(), mode='eval').body
    _need(isinstance(tree, ast.Dict) and len(tree.keys) == 3
          and {ast.literal_eval(k) for k in tree.keys} == {'descr', 'fortran_order', 'shape'},
          'Unexpected or repeated NPY header fields')
    value = ast.literal_eval(tree)
    _need(value['descr'] == '<f4' and value['fortran_order'] is False
          and type(value['shape']) is tuple and all(type(n) is int for n in value['shape'])
          and value['shape'] == shape and size == 8+width+length+shape[0]*shape[1]*4,
          'NPY dtype, shape, order or payload length differs')
    _check(cancel)


class _Member:
    """Exact raw member consumption; ZipExtFile can hide excess inflated data."""
    def __init__(self, stream, entry, offset, cancel):
        self.stream, self.entry, self.offset, self.cancel = stream, entry, offset, cancel
        self.position = self.total = self.crc = 0
        self.pending = b''; self.done = False
        self.decoder = zlib.decompressobj(-15) if entry.compress_type == zipfile.ZIP_DEFLATED else None
        _need(self.decoder is not None or entry.compress_size == entry.file_size,
              'Stored activation member sizes differ')

    def read(self, size):
        chunks, remaining = [], size
        while remaining and not self.done:
            _check(self.cancel)
            if self.pending:
                data = self.pending
            else:
                count = min(4096, self.entry.compress_size-self.position)
                self.stream.seek(self.offset+self.position)
                data = self.stream.read(count)
                _need(len(data) == count, 'Truncated compressed activation member')
                self.position += count
            if self.decoder is None:
                out, self.pending = data[:remaining], data[remaining:]
            else:
                out = self.decoder.decompress(data, remaining)
                self.pending = self.decoder.unconsumed_tail
            self.total += len(out); self.crc = zlib.crc32(out, self.crc)
            _need(self.total <= self.entry.file_size, 'Activation inflates beyond declared payload')
            chunks.append(out); remaining -= len(out)
            ended = (self.decoder.eof if self.decoder is not None else
                     self.position == self.entry.compress_size and not self.pending)
            if ended:
                _need(self.position == self.entry.compress_size and not self.pending
                      and (self.decoder is None or not self.decoder.unused_data)
                      and self.total == self.entry.file_size and self.crc == self.entry.CRC,
                      'Activation compressed extent, expanded length or CRC differs')
                self.done = True
            elif not data and not out:
                raise PhraseEvidenceError('Truncated activation deflate stream')
        return b''.join(chunks)


def _load_arrays(stream, size, frames, cancel):
    # Only requested header bytes inflate before all three headers pass.
    archive, offsets = _zip(stream, size, frames, cancel)
    with archive:
        opened = {}
        for key, width in WIDTHS.items():
            entry = archive.getinfo(key+'.npy')
            member = _Member(stream, entry, offsets[entry.filename], cancel)
            _header(member, (frames, width), entry.file_size, cancel)
            opened[key] = member
        arrays = {}
        for key, width in WIDTHS.items():
            array = np.empty((frames, width), dtype='<f4')
            target = memoryview(array).cast('B'); offset = 0
            while offset < len(target):
                _check(cancel)
                raw = opened[key].read(min(65536, len(target)-offset))
                _need(raw and len(raw) % 4 == 0, 'Incomplete activation payload')
                values = np.frombuffer(raw, dtype='<f4')
                _need(bool(np.isfinite(values).all()) and not bool((values < 0).any())
                      and not bool((values > 1).any()), 'Nonfinite or out-of-range saved activation')
                target[offset:offset+len(raw)] = raw; offset += len(raw)
            _need(opened[key].read(1) == b'', 'Trailing activation payload')
            arrays[key] = array
        return arrays


def load_phrase_boundary_evidence(library, job_id, expected_parent_result_sha256, request, *, cancel=lambda: False):
    """Read a fixed complete instrumental parent; caller supplies trusted hash.

    Uses the passed Library lookup capability without constructing a Library,
    writing cache/state, scanning directories or accepting filesystem paths.
    """
    _need(callable(cancel), 'Cancellation callback required')
    _check(cancel)
    try:
        _need(type(job_id) is str and str(uuid.UUID(job_id)) == job_id
              and _sha(expected_parent_result_sha256), 'Exact parent UUID and trusted result hash required')
        with library.connect() as db:
            sizes = db.execute('SELECT length(CAST(request AS BLOB)),length(CAST(result AS BLOB)) FROM jobs WHERE id=?', (job_id,)).fetchone()
        _need(sizes is not None and type(sizes[0]) is int and 0 < sizes[0] <= MAX_RESULT
              and type(sizes[1]) is int and 0 < sizes[1] <= MAX_RESULT, 'Saved parent metadata exceeds bound')
        job = library.job(job_id)
        _need(job['id'] == job_id and job['state'] == 'complete' and job['request'].get('kind') == 'instrumental',
              'Completed instrumental review required')
        parent_hash = hashlib.sha256(parent_canonical(job['result'])).hexdigest()
        _need(parent_hash == expected_parent_result_sha256, 'Trusted parent result identity differs')
        spec = job['request']
        _need(type(spec.get('asset_id')) is str and str(uuid.UUID(spec['asset_id'])) == spec['asset_id']
              and _sha(spec.get('sha256')), 'Invalid saved original association')
        times = [spec.get(k) for k in ('start_seconds', 'end_seconds')]
        _need(all(type(t) in (int, float) and math.isfinite(t) for t in times)
              and 0 <= times[0] < times[1] <= times[0]+10, 'Saved parent passage differs')
        origin, end = (round(t*44100) for t in times)
        source = library.get(spec['asset_id'])
        _need(source['id'] == spec['asset_id'] and source['kind'] == 'original'
              and source['sha256'] == spec['sha256'] and _sha(source['sha256'])
              and _int(source.get('size'), 1, MAX_SOURCE), 'Original descriptor differs')
        bundle = _bundle(job['result']['candidates'], cancel)
        _need(bundle['source_id'] == source['source_id'] and bundle['source_sha256'] == source['sha256']
              and (bundle['origin'], bundle['end']) == (origin, end), 'Candidate/source clock association differs')
        reference = job['result']['evidence']
        raw = bundle['raw_activations']
        _need(reference == {'sha256': raw['sha256'], 'bytes': raw['bytes'], 'format': 'numpy-npz-v1'},
              'Saved activation reference differs')
        request = _request(request, origin, end)
        with ExitStack() as held:
            _, source_guard = _hold(held, Path(library.media)/source['sha256'], MAX_SOURCE,
                                    {'bytes': source['size'], 'sha256': source['sha256']}, cancel)
            stream, archive_guard = _hold(held, Path(library.root)/'analysis-evidence'/reference['sha256'],
                                         MAX_ARCHIVE, {'bytes': reference['bytes'], 'sha256': reference['sha256']}, cancel)
            arrays = _load_arrays(stream, reference['bytes'], len(raw['frame_samples']), cancel)
            report = phrase_boundary_evidence(bundle, arrays, request, cancel=cancel)
            report['binding'].update(authority='verified_saved_instrumental_job',
                parent={'job_id': job_id, 'result_sha256': parent_hash, 'request_sha256': _digest(spec)})
            source_guard(); archive_guard()
            _need(library.job(job_id) == job and library.get(source['id']) == source,
                  'Saved metadata changed during phrase read')
            return _bounded(report, cancel)
    except InterruptedError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError, RecursionError,
            SyntaxError, UnicodeError, zipfile.BadZipFile, EOFError, struct.error, zlib.error):
        raise PhraseEvidenceError('Saved phrase evidence failed verification; parent is unchanged.') from None
