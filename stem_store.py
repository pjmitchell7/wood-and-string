"""Private immutable acoustic companions, separate from Library media rows.

The caller owns worker.lock, resource admission and the later job-result commit.
Only canonical UUID capture folders and full-manifest content addresses are
accepted. Publication copies observed evidence; it does not establish musical,
native-execution, source-exclusion or acoustic-alignment acceptance.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from audio.acoustic_stem_capture import VERSION as CAPTURE_VERSION
from audio.acoustic_stem_capture import MAX_RECEIPT_BYTES
from audio.full_decode import _protected_reader
from audio.render import RENDER_VERSION
import stem_evidence

VERSION = 'acoustic-stem-store-v1'
STAGE_VERSION = 'acoustic-stem-store-stage-v1'
MAX_FRAMES = 31 * 44100
MAX_AUDIO_BYTES = 3 * (44 + MAX_FRAMES * 4)
MAX_VERIFICATION_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = stem_evidence.MAX_MANIFEST_BYTES
MAX_BUNDLE_BYTES = MAX_AUDIO_BYTES + MAX_MANIFEST_BYTES + MAX_RECEIPT_BYTES + MAX_VERIFICATION_BYTES
CHUNK = 65536
SHA = re.compile(r'[0-9a-f]{64}')
FILE_NAMES = frozenset(('performer-1.wav', 'performer-2.wav', 'combined.wav',
                        'manifest.json', 'renderer-receipt.json', 'verification.json'))
RECEIPT_KEYS = frozenset(('version', 'source', 'revision', 'arrangement_sha256', 'clock',
    'commands', 'commands_sha256', 'instances', 'performer_float', 'master_float', 'raw_peak',
    'global_gain', 'renderer_metrics', 'renderer_module_sha256', 'capture_module_sha256',
    'combined', 'native_execution_independently_verified',
    'source_or_vocal_content_exclusion_verified', 'musical_acceptance'))
COMMAND_KEYS = frozenset(('id', 'start', 'end', 'pitch', 'velocity', 'player', 'string', 'fret', 'role'))


class CompanionStoreError(ValueError):
    """Bounded optional-companion refusal; the normal preview remains usable."""
    def __init__(self, phase, error, pending_stage=None):
        super().__init__(f'Acoustic companion store refused during {phase} ({type(error).__name__})')
        self.phase = phase
        self.cancelled = isinstance(error, InterruptedError)
        # A bounded, path-free ownership ledger permits a later controlled retry.
        self.pending_stage = deepcopy(pending_stage)


class CompanionStoreCancelled(CompanionStoreError, InterruptedError):
    pass


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, 'Repeated companion metadata key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Nonfinite companion metadata')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    _need(_canonical(value) == raw, 'Companion metadata is not exact canonical JSON')
    return value


def _uuid(value):
    _need(type(value) is str and str(uuid.UUID(value)) == value, 'Invalid capture identity')
    return value


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
              'Linked companion path')
        if item == path:
            _need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
                  'Nonregular companion member')
            _need(directory or info.st_nlink == 1, 'Hard-linked companion member')
    return path


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _directory(path):
    info = _plain(path, directory=True).stat()
    return info.st_dev, info.st_ino


def _check(cancel):
    if cancel():
        raise InterruptedError('Acoustic companion publication cancelled')


def _hash(stream, maximum):
    stream.seek(0)
    digest, size = hashlib.sha256(), 0
    while data := stream.read(min(CHUNK, maximum + 1 - size)):
        size += len(data)
        _need(size <= maximum, 'Companion file exceeds byte bound')
        digest.update(data)
    stream.seek(0)
    return digest.hexdigest(), size


def _held(stack, path, maximum):
    path = _plain(path)
    before = path.stat()
    _need(0 < before.st_size <= maximum, 'Companion file exceeds byte bound')
    stream = stack.enter_context(_protected_reader(path))
    _need(_signature(os.fstat(stream.fileno())) == _signature(before), 'Companion identity changed while opening')
    return stream


def _metadata(stack, path, maximum, expected_sha256=None):
    stream = _held(stack, path, maximum)
    raw = stream.read(maximum + 1)
    _need(len(raw) == os.fstat(stream.fileno()).st_size and
          (expected_sha256 is None or hashlib.sha256(raw).hexdigest() == expected_sha256),
          'Companion metadata hash differs')
    return _json(raw), raw


def _members(manifest):
    return [stem['file'] for stem in manifest['stems']] + [manifest['combined']['file']]


def _audio_policy(value):
    _need(_int(value, 48, MAX_AUDIO_BYTES), 'Invalid companion audio byte policy')


def _acoustic(manifest, receipt):
    """Constrain the generic backend to the current complete acoustic capture."""
    clock, arrangement, renderer = (manifest[key] for key in ('clock', 'arrangement', 'renderer'))
    count = arrangement['performers']
    _need(count in (1, 2) and 44100 < clock['frames'] <= MAX_FRAMES and clock['tail_frames'] == 44100 and
          arrangement['label'] == ('Acoustic Solo' if count == 1 else 'Acoustic Duo') and
          renderer['id'] == 'sfizz-meta-acoustic' and renderer['version'] == RENDER_VERSION,
          'Unsupported acoustic companion backend or interval')
    stems = manifest['stems']
    _need(len(stems) == count and [stem['id'] for stem in stems] == [f'performer-{n}' for n in range(1, count + 1)] and
          all(stem['instrument'] == 'acoustic' and stem['gain'] == 1 and stem['default_audible'] is True and
              stem['file']['name'] == stem['id'] + '.wav' for stem in stems) and
          manifest['combined']['file']['name'] == 'combined.wav' and
          manifest['combined']['active_stem_ids'] == [stem['id'] for stem in stems],
          'Unsupported acoustic performer/default-mix schema')
    _need(type(receipt) is dict and set(receipt) == RECEIPT_KEYS and receipt['version'] == CAPTURE_VERSION and
          _canonical(receipt['source']) == _canonical(manifest['source']) and receipt['revision'] == arrangement['revision'] and
          receipt['arrangement_sha256'] == arrangement['sha256'] and _canonical(receipt['clock']) == _canonical(clock) and
          _canonical(receipt['combined']) == _canonical(manifest['combined']['file']) and
          all(receipt[key] is False for key in ('native_execution_independently_verified',
              'source_or_vocal_content_exclusion_verified', 'musical_acceptance')) and
          all(_sha(receipt[key]) for key in ('renderer_module_sha256', 'capture_module_sha256', 'commands_sha256')),
          'Acoustic renderer receipt binding or schema differs')
    commands = receipt['commands']
    _need(type(commands) is list and 0 < len(commands) <= 4000 and
          hashlib.sha256(_canonical(commands)).hexdigest() == receipt['commands_sha256'], 'Invalid captured command ledger')
    ids = set()
    for command in commands:
        _need(type(command) is dict and set(command) == COMMAND_KEYS and
              stem_evidence._text(command['id']) and command['id'] not in ids and
              _int(command['start'], clock['origin_sample'], clock['origin_sample'] + clock['body_frames'] - 1) and
              _int(command['end'], command['start'] + 1, clock['origin_sample'] + clock['body_frames']) and
              _int(command['pitch'], 0, 127) and _int(command['velocity'], 1, 127) and
              _int(command['player'], 1, count) and _int(command['string'], 0, 5) and _int(command['fret'], 0, 36) and
              command['role'] in ('melody', 'bass', 'harmony'), 'Invalid captured acoustic command')
        ids.add(command['id'])
    instances = [list(value) for value in sorted({(c['player'], c['string']) for c in commands})]
    _need({c['player'] for c in commands} == set(range(1, count + 1)) and
          max(c['end'] for c in commands) == clock['origin_sample'] + clock['body_frames'] and
          _canonical(receipt['instances']) == _canonical(instances), 'Captured roster, native instance order or endpoint differs')
    floats = receipt['performer_float']
    _need(type(floats) is dict and set(floats) == {str(n) for n in range(1, count + 1)}, 'Invalid captured float roster')
    for value, format_name, size in [(floats[str(n)], 'stereo_f64le', clock['frames'] * 16)
                                   for n in range(1, count + 1)] + [(receipt['master_float'], 'stereo_f32le', clock['frames'] * 8)]:
        _need(type(value) is dict and set(value) == {'format', 'bytes', 'sha256'} and value['format'] == format_name and
              type(value['bytes']) is int and value['bytes'] == size and _sha(value['sha256']), 'Invalid captured float declaration')
    metrics = receipt['renderer_metrics']
    _need(type(metrics) is dict and len(_canonical(metrics)) <= 128 * 1024 and metrics.get('renderer_version') == RENDER_VERSION and
          type(metrics.get('sample_rate')) is int and metrics['sample_rate'] == 44100 and
          type(metrics.get('frames')) is int and metrics['frames'] == clock['frames'] and
          type(metrics.get('source_origin_sample')) is int and metrics['source_origin_sample'] == clock['origin_sample'] and
          type(metrics.get('instances')) is int and metrics['instances'] == len(instances) and
          metrics.get('processing_mode') == 'freewheeling' and metrics.get('level_policy') in ('peak', 'legacy_fixed'),
          'Invalid captured renderer metrics')
    peak, gain = receipt['raw_peak'], receipt['global_gain']
    _need(type(peak) in (int, float) and math.isfinite(peak) and peak > 1e-8 and
          type(metrics.get('raw_peak')) in (int, float) and metrics['raw_peak'] == peak and
          type(gain) in (int, float) and math.isfinite(gain) and gain > 0 and
          gain == (10 ** (-3 / 20) / peak if metrics['level_policy'] == 'peak' else .25) and
          type(metrics.get('gain_db')) in (int, float) and metrics['gain_db'] == 20 * math.log10(gain),
          'Invalid captured common scalar')
    qualities = metrics.get('quality_by_instance')
    _need(type(qualities) is list and len(qualities) == len(instances) and all(type(q) is dict and
          set(q) == {'sample', 'oscillator'} and all(_int(v, 0, 10) for v in q.values()) for q in qualities),
          'Invalid captured per-instance quality declaration')
    for number, stem in enumerate(stems, 1):
        _need(stem['role'] == ' and '.join(sorted({c['role'] for c in commands if c['player'] == number})) and
              _canonical(stem['provenance']) == _canonical({'source_ids': [f"arrangement:{arrangement['sha256']}:player:{number}"],
                'assignment': 'existing assigned source events; see bound command ledger',
                'adaptations': ['Existing adaptations retained in the bound arrangement; capture changes none'],
                'newly_composed': False, 'source_kind': 'source_event_lineage'}), 'Acoustic provenance differs from capture schema')


def _reference(manifest, manifest_raw, receipt_raw, verification_raw):
    def member(raw):
        return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    identity = hashlib.sha256(manifest_raw).hexdigest()
    return {'version': VERSION, 'bundle_id': identity, 'manifest': member(manifest_raw),
            'renderer_receipt': member(receipt_raw), 'verification': member(verification_raw),
            'audio': [{key: row[key] for key in ('name', 'bytes', 'sha256')} for row in _members(manifest)]}


def validate_reference(reference):
    """Return a detached bounded path-free reference, without trusting its media."""
    _need(type(reference) is dict and set(reference) == {'version', 'bundle_id', 'manifest', 'renderer_receipt', 'verification', 'audio'} and
          reference['version'] == VERSION and _sha(reference['bundle_id']), 'Invalid acoustic companion reference')
    for key, maximum in (('manifest', MAX_MANIFEST_BYTES), ('renderer_receipt', MAX_RECEIPT_BYTES),
                         ('verification', MAX_VERIFICATION_BYTES)):
        row = reference[key]
        _need(type(row) is dict and set(row) == {'sha256', 'bytes'} and _sha(row['sha256']) and
              _int(row['bytes'], 1, maximum), 'Invalid acoustic companion metadata reference')
    _need(reference['bundle_id'] == reference['manifest']['sha256'], 'Companion address differs from full manifest SHA')
    audio = reference['audio']
    _need(type(audio) is list and len(audio) in (2, 3), 'Invalid acoustic companion audio reference')
    expected = [f'performer-{n}.wav' for n in range(1, len(audio))] + ['combined.wav']
    for row, name in zip(audio, expected):
        _need(type(row) is dict and set(row) == {'name', 'bytes', 'sha256'} and row['name'] == name and
              _sha(row['sha256']) and _int(row['bytes'], 48, 44 + MAX_FRAMES * 4), 'Invalid acoustic audio member reference')
    return deepcopy(reference)


def _copy_stream(source, target, expected_bytes, cancel):
    source.seek(0)
    remaining = expected_bytes
    while remaining:
        _check(cancel)
        data = source.read(min(CHUNK, remaining))
        _need(data, 'Incomplete acoustic companion copy')
        _need(target.write(data) == len(data), 'Incomplete acoustic companion write')
        remaining -= len(data)
    _need(source.read(1) == b'', 'Acoustic companion grew during copy')


class StemStore:
    def __init__(self, library):
        self.library = library
        self.root = Path(library.root).absolute() / 'stem-bundles'

    def capture_folder(self, capture_id):
        """The sole accepted capture location; this method creates nothing."""
        try:
            return Path(self.library.root).absolute() / 'staging' / (_uuid(capture_id) + '.stem-capture')
        except Exception as error:
            raise CompanionStoreError('capture identity', error) from error

    def _authority(self):
        root = _plain(self.library.root, directory=True)
        _need(self.root == root / 'stem-bundles' and Path(self.library.stage).absolute() == root / 'staging',
              'Unexpected Library staging authority')
        _plain(self.library.stage, directory=True)

    @contextmanager
    def _protect(self, folder, *, expected_binding, expected_manifest_sha256,
                 expected_renderer_receipt_sha256, max_total_audio_bytes, reference=None,
                 forbidden_audio_sha256=(), cancel=lambda: False):
        _check(cancel)
        _audio_policy(max_total_audio_bytes)
        _need(_sha(expected_manifest_sha256) and _sha(expected_renderer_receipt_sha256), 'Expected full companion hashes are required')
        identity = _directory(folder)
        with ExitStack() as stack:
            manifest, manifest_raw = _metadata(stack, folder / 'manifest.json', MAX_MANIFEST_BYTES, expected_manifest_sha256)
            manifest = stem_evidence.validate_manifest(manifest, max_frames=MAX_FRAMES,
                                                       max_total_audio_bytes=max_total_audio_bytes)
            receipt, receipt_raw = _metadata(stack, folder / 'renderer-receipt.json', MAX_RECEIPT_BYTES,
                                             expected_renderer_receipt_sha256)
            _need(manifest['renderer']['receipt_sha256'] == expected_renderer_receipt_sha256,
                  'Expected renderer receipt differs from manifest')
            _acoustic(manifest, receipt)
            audio_bytes = sum(row['bytes'] for row in _members(manifest))
            verified = stack.enter_context(stem_evidence.protect_bundle(folder, manifest,
                expected_binding=expected_binding, expected_manifest_sha256=expected_manifest_sha256,
                forbidden_audio_sha256=forbidden_audio_sha256, cancel=cancel,
                max_frames=MAX_FRAMES, max_total_audio_bytes=audio_bytes))
            # The verifier's exact admitted audio size makes this receipt stable
            # across distinct caller caps. Caller caps were enforced above.
            verification_raw = _canonical(verified['verification'])
            _need(len(verification_raw) <= MAX_VERIFICATION_BYTES, 'Verifier receipt exceeds bound')
            actual_reference = _reference(manifest, manifest_raw, receipt_raw, verification_raw)
            if reference is not None:
                _need(actual_reference == validate_reference(reference), 'Saved acoustic companion reference differs')
                _, saved_verification = _metadata(stack, folder / 'verification.json', MAX_VERIFICATION_BYTES,
                                                   reference['verification']['sha256'])
                _need(saved_verification == verification_raw, 'Saved verifier receipt differs from fresh verification')
                self._exact_members(folder, {row['name'] for row in _members(manifest)} |
                                    {'manifest.json', 'renderer-receipt.json', 'verification.json'})
            audio = {row['name']: _held(stack, folder / row['name'], row['bytes']) for row in _members(manifest)}
            _need(_directory(folder) == identity, 'Companion folder changed during verification')
            _check(cancel)
            yield {'reference': actual_reference, 'manifest': manifest, 'renderer_receipt': receipt,
                   'verification': verified['verification'], 'audio': audio,
                   'metadata': {'manifest.json': manifest_raw, 'renderer-receipt.json': receipt_raw,
                                'verification.json': verification_raw}}

    @staticmethod
    def _exact_members(folder, expected):
        # Never materialize an unbounded directory listing.
        found = set()
        with os.scandir(folder) as entries:
            for entry in entries:
                _need(len(found) < len(expected) and entry.name in expected, 'Unexpected immutable companion member')
                found.add(entry.name)
        _need(found == expected, 'Incomplete immutable companion bundle')

    @contextmanager
    def protect(self, reference, *, expected_binding, expected_manifest_sha256,
                expected_renderer_receipt_sha256, max_total_audio_bytes,
                forbidden_audio_sha256=(), cancel=lambda: False):
        """Reverify and hold exact WAV readers for a future checked media route.

        Readers are valid only within this context. No URL or path is trusted
        from the reference. The caller separately authorizes job/player access.
        """
        with ExitStack() as stack:
            try:
                self._authority()
                reference = validate_reference(reference)
                _need(reference['bundle_id'] == expected_manifest_sha256 and
                      reference['renderer_receipt']['sha256'] == expected_renderer_receipt_sha256,
                      'Trusted companion reference identity differs')
                _plain(self.root, directory=True)
                checked = stack.enter_context(self._protect(self.root / reference['bundle_id'], reference=reference,
                    expected_binding=expected_binding, expected_manifest_sha256=expected_manifest_sha256,
                    expected_renderer_receipt_sha256=expected_renderer_receipt_sha256,
                    max_total_audio_bytes=max_total_audio_bytes, forbidden_audio_sha256=forbidden_audio_sha256,
                    cancel=cancel))
            except Exception as error:
                failure = CompanionStoreCancelled if isinstance(error, InterruptedError) else CompanionStoreError
                raise failure('read verification', error) from error
            yield {key: checked[key] for key in ('reference', 'manifest', 'renderer_receipt', 'verification', 'audio')}

    def verify(self, reference, **context):
        """Return detached verified metadata; holds close before return."""
        with self.protect(reference, **context) as checked:
            return deepcopy({key: checked[key] for key in ('reference', 'manifest', 'renderer_receipt', 'verification')})

    def _write(self, stage, record, name, source, size, cancel):
        _need(name in FILE_NAMES and _directory(stage) == tuple(record['directory']), 'Owned companion stage changed')
        path = stage / name
        with path.open('x+b') as target:
            created = os.fstat(target.fileno())
            try:
                _copy_stream(source, target, size, cancel)
                target.flush()
                os.fsync(target.fileno())
            finally:
                # Read the file WE created through our held descriptor, even
                # after a partial write. A replacement path is never adopted.
                try:
                    target.flush()
                    digest, actual_size = _hash(target, MAX_BUNDLE_BYTES)
                    info = os.fstat(target.fileno())
                    if (info.st_dev, info.st_ino) == (created.st_dev, created.st_ino):
                        record['files'].append({'name': name, 'signature': list(_signature(info)),
                                                'sha256': digest, 'bytes': actual_size})
                except (OSError, ValueError):
                    pass  # Unknown bytes remain as a controlled orphan stage.

    def cleanup_stage(self, record):
        """Retry ONLY a supplied owned-stage ledger; never scan or recurse.

        Changed, linked, foreign and unknown paths remain. A missing/changed
        stage is a no-op. Caller retains its lock while reconciling this record.
        """
        try:
            _need(type(record) is dict and set(record) == {'version', 'id', 'directory', 'files'} and
                  record['version'] == STAGE_VERSION, 'Invalid companion stage ledger')
            _uuid(record['id'])
            _need(type(record['directory']) is list and len(record['directory']) == 2 and
                  all(_int(v, 0, 2**128 - 1) for v in record['directory']) and
                  type(record['files']) is list and len(record['files']) <= len(FILE_NAMES), 'Invalid stage ownership bounds')
            names = set()
            for row in record['files']:
                _need(type(row) is dict and set(row) == {'name', 'signature', 'sha256', 'bytes'} and
                      row['name'] in FILE_NAMES and row['name'] not in names and _sha(row['sha256']) and
                      _int(row['bytes'], 0, MAX_BUNDLE_BYTES) and type(row['signature']) is list and len(row['signature']) == 5 and
                      all(_int(v, 0, 2**128 - 1) for v in row['signature']) and
                      row['signature'][2] == row['bytes'] and row['signature'][4] == 1, 'Invalid owned stage member')
                names.add(row['name'])
            self._authority()
            _plain(self.root, directory=True)
        except Exception as error:
            raise CompanionStoreError('stage cleanup admission', error) from error
        stage = self.root / ('.publish-' + record['id'])
        removed = 0
        for row in record['files']:
            try:
                _need(_directory(stage) == tuple(record['directory']), 'Stage ownership changed')
                path = _plain(stage / row['name'])
                _need(_signature(path.stat()) == tuple(row['signature']), 'Stage member changed')
                with _protected_reader(path) as stream:
                    _need(_signature(os.fstat(stream.fileno())) == tuple(row['signature']) and
                          _hash(stream, MAX_BUNDLE_BYTES) == (row['sha256'], row['bytes']), 'Stage bytes changed')
                _need(_directory(stage) == tuple(record['directory']) and
                      _signature(_plain(path).stat()) == tuple(row['signature']), 'Stage member changed before cleanup')
                path.unlink()
                removed += row['bytes']
            except (OSError, ValueError):
                pass
        try:
            _need(_directory(stage) == tuple(record['directory']), 'Stage ownership changed')
            stage.rmdir()  # Safely refuses any unknown/retained entry.
        except (OSError, ValueError):
            pass
        return {'bytes': removed}

    def publish(self, capture_id, *, expected_binding, expected_manifest_sha256,
                expected_renderer_receipt_sha256, max_total_audio_bytes, max_publish_bytes,
                forbidden_audio_sha256=(), cancel=lambda: False):
        """Copy, freshly verify, then atomically publish a complete directory.

        A complete unreferenced blob is preserved after publication, including
        cancellation/failure before a later caller-owned DB commit. Never append
        this reference to an already accepted immutable job result.
        """
        record = None
        phase = 'admission'
        try:
            _need(os.name == 'nt', 'Companion publication requires Windows no-replace rename')
            _need(callable(cancel) and _int(max_publish_bytes, 1, 2**53 - 1), 'Explicit companion byte admission is required')
            _check(cancel)
            self._authority()
            folder = self.capture_folder(capture_id)
            phase = 'source verification'
            with self._protect(folder, expected_binding=expected_binding,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_renderer_receipt_sha256=expected_renderer_receipt_sha256,
                max_total_audio_bytes=max_total_audio_bytes, forbidden_audio_sha256=forbidden_audio_sha256,
                cancel=cancel) as source:
                reference = source['reference']
                total = sum(row['bytes'] for row in reference['audio']) + sum(len(raw) for raw in source['metadata'].values())
                _need(total <= max_publish_bytes and total <= MAX_BUNDLE_BYTES, 'Companion publication exceeds caller byte policy')
                target = self.root / reference['bundle_id']
                occupied = target.exists() or target.is_symlink()
                phase = 'disk admission'
                self.library.space(0 if occupied else total)  # Library enforces its 8 GiB reserve.
                if occupied:
                    checked = self.verify(reference, expected_binding=expected_binding,
                        expected_manifest_sha256=expected_manifest_sha256,
                        expected_renderer_receipt_sha256=expected_renderer_receipt_sha256,
                        max_total_audio_bytes=max_total_audio_bytes,
                        forbidden_audio_sha256=forbidden_audio_sha256, cancel=cancel)
                    return checked['reference']
                _plain(self.library.root, directory=True)
                self.root.mkdir(exist_ok=True)
                _plain(self.root, directory=True)
                stage_id = str(uuid.uuid4())
                stage = self.root / ('.publish-' + stage_id)
                stage.mkdir()  # Never adopt an occupied stage or its files.
                record = {'version': STAGE_VERSION, 'id': stage_id, 'directory': list(_directory(stage)), 'files': []}
                phase = 'copy'
                for row in reference['audio']:
                    self._write(stage, record, row['name'], source['audio'][row['name']], row['bytes'], cancel)
                import io
                for name, raw in source['metadata'].items():
                    self._write(stage, record, name, io.BytesIO(raw), len(raw), cancel)
                phase = 'staged verification'
                with self._protect(stage, reference=reference, expected_binding=expected_binding,
                    expected_manifest_sha256=expected_manifest_sha256,
                    expected_renderer_receipt_sha256=expected_renderer_receipt_sha256,
                    max_total_audio_bytes=max_total_audio_bytes, forbidden_audio_sha256=forbidden_audio_sha256,
                    cancel=cancel):
                    pass
                _need(_directory(stage) == tuple(record['directory']), 'Stage changed before publication')
                for row in record['files']:
                    _need(_signature(_plain(stage / row['name']).stat()) == tuple(row['signature']), 'Stage member changed before publication')
                _check(cancel)
                phase = 'atomic publication'
                try:
                    os.rename(stage, target)  # Windows refuses any occupied directory.
                except FileExistsError:
                    pass  # Reverify the concurrently published exact identity.
                phase = 'final verification'
                return self.verify(reference, expected_binding=expected_binding,
                    expected_manifest_sha256=expected_manifest_sha256,
                    expected_renderer_receipt_sha256=expected_renderer_receipt_sha256,
                    max_total_audio_bytes=max_total_audio_bytes, forbidden_audio_sha256=forbidden_audio_sha256,
                    cancel=cancel)['reference']
        except CompanionStoreError:
            raise
        except Exception as error:
            failure = CompanionStoreCancelled if isinstance(error, InterruptedError) else CompanionStoreError
            raise failure(phase, error, record) from error
        finally:
            if record is not None:
                try:
                    self.cleanup_stage(record)
                except CompanionStoreError:
                    pass
