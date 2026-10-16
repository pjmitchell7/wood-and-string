"""Bounded synthesized-stem geometry/mix verification, without rendering.

Caller-owned renderer lineage is a declaration, not proof of vocal exclusion,
source-note accuracy or acoustic alignment. This module writes no files and
does not publish assets, reserve resources, or enable a playback feature.
"""
import array
from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys

from audio.full_decode import _protected_reader

VERSION = 'stem-bundle-v1'
DEFAULT_MAX_FRAMES = 31 * 44100
DEFAULT_MAX_TOTAL_AUDIO_BYTES = 64 * 1024**2
HARD_MAX_FRAMES = 900 * 44100
HARD_MAX_TOTAL_AUDIO_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 128 * 1024
CHUNK_FRAMES = 4096
SHA = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
BINDING_KEYS = ('source', 'clock', 'arrangement', 'renderer')


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _shape(value, keys):
    _need(type(value) is dict and set(value) == set(keys), 'Unexpected stem metadata fields')


def _text(value, maximum=128):
    return (type(value) is str and 0 < len(value) <= maximum and
            not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _int(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def _gain(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 1
    except OverflowError:
        return False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _policy(max_frames, max_total_audio_bytes):
    _need(_int(max_frames, 1, HARD_MAX_FRAMES) and
          _int(max_total_audio_bytes, 48, HARD_MAX_TOTAL_AUDIO_BYTES), 'Invalid stem verification policy')


def _binding(value):
    _shape(value, BINDING_KEYS)
    source, clock, arrangement, renderer = (value[k] for k in BINDING_KEYS)
    _shape(source, ('id', 'sha256'))
    _need(_text(source['id']) and _sha(source['sha256']), 'Invalid original source identity')
    _shape(clock, ('sample_rate', 'origin_sample', 'body_frames', 'tail_frames', 'frames'))
    _need(type(clock['sample_rate']) is int and clock['sample_rate'] == 44100 and
          _int(clock['body_frames'], 1, HARD_MAX_FRAMES) and
          _int(clock['tail_frames'], 0, HARD_MAX_FRAMES) and
          _int(clock['frames'], 1, HARD_MAX_FRAMES) and
          clock['body_frames'] + clock['tail_frames'] == clock['frames'] and
          _int(clock['origin_sample'], 0, 2**53 - 1 - clock['frames']), 'Invalid common source clock')
    _shape(arrangement, ('revision', 'sha256', 'performers', 'label'))
    _need(_text(arrangement['revision']) and _sha(arrangement['sha256']) and
          _int(arrangement['performers'], 1, 3) and _text(arrangement['label']),
          'Invalid immutable musical arrangement identity')
    _shape(renderer, ('id', 'version', 'receipt_sha256', 'source_kind', 'content_exclusion'))
    _need(_text(renderer['id']) and _text(renderer['version']) and _sha(renderer['receipt_sha256']) and
          renderer['source_kind'] == 'synthesized' and
          renderer['content_exclusion'] == 'declared_not_verified', 'Missing synthesized renderer declaration')


def validate_manifest(value, *, max_frames=DEFAULT_MAX_FRAMES,
                      max_total_audio_bytes=DEFAULT_MAX_TOTAL_AUDIO_BYTES):
    """Return a detached manifest. Validation alone does not verify any media."""
    _policy(max_frames, max_total_audio_bytes)
    _shape(value, ('version', *BINDING_KEYS, 'stems', 'combined'))
    _need(value['version'] == VERSION, 'Unknown stem bundle version')
    _binding({k: value[k] for k in BINDING_KEYS})
    frames = value['clock']['frames']
    _need(frames <= max_frames, 'Stem duration exceeds caller policy')
    stems = value['stems']
    _need(type(stems) is list and 1 <= len(stems) <= 6, 'Expected one to six synthesized stems')
    ids, names = set(), set()

    def file(row):
        _shape(row, ('name', 'bytes', 'sha256', 'format', 'channels', 'sample_rate', 'frames'))
        name = row['name']
        _need(type(name) is str and NAME.fullmatch(name) is not None and not name.endswith('.') and
              name.split('.')[0].upper() not in {'CON', 'PRN', 'AUX', 'NUL',
              *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}, 'Unsafe stem basename')
        _need(name.casefold() not in names, 'Repeated or case-colliding audio filename')
        names.add(name.casefold())
        _need(type(row['bytes']) is int and row['bytes'] == 44 + frames * 4 and _sha(row['sha256']) and
              row['format'] == 'pcm_s16le' and type(row['channels']) is int and row['channels'] == 2 and
              type(row['sample_rate']) is int and row['sample_rate'] == 44100 and
              type(row['frames']) is int and row['frames'] == frames, 'Audio member geometry differs from common interval')

    for stem in stems:
        _shape(stem, ('id', 'instrument', 'role', 'gain', 'default_audible', 'file', 'provenance'))
        _need(type(stem['id']) is str and ID.fullmatch(stem['id']) is not None and
              stem['id'] not in ids, 'Invalid or repeated performer/percussion id')
        ids.add(stem['id'])
        _need(type(stem['instrument']) is str and stem['instrument'] in ('acoustic', 'electric', 'bass', 'drums') and _text(stem['role']) and
              _gain(stem['gain']) and type(stem['default_audible']) is bool, 'Invalid stem role or declared gain')
        file(stem['file'])
        p = stem['provenance']
        _shape(p, ('source_ids', 'assignment', 'adaptations', 'newly_composed', 'source_kind'))
        _need(type(p['source_ids']) is list and len(p['source_ids']) <= 64 and
              all(_text(s) for s in p['source_ids']) and len(set(p['source_ids'])) == len(p['source_ids']) and
              _text(p['assignment']) and type(p['adaptations']) is list and len(p['adaptations']) <= 32 and
              all(_text(s, 256) for s in p['adaptations']) and type(p['newly_composed']) is bool and
              _text(p['source_kind']), 'Invalid orthogonal declared provenance summary')
    _need(sum(s['instrument'] != 'drums' for s in stems) == value['arrangement']['performers'] and
          sum(s['instrument'] == 'drums' for s in stems) <= 3, 'Performer roster differs from arrangement')
    combined = value['combined']
    _shape(combined, ('file', 'mix_point', 'active_stem_ids', 'quantization', 'tolerance_lsb'))
    file(combined['file'])
    active = combined['active_stem_ids']
    _need(type(active) is list and all(type(s) is str for s in active) and len(active) == len(set(active)) and
          set(active) == {s['id'] for s in stems if s['default_audible']}, 'Combined export must bind the exact default active parts')
    _need(combined['mix_point'] == 'linear_pre_master' and
          combined['quantization'] == 'sum_retained_pcm16_then_nearest_ties_even' and
          type(combined['tolerance_lsb']) is int and combined['tolerance_lsb'] == 1, 'Unsupported combined processing point')
    _need(sum(s['file']['bytes'] for s in stems) + combined['file']['bytes'] <= max_total_audio_bytes,
          'Bundle audio exceeds caller byte policy')
    raw = _canonical(value)
    _need(len(raw) <= MAX_MANIFEST_BYTES, 'Stem manifest exceeds bound')
    return json.loads(raw)


def parse_manifest(raw, **policy):
    """Strict whole UTF-8 JSON, including duplicate-key/nonfinite rejection."""
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_MANIFEST_BYTES, 'Invalid manifest bytes')
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, 'Repeated stem manifest key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Nonfinite stem manifest JSON')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Malformed stem manifest JSON') from error
    return validate_manifest(value, **policy)


def _plain(path, directory=False):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        s = item.lstat()
        _need(not stat.S_ISLNK(s.st_mode) and not getattr(s, 'st_file_attributes', 0) & 0x400,
              'Linked stem bundle path')
        if item == path:
            _need(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), 'Nonregular bundle member')
            _need(directory or s.st_nlink == 1, 'Hard-linked bundle member')
    return path


