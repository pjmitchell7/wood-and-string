"""Temporary authored storage/fake-sfizz adapter controls; no actual providers."""
from contextlib import ExitStack
from copy import deepcopy
import ctypes
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from audio import acoustic_companion_probe as probe
from audio import render
from instrumental_preview import prepare_arrangement, arrange_review, canonical as review_canonical
from library import Library
from tests.test_acoustic_stem_render import fake_boundary
from tests import test_instrumental_preview as instrumental_fixture
import worker


class Adapter(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.space = self.stack.enter_context(patch.object(Library, 'space'))
        # The real protected path reader accepts only APP descendants. These
        # are fresh authored bytes, never copies of retained musical evidence.
        temporary = tempfile.TemporaryDirectory
        with patch('tests.test_instrumental_preview.tempfile.TemporaryDirectory',
                   side_effect=lambda: temporary(dir=probe.APP/'work', prefix='authored-worker-adapter-')):
            self.fixture = instrumental_fixture.InstrumentalPreview(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.library = self.fixture.library
        self.root = self.library.root.parent
        self.prepared = prepare_arrangement(self.library, self.fixture.request())
        self.spec = self.prepared['spec']; self.arrangement = arrange_review(self.prepared)
        self.identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(self.spec, sort_keys=True)))
        self.old_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'authored-old-worker-adapter'))
        self.legacy = deepcopy(self.spec); self.legacy.pop('acoustic_companion_version')
        output = self.library.stage/'authored-prior.wav'
        boundary, api = fake_boundary()
        with boundary:
            self.metrics = render.render(self.arrangement, output)
        self.assertFalse(api.states)
        self.master = self.library.publish(output, 'Authored old selected notes.wav', 'audio/wav',
            kind='instrumental-selected-acoustic', source_id=self.fixture.source['source_id'])
        self.old_result = dict(asset=self.master, arrangement=self.arrangement, metrics=self.metrics,
                               notice='Authored prior result; no native or model run', analysis=None)
        self.insert(self.old_id, self.legacy, 'complete', self.old_result)
        self.insert(self.identity, self.spec, 'queued', None)
        replacements = dict(ROOT=self.root, SOURCE_ID=self.fixture.source['id'], PARENT_ID=self.fixture.parent_id,
            OLD_JOB_ID=self.old_id, OLD_MASTER_ID=self.master['id'], JOB_ID=self.identity,
            SOURCE_PIN={'bytes':len(self.fixture.original), 'sha256':self.fixture.source['sha256']},
            EVIDENCE_PIN={'bytes':len(self.fixture.raw), 'sha256':self.fixture.reference['sha256']},
            MASTER_PIN={'bytes':self.master['size'], 'sha256':self.master['sha256']},
            PARENT_RESULT_SHA=probe.identity(review_canonical(self.fixture.result))['sha256'],
            ARRANGEMENT_SHA=probe.identity(probe.canonical(self.arrangement))['sha256'],
            NOTE_IDS=('common',), ORIGIN=self.fixture.origin, END=self.fixture.end,
            BODY=max(n['end'] for n in self.arrangement['notes'])-self.fixture.origin,
            FRAMES=self.metrics['frames'])
        for key, value in replacements.items(): self.stack.enter_context(patch.object(probe, key, value))
        self.gate = self.stack.enter_context(patch('audio.native_trial_process._assert_owned_worker'))
        self.ram = self.stack.enter_context(patch('audio.local_arranger_trial.available_ram', return_value=4*1024**3))
        fake_ctypes = SimpleNamespace(**{k:getattr(ctypes, k) for k in
            ('Structure', 'c_ulong', 'c_size_t', 'c_void_p', 'sizeof', 'POINTER', 'byref')},
            windll=SimpleNamespace(kernel32=SimpleNamespace(GetCurrentProcess=Mock(return_value=1)),
                                  psapi=SimpleNamespace(GetProcessMemoryInfo=Mock(return_value=False))))
        self.stack.enter_context(patch.object(worker, 'ctypes', fake_ctypes))
        self.nested = self.stack.enter_context(patch.object(worker, 'run_worker',
            side_effect=AssertionError('No inference, model, process or nested managed launch permitted')))
        self.root.joinpath('outputs').mkdir()
        self.closure_raw = b'{"authored_boundary":"not a real source/native closure"}'
        (self.root/'closure.json').write_bytes(self.closure_raw)
        self.frozen = dict(version=probe.VERSION, profile=deepcopy(probe.PROFILE), job_id=self.identity,
            spec=self.spec, arrangement_sha256=probe.ARRANGEMENT_SHA, frames=probe.FRAMES,
            seed={k:probe._row_pin(v) for k, v in probe._rows(self.library, self.identity).items()},
            sources={name:probe.identity((probe.APP/name).read_bytes()) for name in probe.SOURCE_FILES},
            closure=probe.identity(self.closure_raw))
        self.freeze()
        self.before = probe._rows(self.library, self.identity)
        self.blobs = {path:path.read_bytes() for path in
            (self.library.media/probe.SOURCE_PIN['sha256'], self.library.media/probe.MASTER_PIN['sha256'],
             self.library.root/'analysis-evidence'/probe.EVIDENCE_PIN['sha256'])}

    def insert(self, identity, spec, state, result):
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                (identity, state, 1 if state=='complete' else 0, json.dumps(spec),
                 json.dumps(result) if result is not None else None, None, 123.))

    def freeze(self):
        raw = probe.canonical(self.frozen)
        (self.root/'frozen.json').write_bytes(raw)
        self.request = {'freeze_sha256':probe.identity(raw)['sha256'], 'job_id':self.identity}
        (self.root/'consumed.json').write_bytes(probe.canonical({'version':probe.VERSION, **self.request, 'retry':False}))

    def execute(self, extra=None):
        boundary, api = fake_boundary()
        events = []
        with boundary, ExitStack() as stack:
            if extra is not None: stack.enter_context(extra)
            reply = probe.run(self.request, events.append)
        self.assertFalse(api.states)
        self.nested.assert_not_called()
        return reply, json.loads((self.root/'outputs/result.json').read_bytes()), events

    def unchanged(self):
        self.assertEqual(probe._rows(self.library, self.identity), self.before)
        for path, raw in self.blobs.items(): self.assertEqual(path.read_bytes(), raw)

    def test_actual_production_worker_round_trip_and_truthful_scope(self):
        actual = worker.run
        calls = []
        def wrapped(root, identity, parent_gone):
            calls.append((root, identity))
            # Verify the child really owns the standard production lock.
            with self.assertRaises(OSError):
                with Library(root).exclusive('worker.lock'): self.fail('lock was not held')
            return actual(root, identity, parent_gone)
        reply, result, events = self.execute(patch.object(worker, 'run', side_effect=wrapped))
        self.assertEqual(calls, [(self.library.root, self.identity)])
        self.assertEqual(reply['state'], 'verified_production_worker_round_trip_only')
        self.assertEqual(reply['job_id'], self.identity)
        self.assertEqual({k:reply[k] for k in ('bytes', 'sha256')}, probe.identity((self.root/'outputs/result.json').read_bytes()))
        self.assertEqual(self.library.job(self.identity)['state'], 'complete')
        self.assertEqual(result['master'], probe.MASTER_PIN)
        self.assertTrue(result['owned_helper_staging_removed'])
        self.assertFalse(result['fresh_recognition_run']); self.assertFalse(result['musical_acceptance'])
        self.assertIn('scripted', result['selection_authority'])
        self.assertEqual([e['phase'] for e in events], ['production-worker', 'verified-production-worker'])
        self.assertFalse(list(self.library.stage.iterdir()))
        self.assertLessEqual((self.root/'outputs/result.json').stat().st_size, probe.RESULT_CAP)
        retained = (self.root/'outputs/result.json').read_bytes()
        with patch.object(worker, 'run') as retry, self.assertRaisesRegex(ValueError, 'Occupied'):
            probe.run(self.request, lambda _:None)
        retry.assert_not_called()
        self.assertEqual((self.root/'outputs/result.json').read_bytes(), retained)
        self.unchanged()

    def test_fixed_request_rejects_paths_extra_keys_and_wrong_identity_before_provider(self):
        for request in ({**self.request, 'root':'../outside'}, {**self.request, 'job_id':self.old_id},
                        {**self.request, 'freeze_sha256':'F'*64}, {'job_id':self.identity}):
            with self.subTest(request=request), self.assertRaises(ValueError): probe.run(request, lambda _:None)
        self.ram.assert_not_called()
        self.assertFalse(list((self.root/'outputs').iterdir())); self.unchanged()

    def test_owned_gate_runs_before_any_request_or_provider(self):
        self.gate.side_effect = RuntimeError('authored unreleased gate')
        with self.assertRaisesRegex(RuntimeError, 'unreleased'): probe.run(self.request, lambda _:None)
        self.ram.assert_not_called(); self.unchanged()

    def test_changed_freeze_source_closure_or_marker_refuses_before_worker(self):
        scenarios = [('frozen.json', (self.root/'frozen.json').read_bytes()+b' '),
                     ('closure.json', self.closure_raw+b' '),
                     ('consumed.json', probe.canonical({'version':probe.VERSION, **self.request, 'retry':True}))]
        for name, bad in scenarios:
            original = (self.root/name).read_bytes(); (self.root/name).write_bytes(bad)
            with self.subTest(name=name), patch.object(worker, 'run') as run, self.assertRaises(ValueError):
                probe.run(self.request, lambda _:None)
            run.assert_not_called(); (self.root/name).write_bytes(original)
        self.frozen['sources']['worker.py']['sha256'] = 'a'*64; self.freeze()
        with patch.object(worker, 'run') as run, self.assertRaises(ValueError): probe.run(self.request, lambda _:None)
        run.assert_not_called(); self.ram.assert_not_called(); self.unchanged()

    def test_profile_seed_and_current_version_are_exact(self):
        original = deepcopy(self.frozen)
        for field, value in [('profile', {**probe.PROFILE, 'wall_seconds':60.0}),
                             ('seed', {**self.frozen['seed'], 'foreign':self.frozen['seed']['source_row']}),
                             ('seed', {**self.frozen['seed'], 'source_row':{'bytes':probe.ROW_CAP+1, 'sha256':'a'*64}}),
                             ('closure', {'bytes':probe.CLOSURE_CAP+1, 'sha256':'a'*64}),
                             ('sources', {**self.frozen['sources'], 'worker.py':{'bytes':128*1024+1, 'sha256':'a'*64}}),
                             ('frames', float(probe.FRAMES)), ('arrangement_sha256', 'a'*64),
                             ('spec', {**self.spec, 'acoustic_companion_version':'future'})]:
            self.frozen = deepcopy(original); self.frozen[field] = value; self.freeze()
            with self.subTest(field=field), patch.object(worker, 'run') as run, self.assertRaises(ValueError):
                probe.run(self.request, lambda _:None)
            run.assert_not_called()
        self.unchanged()

    def test_occupied_outputs_or_staging_preserve_foreign_sentinel_before_worker(self):
        for folder in (self.root/'outputs', self.library.stage):
            sentinel = folder/'unrelated.txt'; sentinel.write_bytes(b'preserve authored foreign file')
            with self.subTest(folder=folder.name), patch.object(worker, 'run') as run, self.assertRaises(ValueError):
                probe.run(self.request, lambda _:None)
            run.assert_not_called(); self.assertEqual(sentinel.read_bytes(), b'preserve authored foreign file')
            sentinel.unlink()
        self.unchanged()

    def test_nonfresh_job_and_extra_catalogue_row_refuse_before_worker(self):
        with self.library.connect() as db:
            db.execute('UPDATE jobs SET state=? WHERE id=?', ('complete', self.identity))
        with patch.object(worker, 'run') as run, self.assertRaises(ValueError): probe.run(self.request, lambda _:None)
        run.assert_not_called()
        with self.library.connect() as db:
            db.execute('UPDATE jobs SET state=? WHERE id=?', ('queued', self.identity))
        other = str(uuid.uuid4()); self.insert(other, {}, 'queued', None)
        with patch.object(worker, 'run') as run, self.assertRaises(ValueError): probe.run(self.request, lambda _:None)
        run.assert_not_called()
        with self.library.connect() as db: db.execute('DELETE FROM jobs WHERE id=?', (other,))
        self.unchanged()

    def test_wrong_source_evidence_or_old_master_bytes_refuse_without_deleting(self):
        for path, raw in self.blobs.items():
            path.write_bytes(raw[:-1]+bytes((raw[-1]^1,)))
            with self.subTest(path=path.name), patch.object(worker, 'run') as run, self.assertRaises(ValueError):
                probe.run(self.request, lambda _:None)
            run.assert_not_called(); self.assertTrue(path.exists()); path.write_bytes(raw)
        self.unchanged()

    def test_host_admission_refusal_precedes_started_and_worker(self):
        self.ram.return_value = 2*1024**3
        with patch.object(worker, 'run') as run, self.assertRaises(ValueError): probe.run(self.request, lambda _:None)
        run.assert_not_called(); self.assertFalse(list((self.root/'outputs').iterdir())); self.unchanged()

    def test_worker_failure_retains_bounded_receipt_and_seeded_objects(self):
        def refuse(root, identity, parent_gone):
            Library(root).update_job(identity, 'failed', error='Authored refusal')
        with self.assertRaisesRegex(ValueError, 'did not commit'):
            self.execute(patch.object(worker, 'run', side_effect=refuse))
        failure = json.loads((self.root/'outputs/failure.json').read_bytes())
        self.assertEqual(failure['error_type'], 'ValueError')
        self.assertNotIn(str(self.root), json.dumps(failure))
        self.assertFalse((self.root/'outputs/result.json').exists()); self.unchanged()

    def test_postcommit_companion_corruption_refuses_only_attempt_report_and_preserves_master(self):
        actual = worker.run
        def corrupt(root, identity, parent_gone):
            actual(root, identity, parent_gone)
            job = self.library.job(identity)
            ref = job['result']['acoustic_companion']['reference']
            path = self.library.root/'stem-bundles'/ref['bundle_id']/'manifest.json'
            raw = path.read_bytes(); path.write_bytes(raw+b' ')
        with self.assertRaises(ValueError): self.execute(patch.object(worker, 'run', side_effect=corrupt))
        self.assertEqual(self.library.job(self.identity)['state'], 'complete')
        self.assertTrue((self.root/'outputs/failure.json').is_file())
        self.unchanged()

    def test_helper_leftover_is_retained_and_reported_without_broad_cleanup(self):
        actual = worker.run
        def leftover(root, identity, parent_gone):
            actual(root, identity, parent_gone)
            (Library(root).stage/'foreign-sentinel').write_bytes(b'leave foreign staging alone')
        with self.assertRaisesRegex(ValueError, 'Occupied'):
            self.execute(patch.object(worker, 'run', side_effect=leftover))
        self.assertEqual((self.library.stage/'foreign-sentinel').read_bytes(), b'leave foreign staging alone')
        self.assertEqual(self.library.job(self.identity)['state'], 'complete'); self.unchanged()

    def test_seed_rows_mutated_after_run_are_refused_even_if_master_and_parts_verify(self):
        actual = worker.run
        def mutate(root, identity, parent_gone):
            actual(root, identity, parent_gone)
            with self.library.connect() as db:
                db.execute('UPDATE jobs SET progress=? WHERE id=?', (.5, self.old_id))
        with self.assertRaisesRegex(ValueError, 'row changed'):
            self.execute(patch.object(worker, 'run', side_effect=mutate))
        self.assertTrue((self.root/'outputs/failure.json').is_file())
        for path, raw in self.blobs.items(): self.assertEqual(path.read_bytes(), raw)

    def test_seed_bytes_are_held_during_fresh_prepare_and_bundle_through_final_event(self):
        from instrumental_preview import prepare_arrangement as prepare
        checks = []
        def held_prepare(*args, **kwargs):
            for path in self.blobs:
                with self.assertRaises(PermissionError):
                    with path.open('r+b'): self.fail('seeded bytes were writable during prepare')
            checks.append('prepare')
            return prepare(*args, **kwargs)
        def event(value):
            if value['phase'] == 'verified-production-worker':
                job = self.library.job(self.identity)
                root = self.library.root/'stem-bundles'/job['result']['acoustic_companion']['reference']['bundle_id']
                for path in (*self.blobs, root/'manifest.json', root/'combined.wav'):
                    with self.assertRaises(PermissionError):
                        with path.open('r+b'): self.fail('verified bytes were writable before result receipt')
                checks.append('verified')
        boundary, api = fake_boundary()
        with boundary, patch('instrumental_preview.prepare_arrangement', side_effect=held_prepare):
            probe.run(self.request, event)
        self.assertEqual(checks, ['prepare', 'prepare', 'verified'])
        self.assertFalse(api.states); self.unchanged()

    def test_sampled_host_floor_failure_stops_real_worker_and_cleans_owned_helper(self):
        clock = time.monotonic
        offset = [0.]
        def event(value):
            if value['phase'] == 'production-worker':
                self.ram.return_value = 1*1024**3
                offset[0] = 1.  # Make the next sampled guard due without waiting.
        boundary, api = fake_boundary()
        with boundary, patch.object(probe.time, 'monotonic', side_effect=lambda:clock()+offset[0]), \
             self.assertRaisesRegex(ValueError, 'sampled floor'):
            probe.run(self.request, event)
        self.assertFalse(api.states)
        self.assertEqual(self.library.job(self.identity)['state'], 'failed')
        self.assertFalse(list(self.library.stage.iterdir()))
        self.assertTrue((self.root/'outputs/failure.json').is_file())
        self.assertFalse((self.root/'outputs/result.json').exists()); self.unchanged()


if __name__ == '__main__': unittest.main()
