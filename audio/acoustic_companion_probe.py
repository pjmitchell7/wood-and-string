"""One fixed production-worker round trip inside the existing managed bootstrap.

No preparation, launch, acquisition or admission authority lives here. The
operator owns the full held current closure, seeded Library, consumed marker,
resource accounting and original-process cleanup proof. The recorded candidate
selection is a scripted role assertion, not a new human musical acceptance.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

APP = Path(__file__).resolve().parents[1]
ROOT = APP/'work/acoustic-parts-integration-001/native-worker-001'
VERSION = 'acoustic-companion-probe-v1'
PROFILE = {'wall_seconds': 60, 'memory_bytes': 512*1024**2,
           'host_admission_bytes': 3*1024**3, 'host_floor_bytes': 2*1024**3,
           'all_new_material_bytes': 14*1024**2, 'cpu_only': True}
SOURCE_FILES = ('audio/acoustic_companion_probe.py', 'worker.py', 'library.py',
    'instrumental_preview.py', 'analysis_evidence.py', 'acoustic_companion.py',
    'stem_store.py', 'stem_evidence.py', 'render_evidence.py', 'audio/render.py',
    'audio/acoustic_stem_capture.py', 'audio/performance.py',
    'audio/preview_instruments.py', 'audio/full_decode.py',
    'audio/native_trial_process.py', 'audio/managed_process.py')
SOURCE_ID = 'a45c5152-8bda-49e1-b32a-eeb818f9998e'
PARENT_ID = 'ce34d348-7b18-516f-8857-f6a5d3935544'
OLD_JOB_ID = '0a45b185-b5ab-527e-b581-3e1a07a072a9'
OLD_MASTER_ID = '49f45030-18c1-440c-9229-7df1aa6e4eec'
JOB_ID = '9b94566f-310c-5735-90b3-218c7c9dd6c9'
SOURCE_PIN = {'bytes': 1058444, 'sha256': '2d5393870461a4edc7e5470d8c5a34664253097318df026fc45668cfc06c8c9a'}
EVIDENCE_PIN = {'bytes': 755970, 'sha256': 'f24677d838f5d8dc9b999a021d040357a1ffe5ff01dc41d4a13ee6ff5e16f776'}
MASTER_PIN = {'bytes': 446780, 'sha256': 'a59c141172fd13b8472cf17e302700096eb2aec36fba5f91d3a78cd08b016b82'}
PARENT_RESULT_SHA = 'db56e4aa5aca7c5a37ae9c29ea4b2cca90c3084f80da5f84188647e8617e30cb'
ARRANGEMENT_SHA = '7e1e6208986af8b20256c769a7ba3394d8282d52233b93489bbca73f9b8c3d0b'
NOTE_IDS = tuple('published_default-'+str(n) for n in range(39, 44))
RATE, ORIGIN, END, BODY, FRAMES = 44100, 0, 264600, 67584, 111684
FREEZE_CAP, CLOSURE_CAP, ROW_CAP, RESULT_CAP = 64*1024, 512*1024, 4*1024**2, 16*1024
SHA = re.compile('[0-9a-f]{64}')
SCOPE = ('One existing selected-candidate Solo production worker.run, acoustic '
         'capture, immutable publication and cleanup round trip. No fresh '
         'recognition, new human role acceptance, musical/listening acceptance, '
         'Duo worker, server activation or CLI parent-watcher proof.')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def identity(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _pin(value, maximum):
    _need(type(value) is dict and set(value) == {'bytes', 'sha256'}
          and type(value['bytes']) is int and 0 < value['bytes'] <= maximum
          and type(value['sha256']) is str and SHA.fullmatch(value['sha256']),
          'Exact bounded byte identity required')
    return value


def _row_pin(value):
    raw = canonical(value)
    _need(len(raw) <= ROW_CAP, 'Seeded row exceeds metadata bound')
    return identity(raw)


def _empty(path):
    from audio.drum_acquisition import plain
    plain(path, True)
    with os.scandir(path) as entries:
        _need(next(entries, None) is None, 'Occupied owned output or staging; no retry')


def _members(path, names, *, directories=False):
    """Count only the tiny seeded directories; reject the first extra member."""
    from audio.drum_acquisition import plain
    plain(path, True)
    expected, observed = set(names), set()
    with os.scandir(path) as entries:
        for entry in entries:
            _need(entry.name in expected and entry.name not in observed,
                  'Unexpected seeded Library member')
            plain(path/entry.name, directories)
            observed.add(entry.name)
    _need(observed == expected, 'Incomplete seeded Library')


def _hold_blob(stack, path, pin, check):
    from audio.drum_acquisition import plain, hold_parents
    from audio.full_decode import _protected_reader
    plain(path); hold_parents(stack, path)
    before = path.stat()
    stream = stack.enter_context(_protected_reader(path))
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
    _need(signature(os.fstat(stream.fileno())) == signature(before)
          and before.st_size == pin['bytes'], 'Seeded blob geometry differs')
    digest, remaining = hashlib.sha256(), pin['bytes']
    while remaining:
        check()
        raw = stream.read(min(65536, remaining))
        _need(raw, 'Incomplete seeded blob read')
        digest.update(raw); remaining -= len(raw)
    _need(not stream.read(1) and digest.hexdigest() == pin['sha256'],
          'Seeded blob identity differs')
    def unchanged():
        check()
        _need(signature(path.stat()) == signature(before)
              and signature(os.fstat(stream.fileno())) == signature(before),
              'Held seeded blob changed')
    return unchanged


def _save(path, value, maximum=RESULT_CAP):
    raw = canonical(value)
    _need(len(raw) <= maximum, 'Worker adapter receipt exceeds bound')
    with path.open('x+b') as stream:
        _need(stream.write(raw) == len(raw), 'Incomplete worker adapter receipt')
        stream.flush(); os.fsync(stream.fileno()); stream.seek(0)
        _need(stream.read(maximum+1) == raw, 'Worker adapter receipt differs')
    return identity(raw)


def _job(library, job_id):
    with library.connect() as db:
        sizes = db.execute('SELECT length(request),length(result) FROM jobs WHERE id=?', (job_id,)).fetchone()
    _need(sizes is not None and type(sizes[0]) is int and 0 < sizes[0] <= ROW_CAP
          and (sizes[1] is None or type(sizes[1]) is int and 0 < sizes[1] <= ROW_CAP),
          'Saved job exceeds bounded metadata policy')
    return library.job(job_id)


def _rows(library, job_id):
    """No unbounded items/cache load: exactly two assets and three jobs."""
    with library.connect() as db:
        assets = [row['id'] for row in db.execute('SELECT id FROM assets LIMIT 3')]
        jobs = [row['id'] for row in db.execute('SELECT id FROM jobs LIMIT 4')]
    _need(set(assets) == {SOURCE_ID, OLD_MASTER_ID} and len(assets) == 2
          and set(jobs) == {PARENT_ID, OLD_JOB_ID, job_id} and len(jobs) == 3,
          'Exact isolated seeded catalogue required')
    return dict(source_row=library.get(SOURCE_ID), parent_row=_job(library, PARENT_ID),
                old_job_row=_job(library, OLD_JOB_ID), old_master_row=library.get(OLD_MASTER_ID))


def _prepare(library, frozen):
    from acoustic_companion import KEY, VERSION as COMPANION_VERSION
    from instrumental_preview import prepare_arrangement, arrange_review, canonical as review_canonical
    rows = _rows(library, frozen['job_id'])
    _need({key: _row_pin(value) for key, value in rows.items()} == frozen['seed'],
          'Frozen seeded rows differ')
    source, parent, old, master = (rows[k] for k in
        ('source_row', 'parent_row', 'old_job_row', 'old_master_row'))
    _need(source['source_id'] == SOURCE_ID and source['kind'] == 'original'
          and {'bytes': source['size'], 'sha256': source['sha256']} == SOURCE_PIN
          and master['source_id'] == SOURCE_ID and master['kind'] == 'instrumental-selected-acoustic'
          and master['mime'] == 'audio/wav'
          and {'bytes': master['size'], 'sha256': master['sha256']} == MASTER_PIN,
          'Fixed original or prior master row differs')
    _need(parent['state'] == old['state'] == 'complete' and parent['error'] is None
          and old['error'] is None and identity(review_canonical(parent['result']))['sha256'] == PARENT_RESULT_SHA
          and old['result']['asset'] == master
          and identity(canonical(old['result']['arrangement']))['sha256'] == ARRANGEMENT_SHA
          and 'acoustic_companion' not in old['result'], 'Retained parent or old job differs')
    evidence = parent['result']['evidence']
    _need(evidence == {**EVIDENCE_PIN, 'format': 'numpy-npz-v1'}, 'Fixed activation reference differs')
    prepared = prepare_arrangement(library, {'parent_job_id': PARENT_ID,
        'interpretation': 'published', 'note_ids': list(NOTE_IDS),
        'instrument': 'acoustic', 'accept_as_lead': True})
    spec, arrangement = prepared['spec'], arrange_review(prepared)
    derived = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(spec, sort_keys=True)))
    _need(derived == frozen['job_id'] == JOB_ID and canonical(spec) == canonical(frozen['spec'])
          and spec.get(KEY) == COMPANION_VERSION
          and identity(canonical(arrangement))['sha256'] == frozen['arrangement_sha256'] == ARRANGEMENT_SHA,
          'Fresh current preparation, request or arrangement differs')
    _need(prepared['bundle']['origin'] == ORIGIN and prepared['bundle']['end'] == END
          and arrangement['sample_rate'] == RATE and arrangement['performers'] == 1
          and arrangement['render_origin_sample'] == ORIGIN and len(arrangement['notes']) == len(NOTE_IDS)
          and {n['id'] for n in arrangement['notes']} == set(NOTE_IDS)
          and max(n['end'] for n in arrangement['notes'])-ORIGIN == BODY
          and BODY+RATE == FRAMES == frozen['frames'], 'Fixed candidate clock or Solo roster differs')
    legacy = deepcopy(spec); legacy.pop(KEY)
    _need(canonical(old['request']) == canonical(legacy), 'Prior request differs beyond companion version')
    job = _job(library, derived)
    _need(job['state'] == 'queued' and type(job['progress']) in (int, float) and job['progress'] == 0
          and job['result'] is None and job['error'] is None
          and canonical(job['request']) == canonical(spec), 'Exact fresh queued job required')
    return rows, job, arrangement


def _execute(library, frozen, check, emit, held):
    import worker
    from acoustic_companion import protect_saved
    from audio.drum_acquisition import plain
    _members(library.media, (SOURCE_PIN['sha256'], MASTER_PIN['sha256']))
    _members(library.root/'analysis-evidence', (EVIDENCE_PIN['sha256'],))
    _empty(library.stage)
    _need(not (library.root/'stem-bundles').exists() and not (library.root/'stem-bundles').is_symlink(),
          'Occupied companion publication; no retry')
    guards = [_hold_blob(held, library.media/pin['sha256'], pin, check)
              for pin in (SOURCE_PIN, MASTER_PIN)]
    guards.append(_hold_blob(held, library.root/'analysis-evidence'/EVIDENCE_PIN['sha256'], EVIDENCE_PIN, check))
    rows, queued, arrangement = _prepare(library, frozen)
    check(True)
    _save(ROOT/'outputs/started.json', {'version': VERSION, 'job_id': queued['id'],
        'request_sha256': identity(canonical(queued['request']))['sha256'],
        'fresh_recognition_run': False, 'musical_acceptance': False}, 1024)
    emit({'phase': 'production-worker', 'job_id': queued['id']})
    # This is the unmodified production function. The managed bootstrap owns
    # parent-loss termination; this callback adds only sampled wall/RAM checks.
    worker.run(library.root, queued['id'], parent_gone=check)
    check()
    job = _job(library, queued['id'])
    _need(job['state'] == 'complete' and job['progress'] == 1 and job['error'] is None
          and canonical(job['request']) == canonical(queued['request']),
          'Production worker did not commit the exact fresh complete job')
    _need(job['result']['asset'] == rows['old_master_row']
          and canonical(job['result']['arrangement']) == canonical(arrangement),
          'Actual production master or arrangement differs')
    checked = held.enter_context(protect_saved(library, job, cancel=check))
    manifest, reference = checked['manifest'], checked['reference']
    _need(manifest['clock']['frames'] == FRAMES and len(manifest['stems']) == 1
          and manifest['arrangement']['revision'] == queued['id']
          and manifest['combined']['file']['sha256'] == MASTER_PIN['sha256']
          and manifest['combined']['file']['bytes'] == MASTER_PIN['bytes'],
          'Committed companion differs from exact production master')
    verification = deepcopy(checked['verification'])
    reference = deepcopy(reference)
    for guard in guards: guard()
    _need(_rows(library, queued['id']) == rows, 'Original, parent, old job or master row changed')
    _empty(library.stage)
    _need(not (library.root/(queued['id']+'.cleanup-attention.json')).exists(),
          'Production helper cleanup needs attention')
    _members(library.root/'stem-bundles', (reference['bundle_id'],), directories=True)
    _need(plain(library.db).stat().st_size <= 1024**2, 'Final database exceeds allowance')
    emit({'phase': 'verified-production-worker', 'job_id': queued['id']})
    return {'version': VERSION, 'job_id': queued['id'], 'state': job['state'],
        'request': queued['request'], 'job': _row_pin(job), 'result': _row_pin(job['result']),
        'source': SOURCE_PIN, 'activations': EVIDENCE_PIN, 'master': MASTER_PIN,
        'arrangement_sha256': ARRANGEMENT_SHA, 'reference': reference,
        'verification': verification, 'frames': FRAMES, 'sample_rate': RATE,
        'unchanged_seed_rows': frozen['seed'], 'held_seed_blobs_unchanged': True,
        'owned_helper_staging_removed': True, 'fresh_recognition_run': False,
        'musical_acceptance': False, 'selection_authority': 'Repeated recorded scripted candidate selection and lead assertion',
        'scope': SCOPE}


def run(request, emit):
    """Fixed owned entry; the operator must already have admitted this attempt."""
    from audio.native_trial_process import _assert_owned_worker
    _assert_owned_worker()
    _need(type(request) is dict and set(request) == {'freeze_sha256', 'job_id'}
          and type(request['freeze_sha256']) is str and SHA.fullmatch(request['freeze_sha256'])
          and request['job_id'] == JOB_ID, 'Exact fixed worker request required')
    from audio.local_arranger_trial import available_ram
    from audio.drum_acquisition import plain, read, strict, hold_directory
    from library import Library
    began, last, minimum, observations = time.monotonic(), 0., 2**63, 0
    guard_failed = False
    def check(admission=False):
        nonlocal last, minimum, observations, guard_failed
        _need(not guard_failed, 'Host memory admission or sampled floor differs')
        now = time.monotonic()
        if now-began >= PROFILE['wall_seconds']:
            raise TimeoutError('Production worker adapter deadline')
        if admission or now-last >= .2:
            guard_failed = True
            available = available_ram(); last = time.monotonic(); observations += 1
            threshold = PROFILE['host_admission_bytes' if admission else 'host_floor_bytes']
            if type(available) is not int or available < threshold:
                raise ValueError('Host memory admission or sampled floor differs')
            guard_failed = False
            minimum = min(minimum, available)
        if time.monotonic()-began >= PROFILE['wall_seconds']:
            raise TimeoutError('Host observation crossed production worker deadline')
        return False
    with ExitStack() as held:
        plain(ROOT, True); held.enter_context(hold_directory(ROOT))
        raw = read(held, ROOT/'frozen.json', FREEZE_CAP)
        frozen = strict(raw, FREEZE_CAP)
        _need(identity(raw)['sha256'] == request['freeze_sha256'] and canonical(frozen) == raw
              and type(frozen) is dict and set(frozen) == {'version', 'profile', 'job_id', 'spec',
                  'arrangement_sha256', 'frames', 'seed', 'sources', 'closure'}
              and frozen['version'] == VERSION and canonical(frozen['profile']) == canonical(PROFILE)
              and frozen['job_id'] == request['job_id'] and type(frozen['frames']) is int
              and frozen['frames'] == FRAMES, 'Exact frozen worker, profile or interval differs')
        _need(type(frozen['sources']) is dict and set(frozen['sources']) == set(SOURCE_FILES),
              'Exact current source pins required')
        for name in SOURCE_FILES:
            read(held, APP/name, 128*1024, _pin(frozen['sources'][name], 128*1024))
        read(held, ROOT/'closure.json', CLOSURE_CAP, _pin(frozen['closure'], CLOSURE_CAP))
        _need(type(frozen['seed']) is dict and set(frozen['seed']) ==
              {'source_row', 'parent_row', 'old_job_row', 'old_master_row'}, 'Exact seeded row pins required')
        for pin in frozen['seed'].values(): _pin(pin, ROW_CAP)
        marker = strict(read(held, ROOT/'consumed.json', 1024), 1024)
        _need(marker == {'version': VERSION, **request, 'retry': False} and marker['retry'] is False,
              'Exact one-attempt consumed marker required')
        _empty(ROOT/'outputs'); held.enter_context(hold_directory(ROOT/'outputs'))
        library_root = plain(ROOT/'library', True)
        db = plain(library_root/'library.sqlite')
        _need(db.stat().st_size <= 1024**2, 'Seeded database exceeds allowance')
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = db.with_name(db.name+suffix)
            _need(not sidecar.exists() and not sidecar.is_symlink(), 'Unexpected seeded database sidecar')
        for name in ('media', 'staging', 'analysis-evidence'):
            held.enter_context(hold_directory(plain(library_root/name, True)))
        lock = library_root/'worker.lock'
        if lock.exists():
            _need(plain(lock).stat().st_size == 1, 'Invalid seeded worker lock')
        else:
            _need(not lock.is_symlink(), 'Linked seeded worker lock')
        library = Library(library_root)
        with library.exclusive('worker.lock'):
            try:
                result = _execute(library, frozen, check, emit, held)
                check()
                result.update(freeze_sha256=request['freeze_sha256'], wall_seconds=time.monotonic()-began,
                    minimum_available_ram=minimum, host_observations=observations,
                    resource_scope='Host memory sampled between bounded work chunks; parent owns disk accounting and managed memory/kill receipts')
                _members(ROOT/'outputs', ('started.json',))
                pin = _save(ROOT/'outputs/result.json', result)
                return {'state': 'verified_production_worker_round_trip_only', 'job_id': JOB_ID,
                        'file': 'result.json', **pin}
            except BaseException as primary:
                # Never delete worker output, complete blobs or unrelated files.
                # A refusal before started.json is preparation failure only.
                if (ROOT/'outputs/started.json').exists():
                    try:
                        _save(ROOT/'outputs/failure.json', {'version': VERSION, **request,
                            'error_type': type(primary).__name__, 'wall_seconds': time.monotonic()-began,
                            'scope': 'Failed attempt retained; parent original-handle receipt remains authoritative'}, 2048)
                    except BaseException:
                        pass
                raise
