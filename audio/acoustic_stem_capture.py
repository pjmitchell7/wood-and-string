"""Optional observer of the existing acoustic render; no native calls or publication.

Operational failures are sticky companion refusals. The caller continues its
unchanged master render and owns worker.lock, disk admission and publication.
"""
import array
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from .full_decode import _protected_reader
from .render import RENDER_VERSION
from stem_evidence import verify_bundle

VERSION = 'acoustic-stem-capture-v1'
RATE = 44100
MAX_FRAMES = 31 * RATE
MAX_BLOCK = 256
MAX_ARRANGEMENT_BYTES = 12 * 1024**2
MAX_RECEIPT_BYTES = 2 * 1024**2
MAX_MANIFEST_BYTES = 128 * 1024
MAX_VERIFICATION_BYTES = 16 * 1024
MAX_DIAGNOSTIC_BYTES = 8 * 1024
METADATA_BYTES = MAX_RECEIPT_BYTES + MAX_MANIFEST_BYTES + MAX_VERIFICATION_BYTES + MAX_DIAGNOSTIC_BYTES
SHA = re.compile(r'[0-9a-f]{64}')
COMMAND_KEYS = ('id', 'start', 'end', 'pitch', 'velocity', 'player', 'string', 'fret', 'role')


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _text(value):
    return (type(value) is str and 0 < len(value) <= 128 and
            not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _canonical(value, maximum):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                     allow_nan=False).encode('utf-8')
    _need(len(raw) <= maximum, 'Capture metadata exceeds its byte bound')
    return raw


def forecast_capture(*, performers, frames):
    """Conservative EXTRA disk allowance, including a later verified-copy publish.

    The original render's 8*frames float + 4*frames+44 PCM allowance is separate.
    This is a size forecast, never a reservation or a free-disk observation.
    """
    _need(_integer(performers, 1, 2) and _integer(frames, 1, MAX_FRAMES), 'Invalid capture resource geometry')
    wav = 44 + frames * 4
    floats = performers * frames * 16  # Stereo float64, preserving Python sums.
    pcm = (performers + 1) * wav
    return {'performers': performers, 'frames': frames, 'performer_float_bytes': floats,
            'bundle_audio_bytes': pcm, 'metadata_bytes': METADATA_BYTES,
            'staging_bytes': floats + pcm + METADATA_BYTES,
            'additional_disk_bytes': floats + 2 * (pcm + METADATA_BYTES),
            'original_render_allowance_bytes': frames * 12 + 44,
            'capture_block_numeric_bytes': MAX_BLOCK * 2 * 8 * performers + MAX_BLOCK * 2 * 4,
            'scope': 'Extra owned files plus one publication copy; excludes caller inputs, Python object overhead and Library reserve'}


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
              'Linked acoustic capture path')
        if item == path:
            _need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), 'Nonregular acoustic capture path')
            _need(directory or info.st_nlink == 1, 'Hard-linked acoustic capture file')
    return path