def _signature(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_nlink, s.st_mtime_ns)


def _check(cancel):
    if cancel():
        raise InterruptedError('Stem verification cancelled')


def _hash(stream, cancel):
    stream.seek(0)
    h = hashlib.sha256()
    while data := stream.read(65536):
        _check(cancel)
        h.update(data)
    stream.seek(0)
    return h.hexdigest()


def _header(frames):
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + frames * 4, b'WAVE', b'fmt ',
                       16, 1, 2, 44100, 176400, 4, 16, b'data', frames * 4)


@contextmanager
def protect_bundle(folder, manifest, *, expected_binding, expected_manifest_sha256=None,
                   forbidden_audio_sha256=(), cancel=lambda: False,
                   max_frames=DEFAULT_MAX_FRAMES, max_total_audio_bytes=DEFAULT_MAX_TOTAL_AUDIO_BYTES):
    """Verify and hold ordinary member files until context exit; never publish.

    Caller supplies trusted source/arrangement/renderer context. The canonical
    full-manifest SHA binds render files and mix, unlike the musical note SHA.
    Caller must separately authorize resources and own any staging/commit lock.
    """
    snapshot = validate_manifest(manifest, max_frames=max_frames, max_total_audio_bytes=max_total_audio_bytes)
    _binding(expected_binding)
    _need(_canonical(expected_binding) == _canonical({k: snapshot[k] for k in BINDING_KEYS}),
          'Trusted source/clock/arrangement/renderer binding differs')
    encoded = _canonical(snapshot)
    identity = hashlib.sha256(encoded).hexdigest()
    _need(expected_manifest_sha256 is None or (_sha(expected_manifest_sha256) and
          identity == expected_manifest_sha256), 'Saved full render identity differs')
    _need(type(forbidden_audio_sha256) in (list, tuple, set, frozenset) and
          len(forbidden_audio_sha256) <= 1024 and all(_sha(s) for s in forbidden_audio_sha256), 'Invalid excluded audio identities')
    forbidden = set(forbidden_audio_sha256) | {snapshot['source']['sha256']}
    members = [s['file'] for s in snapshot['stems']] + [snapshot['combined']['file']]
    _need(all(m['sha256'] not in forbidden for m in members), 'Known original/source/master audio cannot be an instrumental member')
    folder = _plain(folder, directory=True)
    frames = snapshot['clock']['frames']
    _check(cancel)
    with ExitStack() as stack:
        held = []
        for member in members:
            _check(cancel)
            path = _plain(folder / member['name'])
            before = path.stat()
            _need(before.st_size == member['bytes'], 'WAV byte length differs')
            stream = stack.enter_context(_protected_reader(path))
            _need(_signature(os.fstat(stream.fileno())) == _signature(before), 'Bundle member identity changed while opening')
            _need(_hash(stream, cancel) == member['sha256'], 'Bundle member hash differs')
            _need(stream.read(44) == _header(frames), 'Expected complete canonical stereo PCM16 WAV header')
            held.append((stream, path, _signature(before)))
        active = set(snapshot['combined']['active_stem_ids'])
        max_error = 0
        max_linear_error = 0.0
        positive_peak = 0.0
        negative_peak = 0.0
        for start in range(0, frames, CHUNK_FRAMES):
            _check(cancel)
            count = min(CHUNK_FRAMES, frames - start) * 2
            mixed = [0.0] * count
            positive = [0.0] * count
            negative = [0.0] * count
            arrays = []
            for stream, _, _ in held:
                data = stream.read(count * 2)
                _need(len(data) == count * 2, 'Truncated aligned WAV data')
                values = array.array('h')
                values.frombytes(data)
                if sys.byteorder != 'little':
                    values.byteswap()
                arrays.append(values)
            for stem, values in zip(snapshot['stems'], arrays[:-1]):
                for i, value in enumerate(values):
                    contribution = value * stem['gain']
                    if contribution >= 0:
                        positive[i] += contribution
                    else:
                        negative[i] += contribution
                    if stem['id'] in active:
                        mixed[i] += contribution
            for i, value in enumerate(arrays[-1]):
                _need(positive[i] <= 32767 and negative[i] >= -32768,
                      'A mute subset or allowed gain reduction could overload; choose explicit safer gains')
                positive_peak = max(positive_peak, positive[i])
                negative_peak = min(negative_peak, negative[i])
                error = abs(value - round(mixed[i]))
                _need(error <= 1, 'Combined export differs from declared retained-PCM linear mix')
                max_error = max(max_error, error)
                max_linear_error = max(max_linear_error, abs(value - mixed[i]))
        for member, (stream, path, signature) in zip(members, held):
            _need(stream.read(1) == b'', 'Unexpected WAV trailing bytes')
            _need(_hash(stream, cancel) == member['sha256'] and
                  _signature(os.fstat(stream.fileno())) == signature and
                  _signature(_plain(path).stat()) == signature, 'Bundle member changed during verification')
        _check(cancel)
        yield {'manifest': snapshot, 'verification': {
            'version': 'stem-verification-v1', 'manifest_sha256': identity, 'canonical_manifest_bytes': len(encoded),
            'source': json.loads(_canonical(snapshot['source'])),
            'arrangement': json.loads(_canonical(snapshot['arrangement'])),
            'clock': json.loads(_canonical(snapshot['clock'])),
            'members': len(members), 'total_audio_bytes': sum(m['bytes'] for m in members),
            'policy': {'max_frames': max_frames, 'max_total_audio_bytes': max_total_audio_bytes},
            'common_geometry_and_file_hashes_verified': True, 'linear_default_mix_verified': True,
            'maximum_quantized_error_lsb': max_error, 'maximum_linear_residual_lsb': max_linear_error,
            'all_mute_subsets_and_gain_reductions_within_pcm_range': True,
            'maximum_positive_subset_lsb': positive_peak, 'minimum_negative_subset_lsb': negative_peak,
            'source_clock_acoustic_alignment_verified': False, 'source_or_vocal_content_exclusion_verified': False,
            'renderer_execution_independently_verified': False, 'musical_acceptance': False}}


def verify_bundle(folder, manifest, **kwargs):
    """Completed verification snapshot; handles close before return."""
    with protect_bundle(folder, manifest, **kwargs) as result:
        return result
