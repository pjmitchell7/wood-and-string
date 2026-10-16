"""Optional acoustic preview parts. The existing master remains independent."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import math
import os
import re
import stat
import uuid

from audio.acoustic_stem_capture import AcousticStemCapture, COMMAND_KEYS, forecast_capture, MAX_FRAMES
from audio.render import RENDER_VERSION
from audio.preview_instruments import validate_saved_selection
from audio.full_decode import _protected_reader
from library import MAX_IMPORT
from stem_store import StemStore, CompanionStoreError, validate_reference

VERSION = 'acoustic-companion-v1'
KEY = 'acoustic_companion_version'
KINDS = ('mixed', 'refine', 'instrumental-arrange')
RATE = 44100


def _need(condition, message='Acoustic companion context is invalid.'):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                     allow_nan=False).encode('utf-8')
    _need(len(raw) <= 12*1024**2)
    return raw


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(value):
    _need(type(value) is str and str(uuid.UUID(value)) == value)
    return value


def request_fields(selection, *, frozen=None):
    """New acoustic work opts in; replay preserves historical absence exactly."""
    _need(type(selection) is dict, 'Invalid acoustic companion selection.')
    instrument = selection.get('instrument', 'acoustic')
    _need(instrument in ('acoustic', 'electric', 'jazz-bass'), 'Invalid acoustic companion instrument.')
    if KEY in selection:
        _need(instrument == 'acoustic' and type(selection[KEY]) is str and selection[KEY] == VERSION,
              'Unsupported acoustic companion version or instrument.')
    if frozen is not None:
        _need(type(frozen) is dict, 'Invalid frozen acoustic companion request.')
        if KEY not in frozen:
            return {}
        _need(instrument == 'acoustic' and frozen.get('instrument', 'acoustic') == 'acoustic'
              and type(frozen[KEY]) is str and frozen[KEY] == VERSION,
              'Unsupported acoustic companion version or instrument.')
    return {KEY: VERSION} if instrument == 'acoustic' else {}


def enabled(spec):
    """Validate the saved opt-in before any analysis, render or file creation."""
    _need(type(spec) is dict, 'Invalid acoustic companion request.')
    try:
        selection = validate_saved_selection(spec)
    except (KeyError, TypeError, AttributeError):
        raise ValueError('Invalid acoustic companion request.') from None
    fields = request_fields(selection, frozen=spec)
    if not fields:
        return False
    _need(spec.get('kind') in KINDS and spec.get('renderer_version') == RENDER_VERSION,
          'Unsupported acoustic companion request or renderer.')
    _uuid(spec.get('asset_id'))
    _need(type(spec.get('sha256')) is str and len(spec['sha256']) == 64
          and all(c in '0123456789abcdef' for c in spec['sha256']))
    start, end = spec.get('start_seconds'), spec.get('end_seconds')
    _need(type(start) in (int, float) and type(end) in (int, float)
          and math.isfinite(start) and math.isfinite(end) and 0 <= start < end and end-start <= 30)
    return True


def _geometry(spec, source, arrangement):
    _need(type(source) is dict and source.get('kind') == 'original'
          and source.get('id') == spec['asset_id'] and source.get('sha256') == spec['sha256'])
    _need(type(arrangement) is dict)
    _canonical(arrangement)
    origin = round(spec['start_seconds']*RATE)
    boundary = round(spec['end_seconds']*RATE)
    players = spec['performers']
    _need(type(arrangement.get('sample_rate')) is int and arrangement['sample_rate'] == RATE
          and arrangement.get('instrument', 'acoustic') == 'acoustic'
          and arrangement.get('source_id') == source.get('source_id')
          and type(arrangement.get('performers')) is int and arrangement['performers'] == players
          and type(arrangement.get('render_origin_sample')) is int and arrangement['render_origin_sample'] == origin)
    notes = arrangement.get('notes')
    _need(type(notes) is list and 0 < len(notes) <= 4000)
    commands = [{key: note[key] for key in COMMAND_KEYS} for note in notes]
    _need(len({n['id'] for n in commands}) == len(commands)
          and all(type(n['start']) is int and type(n['end']) is int
                  and origin <= n['start'] < n['end'] <= boundary for n in commands)
          and {n['player'] for n in commands} == set(range(1, players+1)))
    for lineage in (arrangement.get('source'), arrangement.get('refinement_source', arrangement.get('source'))):
        _need(type(lineage) is dict and lineage.get('source_id') == source['source_id']
              and lineage.get('sample_rate') == RATE and type(lineage.get('notes')) is list)
    effective = arrangement.get('refinement_source', arrangement['source'])
    _need({n['id'] for n in commands} <= {n['id'] for n in effective['notes']})
    frames = max(n['end'] for n in commands)-origin+RATE
    _need(RATE < frames <= MAX_FRAMES)
    return dict(source={'id': source['source_id'], 'sha256': source['sha256']},
                arrangement_sha256=_sha(arrangement), commands=commands,
                clock=dict(sample_rate=RATE, origin_sample=origin, body_frames=frames-RATE,
                           tail_frames=RATE, frames=frames), performers=players)


def _binding(context, revision, receipt_sha256):
    count = context['performers']
    return dict(source=deepcopy(context['source']), clock=deepcopy(context['clock']),
                arrangement=dict(revision=revision, sha256=context['arrangement_sha256'], performers=count,
                                 label='Acoustic Solo' if count == 1 else 'Acoustic Duo'),
                renderer=dict(id='sfizz-meta-acoustic', version=RENDER_VERSION, receipt_sha256=receipt_sha256,
                              source_kind='synthesized', content_exclusion='declared_not_verified'))


def _refused(stage, reason, *, error=None, capture=None):
    diagnostic = {'stage': stage, 'reason': reason}
    def error_name(value):
        return value if type(value) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value) else None
    if error is not None:
        diagnostic['error_type'] = error_name(type(error).__name__) or 'Exception'
    try:
        outcome = capture.outcome if capture is not None else None
        detail = outcome.get('diagnostic') if type(outcome) is dict else None
    except Exception:
        detail = None
    if type(detail) is dict:
        name = error_name(detail.get('error_type'))
        if name:
            diagnostic['capture_error_type'] = name
            diagnostic.setdefault('error_type', name)
        if detail.get('stage') in ('admission', 'capture', 'finalization', 'quantization', 'verification'):
            diagnostic['capture_stage'] = detail['stage']
        expected = detail.get('expected_frames')
        captured = detail.get('captured_frames')
        if type(expected) is int and RATE < expected <= MAX_FRAMES:
            diagnostic['expected_frames'] = expected
        if (type(captured) is int and 0 <= captured <= MAX_FRAMES
                and ('expected_frames' not in diagnostic or captured <= diagnostic['expected_frames'])):
            diagnostic['captured_frames'] = captured
    return {'version': VERSION, 'status': 'unavailable', 'diagnostic': diagnostic}


class AcousticCompanion:
    """One optional observer; caller renders once and owns the job commit/lock."""
    def __init__(self, library, job_id, spec, source, arrangement, *, cancel=lambda: False):
        self.library, self.identity, self.cancel = library, _uuid(job_id), cancel
        self.capture = None
        self.record = None
        self.context = None
        self.forecast = None
        if not enabled(spec):
            return
        self.record = _refused('admission', 'Acoustic parts could not be prepared; the normal preview is available.')
        try:
            self._check()
            _need(source == library.get(spec['asset_id']))
            self.context = _geometry(spec, source, arrangement)
            self.forecast = forecast_capture(performers=self.context['performers'], frames=self.context['clock']['frames'])
            # Additional disk includes the eventual complete publication copy.
            library.space(self.forecast['additional_disk_bytes'] + self.forecast['original_render_allowance_bytes'])
            self._check()
            store = StemStore(library)
            self.capture = AcousticStemCapture(store.capture_folder(self.identity), source=self.context['source'],
                arrangement=arrangement, revision=self.identity, performers=list(range(1, self.context['performers']+1)),
                origin_sample=self.context['clock']['origin_sample'], total_frames=self.context['clock']['frames'],
                max_work_bytes=self.forecast['additional_disk_bytes'], cancel=cancel)
            self._check()
            if self.capture.outcome['status'] != 'capturing':
                self.record = _refused('capture', 'Acoustic parts were refused; the normal preview is available.', capture=self.capture)
                self.cleanup()
                self.capture = None
        except Exception as error:
            self.record = _refused('admission', 'Acoustic parts could not be prepared; the normal preview is available.',
                                   error=error, capture=self.capture)
            self.cleanup()
            self.capture = None
            self._check()  # Actual caller cancellation always remains a job cancellation.

    def _check(self):
        if self.cancel():
            raise InterruptedError('Acoustic preview cancelled.')

    def finish(self, master_path, metrics):
        self._check()
        if self.capture is None:
            return deepcopy(self.record)
        stage = 'finalization'
        try:
            _need(metrics.get('level_policy') == 'peak')
            peak = metrics['raw_peak']
            outcome = self.capture.finish(master_path, raw_peak=peak, global_gain=10**(-3/20)/peak,
                                           renderer_metrics=metrics)
            self._check()
            _need(outcome['status'] == 'admitted')
            manifest = outcome['bundle']['manifest']
            _need(manifest['arrangement']['sha256'] == self.context['arrangement_sha256'])
            stage = 'publication'
            receipt_sha = manifest['renderer']['receipt_sha256']
            reference = StemStore(self.library).publish(self.identity,
                expected_binding=_binding(self.context, self.identity, receipt_sha),
                expected_manifest_sha256=_sha(manifest), expected_renderer_receipt_sha256=receipt_sha,
                max_total_audio_bytes=self.forecast['bundle_audio_bytes'],
                max_publish_bytes=self.forecast['bundle_audio_bytes'] + self.forecast['metadata_bytes'],
                cancel=self.cancel)
            self._check()
            self.record = dict(version=VERSION, status='available', reference=reference,
                               arrangement_sha256=self.context['arrangement_sha256'])
        except Exception as error:
            self._check()
            self.record = _refused(stage, 'Acoustic parts could not be saved; the normal preview is available.',
                                   error=error, capture=self.capture)
        return deepcopy(self.record)

    def cleanup(self):
        if self.capture is not None:
            self.capture.cleanup(keep_diagnostic=False)
            return self.capture.folder is None or not self.capture.folder.exists()
        return True


def _verify_asset(stack, library, row, maximum, cancel):
    """Bound the actual hash read and retain protection through media use."""
    digest = row.get('sha256')
    size = row.get('size')
    _need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
          and type(size) is int and 0 < size <= maximum)
    path = library.media/digest
    for part in (path, *path.parents):
        info = part.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400)
    before = path.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == size)
    stream = stack.enter_context(_protected_reader(path))
    _need(signature(os.fstat(stream.fileno())) == signature(before))
    observed = hashlib.sha256()
    remaining = size
    while remaining:
        if cancel():raise InterruptedError('Acoustic preview cancelled.')
        raw = stream.read(min(65536, remaining))
        _need(raw)
        observed.update(raw); remaining -= len(raw)
    _need(stream.read(1) == b'' and observed.hexdigest() == digest
          and signature(os.fstat(stream.fileno())) == signature(before) and signature(path.stat()) == signature(before))


def _saved_context(library, job, stack, cancel):
    _need(type(job) is dict and job.get('state') == 'complete' and enabled(job['request']),
          'This preview has no available acoustic parts.')
    revision = _uuid(job['id'])
    result = job['result']
    companion = result['acoustic_companion']
    _need(type(companion) is dict and set(companion) == {'version', 'status', 'reference', 'arrangement_sha256'}
          and companion['version'] == VERSION and companion['status'] == 'available',
          'This preview has no available acoustic parts.')
    reference = validate_reference(companion['reference'])
    spec = job['request']
    source = library.get(spec['asset_id'])
    context = _geometry(spec, source, result['arrangement'])
    _verify_asset(stack, library, source, MAX_IMPORT, cancel)
    _need(companion['arrangement_sha256'] == context['arrangement_sha256'])
    saved = result['asset']
    master = library.get(saved['id'])
    _need(all(saved.get(k) == master.get(k) for k in ('id', 'source_id', 'sha256', 'size', 'mime', 'kind'))
          and master['source_id'] == source['source_id'] and master['kind'] != 'original'
          and master['mime'] == 'audio/wav' and type(master['size']) is int
          and master['size'] == 44+context['clock']['frames']*4,
          'The saved acoustic master failed verification.')
    _verify_asset(stack, library, master, 44+MAX_FRAMES*4, cancel)
    combined = reference['audio'][-1]
    _need(combined['sha256'] == master['sha256'] and combined['bytes'] == master['size']
          and master['size'] == 44+context['clock']['frames']*4)
    policy = dict(expected_binding=_binding(context, revision, reference['renderer_receipt']['sha256']),
                  expected_manifest_sha256=reference['manifest']['sha256'],
                  expected_renderer_receipt_sha256=reference['renderer_receipt']['sha256'],
                  max_total_audio_bytes=(context['performers']+1)*(44+context['clock']['frames']*4))
    return reference, context, policy, result['metrics']


@contextmanager
def protect_saved(library, job, *, cancel=lambda: False):
    """Verify committed context and hold authorized readers; never mutate a job."""
    with ExitStack() as stack:
        try:
            if cancel():
                raise InterruptedError('Acoustic preview cancelled.')
            reference, context, policy, metrics = _saved_context(library, job, stack, cancel)
            checked = stack.enter_context(StemStore(library).protect(reference, **policy, cancel=cancel))
            receipt = checked['renderer_receipt']
            _need(_canonical(receipt['commands']) == _canonical(context['commands'])
                  and receipt['commands_sha256'] == _sha(context['commands'])
                  and all(metrics.get(key) == value for key, value in receipt['renderer_metrics'].items()))
            if cancel():
                raise InterruptedError('Acoustic preview cancelled.')
        except CompanionStoreError:
            raise
        except InterruptedError:
            raise InterruptedError('Acoustic preview cancelled.') from None
        except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError, RecursionError):
            raise ValueError('Saved acoustic parts failed verification; the normal preview is unchanged.') from None
        yield checked


def verify_saved(library, job, *, cancel=lambda: False):
    """Detached metadata for the HTTP boundary; no reader escapes its lifetime."""
    with protect_saved(library, job, cancel=cancel) as checked:
        return deepcopy({key: checked[key] for key in ('reference', 'manifest', 'renderer_receipt', 'verification')})
