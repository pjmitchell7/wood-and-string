"""Authored PCM/fake-sfizz integration. No real resource/native providers."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import ctypes
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid
import wave

import acoustic_companion as parts
from audio import render
from audio.performance import Note, Performance, arrange
from library import Library
from stem_store import CompanionStoreError, StemStore
from tests.test_acoustic_stem_render import fake_boundary
import worker


def original_bytes():
    data = io.BytesIO()
    with wave.open(data, 'wb') as wav:
        wav.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0'*44)
    return data.getvalue()


def performance(source):
    return Performance(source['source_id'], 44100, [Note('lead-a', 0, 8, 64, 80, confidence=.8),
        Note('lead-b', 2, 11, 59, 80, confidence=.7), Note('backing-a', 0, 9, 40, 80, 'bass')])


class Versions(unittest.TestCase):
    def test_new_frozen_and_non_acoustic_fields(self):
        self.assertEqual(parts.request_fields({}), {parts.KEY:parts.VERSION})
        self.assertEqual(parts.request_fields({}, frozen={}), {})
        self.assertEqual(parts.request_fields({}, frozen={parts.KEY:parts.VERSION}), {parts.KEY:parts.VERSION})
        for instrument in ('electric', 'jazz-bass'):
            self.assertEqual(parts.request_fields({'instrument':instrument}), {})
            for argument in ({'selection':{'instrument':instrument, parts.KEY:parts.VERSION}},
                             {'selection':{'instrument':instrument}, 'frozen':{parts.KEY:parts.VERSION}},
                             {'selection':{}, 'frozen':{'instrument':instrument, parts.KEY:parts.VERSION}}):
                with self.assertRaises(ValueError): parts.request_fields(**argument)
        for value in (None, True, '', 'future'):
            with self.assertRaises(ValueError): parts.request_fields({parts.KEY:value})
            with self.assertRaises(ValueError): parts.request_fields({}, frozen={parts.KEY:value})
        # Diagnosis copies only known stages, safe type names and exact frame
        # integers. Raw messages and invalid counters never enter job JSON.
        for captured, expected in ((True, False), (-1, -1), (44112, 44111), (float('inf'), 10**30)):
            observer=SimpleNamespace(outcome={'diagnostic':dict(stage='C:/private/path', error_type='bad/path',
                captured_frames=captured, expected_frames=expected, detail='C:/private/error text')})
            diagnosis=parts._refused('capture','Unavailable',capture=observer)['diagnostic']
            self.assertNotIn('capture_stage',diagnosis);self.assertNotIn('capture_error_type',diagnosis)
            self.assertNotIn('captured_frames',diagnosis);self.assertNotIn('private',json.dumps(diagnosis))
            if type(expected) is not int or not 44100<expected<=parts.MAX_FRAMES:
                self.assertNotIn('expected_frames',diagnosis)


class Companion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.library = Library(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.space = self.stack.enter_context(patch.object(Library, 'space', return_value=None))
        source = self.library.stage/'authored-source'; source.write_bytes(original_bytes())
        self.source = self.library.publish(source, 'authored.wav', 'audio/wav')
        self.identity = str(uuid.uuid4())
        self.spec = dict(kind='mixed', asset_id=self.source['id'], sha256=self.source['sha256'],
                         start_seconds=0., end_seconds=11/44100, performers=2,
                         renderer_version=render.RENDER_VERSION, acoustic_companion_version=parts.VERSION)
        self.arrangement = arrange(performance(self.source), 2)
        self.output = self.library.stage/(self.identity+'.wav')

    def producer(self):
        return parts.AcousticCompanion(self.library, self.identity, self.spec, self.source, self.arrangement)

    def render(self, producer=None, *, cancel=lambda:False):
        boundary, api = fake_boundary()
        options = {'stem_capture':producer.capture} if producer and producer.capture is not None else {}
        with boundary:
            metrics = render.render(self.arrangement, self.output, cancel=cancel, **options)
        self.assertFalse(api.states)
        return metrics

    def insert(self, identity, spec, *, state='queued', result=None):
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (identity, state, 1 if state=='complete' else 0,
                json.dumps(spec), json.dumps(result) if result else None, None, time.time()))

    def complete(self):
        producer = self.producer()
        try:
            metrics = self.render(producer)
            companion = producer.finish(self.output, metrics)
            self.assertEqual(companion['status'], 'available', companion)
            asset = self.library.publish(self.output, 'preview.wav', 'audio/wav', kind='mixed-preview-2', source_id=self.source['source_id'])
            result = dict(asset=asset, arrangement=self.arrangement, metrics={**metrics, 'seconds':.1, 'uses_gpu':False},
                          notice='Authored fake boundary', analysis=None, acoustic_companion=companion)
            self.insert(self.identity, self.spec, state='complete', result=result)
            return self.library.job(self.identity)
        finally: producer.cleanup()

    def test_available_reference_fresh_admission_and_saved_held_readers(self):
        job = self.complete(); before = deepcopy(job)
        companion = job['result']['acoustic_companion']
        self.assertEqual(set(companion), {'version','status','reference','arrangement_sha256'})
        self.assertNotIn(self.temp.name, json.dumps(companion))
        forecast = parts.forecast_capture(performers=2, frames=44111)
        self.assertEqual(self.space.call_args_list[0].args, (forecast['additional_disk_bytes']+forecast['original_render_allowance_bytes'],))
        checked = parts.verify_saved(self.library, job)
        self.assertEqual(checked['manifest']['arrangement']['revision'], self.identity)
        self.assertEqual(checked['manifest']['arrangement']['sha256'], companion['arrangement_sha256'])
        with parts.protect_saved(self.library, job) as held:
            stream = held['audio']['performer-1.wav']; self.assertEqual(stream.read(4), b'RIFF')
            with self.assertRaises(OSError):
                with (self.library.media/job['result']['asset']['sha256']).open('ab') as changed:
                    changed.write(b'forbidden while protected')
        self.assertTrue(stream.closed)
        self.assertEqual(self.library.job(self.identity), before)
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())

    def test_legacy_never_constructs_capture_or_admits_more_disk(self):
        self.spec.pop(parts.KEY)
        with patch.object(parts, 'AcousticStemCapture') as capture:
            producer = self.producer(); metrics = self.render(producer)
            self.assertIsNone(producer.finish(self.output, metrics)); producer.cleanup()
        capture.assert_not_called(); self.space.assert_not_called()

    def test_occupied_crash_capture_is_preserved_and_only_companion_refuses(self):
        folder=StemStore(self.library).capture_folder(self.identity); folder.mkdir()
        sentinel=folder/'unknown.f64'; sentinel.write_bytes(b'preserve unknown crash evidence')
        producer=self.producer()
        try:
            metrics=self.render(producer)
            refused=producer.finish(self.output,metrics)
            self.assertEqual(refused['status'],'unavailable')
            self.assertEqual(refused['diagnostic']['error_type'],'FileExistsError')
            self.assertEqual(refused['diagnostic']['capture_stage'],'admission')
            self.assertEqual(refused['diagnostic']['captured_frames'],0)
            self.assertEqual(refused['diagnostic']['expected_frames'],44111)
        finally:producer.cleanup()
        self.assertEqual(sentinel.read_bytes(),b'preserve unknown crash evidence')
        self.assertTrue(self.output.is_file())

    def test_bad_request_version_kind_and_instrument_refuse_before_capture(self):
        for change in ({parts.KEY:'future'}, {'kind':'diagnostic'}, {'renderer_version':'old'}, {'performers':3}, {'instrument':'jazz-bass'}):
            with self.subTest(change=change), patch.object(parts, 'AcousticStemCapture') as capture:
                with self.assertRaises(ValueError): parts.enabled({**self.spec, **change})
                capture.assert_not_called()

    def test_disk_refusal_keeps_the_normal_master_without_paths_in_diagnosis(self):
        self.space.side_effect = ValueError('C:/private/path/storage refused')
        producer = self.producer(); self.assertIsNone(producer.capture)
        metrics = self.render(producer)
        record = producer.finish(self.output, metrics); producer.cleanup()
        self.assertEqual(record['status'], 'unavailable')
        self.assertEqual(set(record), {'version','status','diagnostic'})
        self.assertEqual(record['diagnostic']['error_type'],'ValueError')
        self.assertNotIn('private', json.dumps(record)); self.assertTrue(self.output.is_file())

    def test_capture_write_refusal_leaves_identical_master(self):
        baseline = self.render(); expected = self.output.read_bytes(); self.output.unlink()
        producer = self.producer()
        class FailedWriter:
            def __init__(self, stream): self.stream=stream
            def write(self, raw): raise OSError('private capture path')
            def close(self): self.stream.close()
        producer.capture._streams[1] = FailedWriter(producer.capture._streams[1])
        try:
            metrics = self.render(producer)
            self.assertEqual(metrics, baseline); self.assertEqual(self.output.read_bytes(), expected)
            refused=producer.finish(self.output, metrics)
            self.assertEqual(refused['status'], 'unavailable')
            self.assertEqual(refused['diagnostic']['error_type'],'ValueError')
            self.assertEqual(refused['diagnostic']['capture_error_type'],'OSError')
            self.assertEqual(refused['diagnostic']['capture_stage'],'capture')
            self.assertEqual(refused['diagnostic']['captured_frames'],0)
            self.assertEqual(refused['diagnostic']['expected_frames'],44111)
            self.assertNotIn('private',json.dumps(refused))
        finally: producer.cleanup()
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())

    def test_publication_refusal_preserves_finished_master(self):
        producer = self.producer()
        try:
            metrics = self.render(producer); expected = self.output.read_bytes()
            with patch.object(StemStore, 'publish', side_effect=CompanionStoreError('copy', OSError('private path'))):
                record = producer.finish(self.output, metrics)
            self.assertEqual(record['status'], 'unavailable'); self.assertEqual(record['diagnostic']['stage'], 'publication')
            self.assertEqual(record['diagnostic']['error_type'],'CompanionStoreError')
            self.assertNotIn('private',json.dumps(record))
            self.assertEqual(self.output.read_bytes(), expected)
        finally: producer.cleanup()

    def test_actual_cancellation_propagates_and_removes_owned_scratch(self):
        stopped = [False]
        producer = parts.AcousticCompanion(self.library, self.identity, self.spec, self.source, self.arrangement,
                                           cancel=lambda:stopped[0])
        try:
            metrics = self.render(producer); stopped[0] = True
            with self.assertRaises(InterruptedError): producer.finish(self.output, metrics)
        finally: producer.cleanup()
        self.assertTrue(self.output.is_file())
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())
        self.assertFalse((self.library.root/'stem-bundles').exists())

    def test_later_db_failure_preserves_complete_unreferenced_bundle(self):
        producer = self.producer()
        try:
            metrics = self.render(producer); companion = producer.finish(self.output, metrics)
            self.insert(self.identity, self.spec)
            with patch.object(Library, 'update_job', side_effect=sqlite3.OperationalError('authored commit failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.library.update_job(self.identity, 'complete', 1, result={'acoustic_companion':companion})
        finally: producer.cleanup()
        folder = self.library.root/'stem-bundles'/companion['reference']['bundle_id']
        self.assertTrue((folder/'combined.wav').is_file())
        self.assertIsNone(self.library.job(self.identity)['result'])
        self.assertTrue(self.output.is_file())

    def test_saved_context_tampering_and_unavailable_schema_never_mutate_job(self):
        job = self.complete(); original = deepcopy(job)
        changes = [lambda j:j['request'].update(sha256='a'*64),
            lambda j:j['result']['asset'].update(sha256='b'*64),
            lambda j:j['result']['asset'].update(source_id=str(uuid.uuid4())),
            lambda j:j['result']['asset'].update(size=1),
            lambda j:j['result']['asset'].update(mime='audio/mpeg'),
            lambda j:j['result']['arrangement']['notes'][0].update(pitch=63),
            lambda j:j['result']['acoustic_companion'].update(arrangement_sha256='c'*64),
            lambda j:j['result']['acoustic_companion'].update(extra=True),
            lambda j:j['result'].update(acoustic_companion={'version':parts.VERSION,'status':'unavailable','diagnostic':{'stage':'capture','reason':'Unavailable'}})]
        for change in changes:
            changed = deepcopy(job); change(changed)
            with self.subTest(change=change), self.assertRaises(ValueError): parts.verify_saved(self.library, changed)
        self.assertEqual(self.library.job(self.identity), original)

    def test_actual_master_corruption_refuses_parts_without_changing_saved_result(self):
        job = self.complete(); asset = job['result']['asset']; path=self.library.media/asset['sha256']
        raw=path.read_bytes(); path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(ValueError): parts.verify_saved(self.library, job)
        self.assertEqual(self.library.job(self.identity), job)

    def test_self_consistent_command_rebinding_still_refuses_committed_arrangement(self):
        from stem_evidence import verify_bundle
        job=self.complete(); changed=deepcopy(job)
        reference=changed['result']['acoustic_companion']['reference']
        folder=self.library.root/'stem-bundles'/reference['bundle_id']
        receipt=json.loads((folder/'renderer-receipt.json').read_bytes())
        receipt['commands'][0]['velocity']-=1
        receipt['commands_sha256']=parts._sha(receipt['commands'])
        raw=parts._canonical(receipt); (folder/'renderer-receipt.json').write_bytes(raw)
        reference['renderer_receipt']={'sha256':parts.hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        manifest=json.loads((folder/'manifest.json').read_bytes())
        manifest['renderer']['receipt_sha256']=reference['renderer_receipt']['sha256']
        raw=parts._canonical(manifest); (folder/'manifest.json').write_bytes(raw)
        reference['manifest']={'sha256':parts.hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        reference['bundle_id']=reference['manifest']['sha256']
        binding={key:manifest[key] for key in ('source','clock','arrangement','renderer')}
        verified=verify_bundle(folder,manifest,expected_binding=binding,
            expected_manifest_sha256=reference['bundle_id'],max_frames=31*44100,
            max_total_audio_bytes=sum(row['bytes'] for row in reference['audio']))
        raw=parts._canonical(verified['verification']); (folder/'verification.json').write_bytes(raw)
        reference['verification']={'sha256':parts.hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        target=folder.parent/reference['bundle_id']
        self.assertEqual(target.parent,self.library.root/'stem-bundles'); folder.rename(target)
        # The independently re-bound store is internally valid. Job/command
        # association must still reject the changed producer declaration.
        StemStore(self.library).verify(reference,expected_binding=binding,
            expected_manifest_sha256=reference['bundle_id'],
            expected_renderer_receipt_sha256=reference['renderer_receipt']['sha256'],
            max_total_audio_bytes=sum(row['bytes'] for row in reference['audio']))
        with self.assertRaises(ValueError):parts.verify_saved(self.library,changed)
        self.assertEqual(self.library.job(self.identity),job)

    def test_refinement_versions_preserve_old_parent_and_frozen_absence(self):
        from preview_refinement import prepare_refinement
        parent = self.complete(); before=deepcopy(parent)
        operations=[{'kind':'omit','note_ids':['lead-b']}]
        new = prepare_refinement(self.library, self.identity, operations)
        self.assertEqual(new['spec'][parts.KEY], parts.VERSION)
        old = prepare_refinement(self.library, self.identity, operations, frozen={})
        self.assertNotIn(parts.KEY, old['spec'])
        replay = prepare_refinement(self.library, self.identity, operations, frozen=new['spec'])
        self.assertEqual(replay['spec'], new['spec'])
        self.assertEqual(self.library.job(self.identity), before)

    def test_selected_instrumental_new_and_frozen_version(self):
        from tests.test_instrumental_preview import InstrumentalPreview
        from instrumental_preview import prepare_arrangement
        fixture = InstrumentalPreview(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        before=deepcopy(fixture.library.job(fixture.parent_id))
        new=prepare_arrangement(fixture.library, fixture.request())
        self.assertEqual(new['spec'][parts.KEY], parts.VERSION)
        old=prepare_arrangement(fixture.library, fixture.request(), frozen={})
        self.assertNotIn(parts.KEY, old['spec'])
        self.assertEqual(prepare_arrangement(fixture.library,fixture.request(),frozen=new['spec'])['spec'],new['spec'])
        self.assertEqual(fixture.library.job(fixture.parent_id), before)


class WorkerIntegration(unittest.TestCase):
    setUp = Companion.setUp
    insert = Companion.insert
    producer = Companion.producer
    render = Companion.render
    complete = Companion.complete
    def execute(self, *, fail_commit=False, cancel_after_render=False):
        from audio.mixed_analysis import PIPELINE_VERSION
        if self.spec['kind']=='mixed':self.spec.update(pipeline_version=PIPELINE_VERSION, runtime_version='authored-runtime')
        self.insert(self.identity, self.spec)
        envelope = dict(performance=asdict(performance(self.source)), report={'warnings':[]}, render_origin_sample=0)
        def analysis(module, request, **kwargs):
            self.assertEqual(module, 'audio.mixed_analysis')
            raw=json.dumps(envelope).encode(); Path(request['result_path']).write_bytes(raw)
            return {'result':{'result_bytes':len(raw),'result_sha256':parts.hashlib.sha256(raw).hexdigest()},'metrics':{'authored':True}}
        boundary, api = fake_boundary()
        original_publish=Library.publish
        def publish(library, staged, *args, **kwargs):
            pending=library.root/'stem-bundles'
            if parts.KEY in self.spec and not self.space.side_effect:
                self.assertTrue(pending.is_dir(), 'Companion must publish before Library consumes master')
            return original_publish(library, staged, *args, **kwargs)
        fake_ctypes=SimpleNamespace(**{k:getattr(ctypes,k) for k in ('Structure','c_ulong','c_size_t','c_void_p','sizeof','POINTER','byref')},
            windll=SimpleNamespace(kernel32=SimpleNamespace(GetCurrentProcess=Mock(return_value=1)),
                                   psapi=SimpleNamespace(GetProcessMemoryInfo=Mock(return_value=False))))
        original_render=render.render
        def rendering(*args, **kwargs):
            metrics=original_render(*args, **kwargs)
            if cancel_after_render:(self.library.stage/(self.identity+'.cancel')).touch()
            return metrics
        update=Library.update_job
        def updating(library, identity, state, *args, **kwargs):
            if fail_commit and state=='complete':raise sqlite3.OperationalError('authored final DB failure')
            return update(library,identity,state,*args,**kwargs)
        with boundary, patch('audio.analysis_runtime.require_runtime',return_value={'manifest':{'runtime_version':'authored-runtime'},'modeldir':'authored-unused','python':'authored-unused'}), \
             patch.object(worker,'run_worker',side_effect=analysis), patch.object(worker,'render',side_effect=rendering) as calls, \
             patch.object(worker,'ctypes',fake_ctypes), patch.object(Library,'publish',new=publish), \
             patch.object(Library,'update_job',new=updating):
            worker.run(str(self.library.root),self.identity)
        self.assertFalse(api.states)
        calls.assert_called_once()
        return self.library.job(self.identity)

    def test_worker_available_finishes_before_master_consumed_and_metrics_augmented(self):
        job=self.execute(); self.assertEqual(job['state'],'complete',job)
        self.assertEqual(job['result']['acoustic_companion']['status'],'available')
        self.assertIn('seconds',job['result']['metrics'])
        parts.verify_saved(self.library,job)
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())

    def test_worker_historical_request_has_no_companion_field(self):
        self.spec.pop(parts.KEY)
        job=self.execute(); self.assertEqual(job['state'],'complete',job)
        self.assertNotIn('acoustic_companion',job['result'])

    def test_worker_cancel_cleans_companion_scratch_and_preserves_original(self):
        job=self.execute(cancel_after_render=True); self.assertEqual(job['state'],'cancelled',job)
        self.assertTrue(self.library.verify(self.source['id']))
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())

    def test_worker_final_db_failure_keeps_complete_orphan(self):
        job=self.execute(fail_commit=True); self.assertEqual(job['state'],'failed',job)
        bundles=list((self.library.root/'stem-bundles').iterdir())
        self.assertEqual(len(bundles),1); self.assertTrue((bundles[0]/'combined.wav').is_file())
        self.assertFalse(StemStore(self.library).capture_folder(self.identity).exists())

    def test_refined_worker_generates_new_parts_without_changing_parent(self):
        from preview_refinement import prepare_refinement
        parent=self.complete(); previous=self.identity
        self.spec=prepare_refinement(self.library,previous,[{'kind':'omit','note_ids':['lead-b']}])['spec']
        self.identity=str(uuid.uuid4())
        job=self.execute(); self.assertEqual(job['state'],'complete',job)
        self.assertEqual(job['result']['acoustic_companion']['status'],'available')
        parts.verify_saved(self.library,job)
        self.assertEqual(self.library.job(previous),parent)

    def test_selected_instrumental_worker_generates_parts_without_reanalysis(self):
        from tests.test_instrumental_preview import InstrumentalPreview
        from instrumental_preview import prepare_arrangement
        fixture=InstrumentalPreview(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.library=fixture.library; self.source=fixture.source
        parent=deepcopy(fixture.library.job(fixture.parent_id))
        self.spec=prepare_arrangement(self.library,fixture.request())['spec']; self.identity=str(uuid.uuid4())
        job=self.execute(); self.assertEqual(job['state'],'complete',job)
        self.assertEqual(job['result']['acoustic_companion']['status'],'available')
        parts.verify_saved(self.library,job)
        self.assertEqual(self.library.job(fixture.parent_id),parent)


if __name__=='__main__':unittest.main()