class AcousticStemCapture:
    """Passive per-instance capture with sticky, non-throwing runtime refusal.

    begin_block(start, count); add_instance(player, string, left, right) in the
    existing sorted native instance order; end_block(existing_float32_values).
    No method modifies caller buffers or calls the renderer. Missing/invalid
    constructor input also returns a refused object, so adoption can be optional.
    """
    def __init__(self, folder, *, source, arrangement, revision, performers,
                 origin_sample, total_frames, max_work_bytes, cancel=lambda: False):
        self.state = 'capturing'
        self.folder = None
        self._folder_identity = None
        self._owned = {}
        self._streams = {}
        self._float_hashes = {}
        self._block = None
        self._covered = 0
        self._master_peak = 0.0
        self._master_hash = hashlib.sha256()
        self._outcome = None
        self._diagnosis = None
        self._cancel = cancel
        self._stage = 'admission'
        try:
            _need(callable(cancel), 'Capture cancellation callback is required')
            self.forecast = forecast_capture(performers=len(performers), frames=total_frames)
            _need(type(performers) in (tuple, list) and all(type(player) is int for player in performers) and
                  list(performers) == list(range(1, len(performers) + 1)),
                  'Expected the exact one/two performer roster')
            _need(_integer(max_work_bytes, 1, 2**53 - 1) and max_work_bytes >= self.forecast['additional_disk_bytes'],
                  'Caller capture byte allowance is too small')
            _need(_integer(origin_sample, 0, 2**53 - 1 - total_frames) and total_frames > RATE,
                  'Invalid common acoustic capture interval')
            _need(type(source) is dict and set(source) == {'id', 'sha256'} and _text(source['id']) and
                  type(source['sha256']) is str and SHA.fullmatch(source['sha256']), 'Actual original source identity is required')
            _need(_text(revision), 'Actual arrangement revision is required')
            raw_arrangement = _canonical(arrangement, MAX_ARRANGEMENT_BYTES)
            self.arrangement = json.loads(raw_arrangement)
            self.source = deepcopy(source)
            self.revision = revision
            self.players = tuple(performers)
            self.origin = origin_sample
            self.frames = total_frames
            self.arrangement_sha256 = hashlib.sha256(raw_arrangement).hexdigest()
            self._validate_arrangement()
            self._check()
            target = Path(folder).absolute()
            _plain(target.parent, directory=True)
            target.mkdir()  # An existing directory is never adopted or cleaned.
            self.folder = target
            info = target.stat()
            self._folder_identity = (info.st_dev, info.st_ino)
            for player in self.players:
                self._streams[player] = self._new(f'performer-{player}.f64')
                self._float_hashes[player] = hashlib.sha256()
            self._stage = 'capture'
        except Exception as error:
            self._refuse(error)

    def _validate_arrangement(self):
        a = self.arrangement
        _need(type(a) is dict and a.get('instrument', 'acoustic') == 'acoustic' and
              a.get('sample_rate') == RATE and type(a.get('sample_rate')) is int and
              a.get('source_id') == self.source['id'] and a.get('render_origin_sample') == self.origin and
              type(a.get('render_origin_sample')) is int and a.get('performers') == len(self.players) and
              type(a.get('performers')) is int, 'Arrangement source, roster or clock differs from capture')
        notes = a.get('notes')
        _need(type(notes) is list and 0 < len(notes) <= 4000, 'Expected bounded assigned acoustic notes')
        original = a.get('source')
        effective = a.get('refinement_source', original)
        for lineage in (original, effective):
            _need(type(lineage) is dict and lineage.get('source_id') == self.source['id'] and
                  lineage.get('sample_rate') == RATE and type(lineage.get('notes')) is list,
                  'Arrangement must retain its actual source-event ledger')
        effective_ids = {note['id'] for note in effective['notes'] if type(note) is dict and _text(note.get('id'))}
        ids = set()
        for note in notes:
            _need(type(note) is dict and all(key in note for key in COMMAND_KEYS), 'Incomplete assigned acoustic command')
            _need(_text(note['id']) and note['id'] not in ids and note['id'] in effective_ids,
                  'Assigned command is missing its source-event identity')
            ids.add(note['id'])
            _need(_integer(note['start'], self.origin, self.origin + self.frames - RATE - 1) and
                  _integer(note['end'], note['start'] + 1, self.origin + self.frames - RATE) and
                  _integer(note['pitch'], 0, 127) and _integer(note['velocity'], 1, 127) and
                  _integer(note['player'], 1, len(self.players)) and _integer(note['string'], 0, 5) and
                  _integer(note['fret'], 0, 36) and note['role'] in ('melody', 'bass', 'harmony'),
                  'Invalid assigned acoustic command')
        _need({note['player'] for note in notes} == set(self.players), 'A requested performer has no assigned source events')
        _need(max(note['end'] for note in notes) - self.origin + RATE == self.frames,
              'Capture endpoint differs from the existing master render endpoint')
        self.instances = tuple(sorted({(note['player'], note['string']) for note in notes}))
        self.commands = [{key: note[key] for key in COMMAND_KEYS} for note in notes]

    def _check(self):
        if self._cancel():
            raise InterruptedError('Acoustic companion capture cancelled')

    def _folder_ok(self):
        _plain(self.folder, directory=True)
        info = self.folder.stat()
        _need((info.st_dev, info.st_ino) == self._folder_identity, 'Owned capture folder changed')

    def _new(self, name):
        self._folder_ok()
        path = self.folder / name
        stream = path.open('xb')
        info = os.fstat(stream.fileno())
        self._owned[path] = (info.st_dev, info.st_ino)
        return stream

    def _write_json(self, name, value, maximum):
        raw = _canonical(value, maximum)
        with self._new(name) as stream:
            stream.write(raw)
        return hashlib.sha256(raw).hexdigest()

    def _hold_metadata(self, stack, name, expected_sha256, maximum):
        path = _plain(self.folder / name)
        before = path.stat()
        _need((before.st_dev, before.st_ino) == self._owned[path] and 0 < before.st_size <= maximum,
              'Saved capture metadata identity or size differs')
        stream = stack.enter_context(_protected_reader(path))
        current = os.fstat(stream.fileno())
        _need((current.st_dev, current.st_ino, current.st_size) == (before.st_dev, before.st_ino, before.st_size),
              'Saved capture metadata changed while opening')
        raw = stream.read(maximum + 1)
        _need(len(raw) == before.st_size and hashlib.sha256(raw).hexdigest() == expected_sha256,
              'Saved capture metadata bytes differ')

    def _close_streams(self):
        for stream in self._streams.values():
            try:
                stream.close()
            except OSError:
                pass

    def _remove_owned(self, *, keep_diagnostic):
        if self.folder is None:
            return
        for path, identity in list(self._owned.items()):
            if keep_diagnostic and path.name == 'diagnostic.json':
                continue
            try:
                self._folder_ok()
                _plain(path)
                info = path.stat()
                if (info.st_dev, info.st_ino) == identity:
                    path.unlink()
                    del self._owned[path]
            except (OSError, ValueError):
                pass  # Changed/foreign paths are never removed.

    def _refuse(self, error):
        self.state = 'cancelled' if isinstance(error, InterruptedError) else 'refused'
        self._block = None
        self._close_streams()
        self._remove_owned(keep_diagnostic=True)
        self._diagnosis = {'version': VERSION, 'status': self.state, 'stage': self._stage,
                          'error_type': type(error).__name__, 'detail': str(error)[:512],
                          'captured_frames': self._covered,
                          'expected_frames': getattr(self, 'frames', None),
                          'standalone_action': 'Continue the existing preview unchanged; this companion is unavailable',
                          'musical_acceptance': False}
        if self.folder is not None and not any(path.name == 'diagnostic.json' for path in self._owned):
            try:
                self._write_json('diagnostic.json', self._diagnosis, MAX_DIAGNOSTIC_BYTES)
            except (OSError, ValueError):
                pass
        self._outcome = {'status': self.state, 'diagnostic': deepcopy(self._diagnosis), 'bundle': None}
        return False

    @property
    def outcome(self):
        return deepcopy(self._outcome or {'status': self.state, 'captured_frames': self._covered, 'bundle': None})

    def bind_render(self, arrangement, *, origin_sample, total_frames):
        """Refuse a different render input before observing any native buffers."""
        if self.state != 'capturing':
            return False
        try:
            self._check()
            actual = hashlib.sha256(_canonical(arrangement, MAX_ARRANGEMENT_BYTES)).hexdigest()
            _need(actual == self.arrangement_sha256 and origin_sample == self.origin and
                  total_frames == self.frames, 'Actual renderer arrangement or interval differs from capture')
            return True
        except Exception as error:
            return self._refuse(error)

    def begin_block(self, start, count):
        if self.state != 'capturing':
            return False
        try:
            self._check()
            _need(self._block is None and type(start) is int and start == self._covered and
                  _integer(count, 1, MAX_BLOCK) and start + count <= self.frames,
                  'Capture block must be contiguous, bounded and nonoverlapping')
            self._block = {'count': count, 'seen': [], 'values': {player: array.array('d', [0.0]) * (2 * count) for player in self.players}}
            return True
        except Exception as error:
            return self._refuse(error)

    def add_instance(self, player, string, left, right):
        if self.state != 'capturing':
            return False
        try:
            self._check()
            block = self._block
            _need(block is not None and len(block['seen']) < len(self.instances) and
                  type(player) is int and type(string) is int and
                  (player, string) == self.instances[len(block['seen'])], 'Native instance order or roster changed')
            count = block['count']
            _need(len(left) >= count and len(right) >= count, 'Incomplete native contribution block')
            values = block['values'][player]
            for index in range(count):
                for channel, source in enumerate((left, right)):
                    sample = source[index]
                    _need(type(sample) in (int, float) and math.isfinite(sample), 'Nonfinite or invalid native contribution')
                    values[2 * index + channel] += sample
                    _need(math.isfinite(values[2 * index + channel]), 'Acoustic performer accumulation overflow')
            block['seen'].append((player, string))
            return True
        except Exception as error:
            return self._refuse(error)

    def end_block(self, master_float32):
        if self.state != 'capturing':
            return False
        try:
            self._check()
            block = self._block
            _need(block is not None and tuple(block['seen']) == self.instances, 'Missing native instance contribution')
            _need(type(master_float32) is array.array and master_float32.typecode == 'f' and
                  len(master_float32) == 2 * block['count'] and all(math.isfinite(value) for value in master_float32),
                  'Pass the existing finite float32 master block before any byteswap')
            master = array.array('f', master_float32)
            self._master_peak = max(self._master_peak, max(map(abs, master)))
            if sys.byteorder != 'little':
                master.byteswap()
            self._master_hash.update(master.tobytes())
            for player in self.players:
                values = block['values'][player]
                if sys.byteorder != 'little':
                    values.byteswap()
                raw = values.tobytes()
                self._streams[player].write(raw)
                self._float_hashes[player].update(raw)
            self._covered += block['count']
            self._block = None
            return True
        except Exception as error:
            return self._refuse(error)

    def _copy_combined(self, source_path):
        source_path = _plain(source_path)
        expected = 44 + self.frames * 4
        _need(source_path.stat().st_size == expected, 'Existing combined WAV geometry differs')
        digest = hashlib.sha256()
        with _protected_reader(source_path) as source, self._new('combined.wav') as target:
            _need(os.fstat(source.fileno()).st_size == expected, 'Existing combined WAV changed while opening')
            remaining = expected
            while remaining:
                self._check()
                data = source.read(min(65536, remaining))
                _need(data, 'Truncated existing combined WAV')
                target.write(data)
                digest.update(data)
                remaining -= len(data)
            _need(source.read(1) == b'', 'Existing combined WAV grew while copying')
        return {'name': 'combined.wav', 'bytes': expected, 'sha256': digest.hexdigest(),
                'format': 'pcm_s16le', 'channels': 2, 'sample_rate': RATE, 'frames': self.frames}

    def _encode(self, player, global_gain):
        import struct
        frames = self.frames
        name = f'performer-{player}.wav'
        header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + frames * 4, b'WAVE', b'fmt ',
                             16, 1, 2, RATE, RATE * 4, 4, 16, b'data', frames * 4)
        pcm_hash, float_hash = hashlib.sha256(), hashlib.sha256()
        path = self.folder / f'performer-{player}.f64'
        _plain(path)
        _need(path.stat().st_size == frames * 16, 'Captured performer geometry differs')
        with _protected_reader(path) as source, self._new(name) as target:
            target.write(header)
            pcm_hash.update(header)
            remaining = frames * 16
            while remaining:
                self._check()
                data = source.read(min(65536, remaining))
                _need(data and len(data) % 16 == 0, 'Truncated captured performer float stream')
                remaining -= len(data)
                float_hash.update(data)
                values = array.array('d')
                values.frombytes(data)
                if sys.byteorder != 'little':
                    values.byteswap()
                pcm = array.array('h')
                for value in values:
                    scaled = value * global_gain
                    _need(math.isfinite(scaled) and abs(scaled) < 1, 'A performer exceeds PCM16 headroom at the existing global gain')
                    sample = round(scaled * 32768)
                    _need(-32768 < sample < 32767, 'A performer reaches PCM16 full scale at the existing global gain')
                    pcm.append(sample)
                if sys.byteorder != 'little':
                    pcm.byteswap()
                raw = pcm.tobytes()
                target.write(raw)
                pcm_hash.update(raw)
            _need(source.read(1) == b'', 'Captured performer float stream grew')
            _need(float_hash.hexdigest() == self._float_hashes[player].hexdigest(), 'Captured performer float bytes changed')
        return ({'name': name, 'bytes': 44 + frames * 4, 'sha256': pcm_hash.hexdigest(),
                 'format': 'pcm_s16le', 'channels': 2, 'sample_rate': RATE, 'frames': frames}, float_hash.hexdigest())

    def finish(self, combined_path, *, raw_peak, global_gain, renderer_metrics, forbidden_audio_sha256=()):
        """Admit a companion or retain a refusal; never write combined_path.

        raw_peak/global_gain must be the ORIGINAL master measurement and scalar,
        not separately measured performer levels or a gain_db round trip.
        """
        if self.state != 'capturing':
            return self.outcome
        try:
            self._stage = 'finalization'
            self._check()
            _need(self._block is None and self._covered == self.frames, 'Capture is incomplete')
            self._close_streams()
            _need(type(raw_peak) in (int, float) and math.isfinite(raw_peak) and raw_peak > 1e-8 and
                  raw_peak == self._master_peak, 'Existing master raw peak differs from observed float32 blocks')
            _need(type(global_gain) in (int, float) and math.isfinite(global_gain) and global_gain > 0,
                  'Existing common scalar gain is required')
            metrics = json.loads(_canonical(renderer_metrics, 128 * 1024))
            _need(type(metrics) is dict and metrics.get('renderer_version') == RENDER_VERSION and
                  metrics.get('sample_rate') == RATE and metrics.get('frames') == self.frames and
                  metrics.get('source_origin_sample') == self.origin and metrics.get('raw_peak') == raw_peak and
                  metrics.get('level_policy') in ('peak', 'legacy_fixed') and
                  metrics.get('processing_mode') == 'freewheeling' and metrics.get('instances') == len(self.instances),
                  'Existing renderer metrics differ from the captured render')
            expected_gain = 10 ** (-3 / 20) / raw_peak if metrics['level_policy'] == 'peak' else .25
            _need(global_gain == expected_gain, 'Shared gain differs from the unchanged master level policy')
            _need(metrics.get('gain_db') == 20 * math.log10(global_gain), 'Existing master gain receipt differs')
            qualities = metrics.get('quality_by_instance')
            _need(type(qualities) is list and len(qualities) == len(self.instances) and all(type(q) is dict and
                  set(q) == {'sample', 'oscillator'} and all(_integer(v, 0, 10) for v in q.values()) for q in qualities),
                  'Missing actual per-instance renderer quality receipt')
            files, float_hashes = {}, {}
            self._stage = 'quantization'
            for player in self.players:
                files[player], float_hashes[player] = self._encode(player, global_gain)
            combined = self._copy_combined(combined_path)
            clock = {'sample_rate': RATE, 'origin_sample': self.origin, 'body_frames': self.frames - RATE,
                     'tail_frames': RATE, 'frames': self.frames}
            receipt = {'version': VERSION, 'source': self.source, 'revision': self.revision,
                       'arrangement_sha256': self.arrangement_sha256, 'clock': clock,
                       'commands': self.commands, 'commands_sha256': hashlib.sha256(_canonical(self.commands, MAX_RECEIPT_BYTES)).hexdigest(),
                       'instances': [list(instance) for instance in self.instances],
                       'performer_float': {str(p): {'format': 'stereo_f64le', 'bytes': self.frames * 16, 'sha256': float_hashes[p]} for p in self.players},
                       'master_float': {'format': 'stereo_f32le', 'bytes': self.frames * 8, 'sha256': self._master_hash.hexdigest()},
                       'raw_peak': raw_peak, 'global_gain': global_gain, 'renderer_metrics': metrics,
                       'renderer_module_sha256': hashlib.sha256(Path(__file__).with_name('render.py').read_bytes()).hexdigest(),
                       'capture_module_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       'combined': combined, 'native_execution_independently_verified': False,
                       'source_or_vocal_content_exclusion_verified': False, 'musical_acceptance': False}
            receipt_sha = self._write_json('renderer-receipt.json', receipt, MAX_RECEIPT_BYTES)
            arrangement_id = {'revision': self.revision, 'sha256': self.arrangement_sha256,
                              'performers': len(self.players), 'label': 'Acoustic Solo' if len(self.players) == 1 else 'Acoustic Duo'}
            renderer_id = {'id': 'sfizz-meta-acoustic', 'version': RENDER_VERSION, 'receipt_sha256': receipt_sha,
                           'source_kind': 'synthesized', 'content_exclusion': 'declared_not_verified'}
            binding = {'source': deepcopy(self.source), 'clock': clock, 'arrangement': arrangement_id, 'renderer': renderer_id}
            stems = []
            for player in self.players:
                commands = [command for command in self.commands if command['player'] == player]
                reference = f'arrangement:{self.arrangement_sha256}:player:{player}'
                stems.append({'id': f'performer-{player}', 'instrument': 'acoustic',
                              'role': ' and '.join(sorted({command['role'] for command in commands})),
                              'gain': 1, 'default_audible': True, 'file': files[player],
                              'provenance': {'source_ids': [reference], 'assignment': 'existing assigned source events; see bound command ledger',
                                             'adaptations': ['Existing adaptations retained in the bound arrangement; capture changes none'],
                                             'newly_composed': False, 'source_kind': 'source_event_lineage'}})
            manifest = {'version': 'stem-bundle-v1', **binding, 'stems': stems,
                        'combined': {'file': combined, 'mix_point': 'linear_pre_master',
                                     'active_stem_ids': [stem['id'] for stem in stems],
                                     'quantization': 'sum_retained_pcm16_then_nearest_ties_even', 'tolerance_lsb': 1}}
            manifest_sha = self._write_json('manifest.json', manifest, MAX_MANIFEST_BYTES)
            self._stage = 'verification'
            with ExitStack() as metadata:
                self._hold_metadata(metadata, 'manifest.json', manifest_sha, MAX_MANIFEST_BYTES)
                self._hold_metadata(metadata, 'renderer-receipt.json', receipt_sha, MAX_RECEIPT_BYTES)
                verified = verify_bundle(self.folder, manifest, expected_binding=binding,
                                         expected_manifest_sha256=manifest_sha, forbidden_audio_sha256=forbidden_audio_sha256,
                                         cancel=self._cancel, max_frames=self.frames,
                                         max_total_audio_bytes=self.forecast['bundle_audio_bytes'])
                verification_sha = self._write_json('verification.json', verified['verification'], MAX_VERIFICATION_BYTES)
                self._hold_metadata(metadata, 'verification.json', verification_sha, MAX_VERIFICATION_BYTES)
                self._check()
                self.state = 'admitted'
                self._outcome = {'status': 'admitted', 'diagnostic': None,
                                 'bundle': {'folder': str(self.folder), 'manifest': verified['manifest'],
                                            'verification': verified['verification'], 'renderer_receipt': receipt,
                                            'forecast': deepcopy(self.forecast)}}
                return self.outcome
        except Exception as error:
            self._refuse(error)
            return self.outcome

    def cleanup(self, *, keep_diagnostic=True):
        """Only exclusively created, still-identical files; never a recursive delete.

        Call after the caller's verified publication, or after a refusal. A
        diagnostic remains by default. Unrelated or replaced paths are preserved.
        """
        self._close_streams()
        self._remove_owned(keep_diagnostic=keep_diagnostic)
        if self.folder is not None:
            try:
                self._folder_ok()
                self.folder.rmdir()  # Fails safely if any foreign/retained file exists.
            except (OSError, ValueError):
                pass
        if self.state == 'capturing':
            self.state = 'closed'
