"""Verified in-place Emilyguitar prototype assets; never installs or enables a mode."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import wave

from .full_decode import _protected_reader

ROOT = Path(__file__).resolve().parent
PROFILE_VERSION = 'emily-basic-rr1-direct-standard20-v1'
OPEN = (40, 45, 50, 55, 59, 64)
MAX_FRET = 20
PITCH_RANGE = (40, 84)
BANK_FILES = 331
WAV_FILES = 323
PRELOAD = 1048576
PINS = {
    'research/emilyguitar-002/acquisition.json': 'f72df01ec524e021ce9c4796ea2d22140abf92c906af1cf0fb28eec3e13a4f55',
    'research/emilyguitar-002/inventory.json': '8e8b4b9bb5b66c9ff05d248310e7441c3d6e6710fdb8fee4f5d1f69d68796dff',
    'research/emilyguitar-probe-001/headers-and-map.json': '72db6bf84c092b2f27c46d925b10a66d2f153dde2553a6d449eb39045903a691',
    'research/emilyguitar-probe-001/native-001/execution.json': 'b585b0ca3d37de7d72aaeaba3dde7e5df2dac7c7debc9217195712bca5f0b08f',
    'research/emilyguitar-probe-001/native-001/result.json': '499ae29bff234dbee37179654d52884bf569a0b53a4d327e2c8c7135d7edafbd',
}
OPCODES = {'lokey', 'hikey', 'pitch_keycenter', 'bend_up', 'bend_down', 'ampeg_release',
           'lovel', 'hivel', 'amp_velcurve_40', 'amp_velcurve_80', 'amp_velcurve_120',
           'sample', 'lorand', 'hirand', 'trigger', 'volume', 'amp_random', 'rt_decay'}


def _check_cancel(cancel):
    if cancel():
        raise InterruptedError('Electric bank verification cancelled')


def _plain_path(path):
    path = Path(path)
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked electric asset paths are unsupported')
    return path


def _member(name):
    if (type(name) is not str or not name or '\\' in name or ':' in name
            or PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/'))):
        raise ValueError('Invalid electric bank member path')
    return name


def _pinned_json(stack, root, relative, expected, cancel):
    _check_cancel(cancel)
    path = _plain_path(root/relative)
    stream = stack.enter_context(_protected_reader(path))
    raw = stream.read(2*1024**2+1)
    if len(raw) > 2*1024**2 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Electric provenance manifest changed: '+path.name)
    return json.loads(raw)


def _configuration(stack, root, cancel):
    root = Path(root).absolute()
    data = [_pinned_json(stack, root, name, pin, cancel) for name, pin in PINS.items()]
    acquired, members, inspection, execution, result = data
    if (acquired.get('state') != 'complete' or acquired.get('wav_git_exact_count') != WAV_FILES
            or acquired.get('inventory_sha256') != list(PINS.values())[1]
            or len(members) != BANK_FILES or len(inspection['headers']) != WAV_FILES
            or result.get('state') != 'measurements_complete_not_musical_acceptance'
            or len(result.get('root_pairs', [])) != 16
            or not all(row.get('exact') is True for row in result['root_pairs'])):
        raise ValueError('Electric acquisition/header/root evidence is incomplete')
    names = [_member(row['path']) for row in members]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('Colliding electric inventory paths')
    if sum(name.endswith('.wav') for name in names) != WAV_FILES:
        raise ValueError('Unexpected electric sample count')
    return {'root': root, 'bank': root/'research/emilyguitar-002/bank', 'members': members,
            'inspection': inspection, 'native': execution['native'],
            'engine': root/'vendor/sfizz/bin/Release/sfizz.dll'}


def _header(stream):
    """Header/chunk validation on the same locked file; no sample analysis."""
    size = os.fstat(stream.fileno()).st_size
    stream.seek(0)
    head = stream.read(12)
    if len(head) != 12 or head[:4] != b'RIFF' or head[8:] != b'WAVE' or struct.unpack_from('<I', head, 4)[0]+8 != size:
        raise ValueError('Invalid electric WAV RIFF geometry')
    position, data_bytes, chunks = 12, [], 0
    while position < size:
        stream.seek(position)
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError('Truncated electric WAV chunk')
        tag, length = struct.unpack('<4sI', raw)
        position += 8+length+(length & 1)
        chunks += 1
        if position > size or chunks > 128:
            raise ValueError('Invalid electric WAV chunk closure')
        if tag == b'data':
            data_bytes.append(length)
    stream.seek(0)
    with wave.open(stream, 'rb') as wav:
        channels, width, rate, frames, compression, _ = wav.getparams()
    if (channels, width, rate, compression) != (1, 3, 44100, 'NONE') or frames <= 0 or data_bytes != [frames*3]:
        raise ValueError('Expected complete mono PCM24/44100 electric sample')
    return {'channels': channels, 'sample_width_bytes': width, 'sample_rate': rate, 'frames': frames, 'bytes': size}


def _hash_handle(stream, size, cancel):
    sha = hashlib.sha256()
    git = hashlib.sha1(b'blob '+str(size).encode()+b'\0')
    stream.seek(0)
    while block := stream.read(1024*1024):
        _check_cancel(cancel)
        sha.update(block)
        git.update(block)
    return sha.hexdigest(), git.hexdigest()


@contextmanager
def protect_bank(cancel=lambda: False, root=ROOT):
    """Verify and hold owned input handles through the caller's native lifetime.

    The calling managed controller may wrap its entire worker lifetime too.
    System libraries are hashed, never locked. This is the observed static
    sfizz import closure, not dynamic dependency/OS attestation.
    """
    with ExitStack() as stack:
        config = _configuration(stack, root, cancel)
        bank = _plain_path(config['bank'])
        actual = set()
        for directory, dirs, files in os.walk(bank, followlinks=False):
            for name in [*dirs, *files]:
                _plain_path(Path(directory)/name)
            actual.update((Path(directory)/name).relative_to(bank).as_posix() for name in files)
        expected = {row['path'] for row in config['members']}
        if actual != expected:
            raise ValueError('Electric bank file closure changed')
        verified_headers = {}
        for row in config['members']:
            _check_cancel(cancel)
            path = _plain_path(bank/_member(row['path']))
            stream = stack.enter_context(_protected_reader(path))
            size = os.fstat(stream.fileno()).st_size
            checksum, git = _hash_handle(stream, size, cancel)
            if size != row['bytes'] or checksum != row['sha256'] or git != row['actual_git_blob_sha1']:
                raise ValueError('Electric bank asset integrity changed: '+row['path'])
            if row['path'] != 'LICENSE' and (row['identity_check'] != 'exact_git_blob' or git != row['expected_git_blob_sha1']):
                raise ValueError('Electric bank Git provenance changed')
            if row['path'].endswith('.wav'):
                header = _header(stream)
                baseline = config['inspection']['headers'][row['path']]
                if any(header[key] != baseline[key] for key in header) or header['frames'] > PRELOAD:
                    raise ValueError('Electric sample header differs from verified inspection')
                verified_headers[row['path']] = header
        byname = {}
        for entry in config['native']['binaries']:
            name = Path(entry['path']).name.casefold()
            byname.setdefault(name, []).append(entry)
        pending, seen, native = ['sfizz.dll'], set(), []
        while pending:
            name = pending.pop().casefold()
            if name in seen or name.startswith(('api-ms-', 'ext-ms-')):
                continue
            seen.add(name)
            candidates = byname[name]
            if len(candidates) != 1:
                raise ValueError('Ambiguous dependency inside the sfizz closure')
            entry = candidates[0]
            path = _plain_path(Path(entry['path']))
            if name == 'sfizz.dll' and path.resolve() != config['engine'].resolve():
                raise ValueError('Electric engine is not the fixed verified engine')
            _check_cancel(cancel)
            if path.is_relative_to(config['root'].parent):
                stream = stack.enter_context(_protected_reader(path))
                checksum, _ = _hash_handle(stream, os.fstat(stream.fileno()).st_size, cancel)
                size = os.fstat(stream.fileno()).st_size
            else:
                with path.open('rb') as stream:
                    size = os.fstat(stream.fileno()).st_size
                    checksum, _ = _hash_handle(stream, size, cancel)
            if size != entry['bytes'] or checksum != entry['sha256']:
                raise ValueError('Electric native dependency changed: '+name)
            native.append({'path': str(path), 'bytes': size, 'sha256': checksum,
                           'held_read_share_only': path.is_relative_to(config['root'].parent)})
            pending.extend(config['native']['imports'][entry['path']])
        mapping, selected = basic_map(config['inspection']['regions'])
        receipt = {'profile_version': PROFILE_VERSION, 'manifest_sha256': dict(PINS),
                   'bank_files_verified': len(config['members']), 'wav_headers_verified': len(verified_headers),
                   'source_wav_git_identities_verified': WAV_FILES, 'native': native,
                   'mapping_sha256': hashlib.sha256(mapping.encode()).hexdigest(),
                   'selected_attack_regions': sum(row['category']=='notes' for row in selected),
                   'selected_release_regions': sum(row['category']=='release' for row in selected),
                   'integrity_checked': True, 'mode_enabled': False,
                   'scope': 'Pinned bank and observed static engine closure; no sound-quality or OS attestation.'}
        yield {**config, 'mapping': mapping, 'selected_regions': selected, 'headers': verified_headers, 'receipt': receipt}


def basic_map(regions):
    """Fixed first variation of author basic DI, with explicit changes recorded."""
    selected = [row for row in regions if row['category'] in ('notes', 'release')
                and row['candidate_40_84'] and row['random_interval'][0] == 0]
    if (sum(row['category']=='notes' for row in selected), sum(row['category']=='release' for row in selected)) != (64, 16):
        raise ValueError('Unexpected fixed electric region coverage')
    output = []
    for row in selected:
        if set(row['opcodes'])-OPCODES:
            raise ValueError('Unsupported electric map opcode')
        values = {key: value for key, value in row['opcodes'].items() if key not in ('lorand', 'hirand', 'amp_random')}
        values.update(sample=_member(row['sample']), amp_random='0', pitch_keycenter=str(row['pitch_keycenter']),
                      lokey=str(max(40, row['key_range'][0])), hikey=str(min(84, row['key_range'][1])))
        output.append('<region>\n'+'\n'.join(key+'='+str(value) for key, value in values.items())+'\n')
    for pitch in range(40, 85):
        for velocity in range(1, 128):
            matches = [row for row in selected if row['category']=='notes' and row['key_range'][0]<=pitch<=row['key_range'][1]
                       and row['velocity_range'][0]<=velocity<=row['velocity_range'][1]]
            if len(matches) != 1:
                raise ValueError('Electric first-variation coverage has a gap or overlap')
    return ''.join(output), selected


def sample_limits(notes, config):
    """Stored-duration warning only; never lengthen a source gate or loop a sample."""
    result = []
    for note in notes:
        row = next(row for row in config['selected_regions'] if row['category']=='notes'
                   and row['key_range'][0]<=note['pitch']<=row['key_range'][1]
                   and row['velocity_range'][0]<=note['velocity']<=row['velocity_range'][1])
        frames = config['headers'][row['sample']]['frames']
        transposed = frames/2**((note['pitch']-row['pitch_keycenter'])/12)
        result.append({'note_id': note['id'], 'region_id': row['id'], 'sample': row['sample'],
                       'stored_frames': frames, 'sample_root_midi': row['pitch_keycenter'],
                       'transposed_stored_frames_estimate': transposed,
                       'gate_exceeds_stored_sample_estimate': note['end']-note['start']>transposed,
                       'limit': 'No loop; stored duration is not an audible sustain or exact engine-tail guarantee.'})
    return result
