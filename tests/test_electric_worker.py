"""Real worker/catalog with synthetic stubs; never models or native audio."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import worker
from library import Library
from audio.mixed_analysis import PIPELINE_VERSION
from audio.performance import Note, Performance
from audio.preview_instruments import selection_fields, renderer_version, arrange_selected

RUNTIME={'python':'never-executed','modeldir':'never-loaded',
         'manifest':{'runtime_version':'silent-runtime'}}
PROTOTYPE={'renderer_version':'private-prototype-stub','native_adapter_acceptance':False,
           'musical_acceptance':False,'app_mode_enabled':False}
REFERENCE={'format':'electric-render-evidence-v1','raw':{'sha256':'1'*64,'bytes':8},
           'receipt':{'sha256':'2'*64,'bytes':100}}


class ElectricWorker(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.library=Library(self.temp.name)
        staged=self.library.stage/'source';staged.write_bytes(b'original-unmodified-silent-fixture')
        self.asset=self.library.publish(staged,'Source.wav','audio/wav')
        self.original=(self.library.media/self.asset['sha256']).read_bytes()
        self.calls=[];self.cancel_now=False;self.render_failure=None;self.evidence_failure=None
        self.cancel_after_render=False;self.cancel_after_evidence=False;self.analysis_roles=('melody','bass')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.runtime=self.stack.enter_context(patch('audio.analysis_runtime.require_runtime',return_value=RUNTIME))
        self.analyzer=self.stack.enter_context(patch('worker.run_worker',side_effect=self.analyze))
        self.acoustic=self.stack.enter_context(patch('worker.render',side_effect=lambda *a,**kw:self.render('acoustic',*a,**kw)))
        self.bass=self.stack.enter_context(patch('worker.render_bass',side_effect=lambda *a,**kw:self.render('jazz-bass',*a,**kw)))
        self.electric=self.stack.enter_context(patch('worker.render_electric',side_effect=lambda *a,**kw:self.render('electric',*a,**kw)))
        self.evidence=self.stack.enter_context(patch('worker.RenderEvidence',return_value=SimpleNamespace(publish=self.publish_evidence)))
        self.gc=self.stack.enter_context(patch('worker.reconcile_render_evidence',return_value={'files':0,'bytes':0}))
        original_publish=Library.publish
        def publish(library,*args,**kwargs):
            self.calls.append(('media',kwargs['kind']))
            return original_publish(library,*args,**kwargs)
        self.media=self.stack.enter_context(patch.object(Library,'publish',autospec=True,side_effect=publish))

    def create(self,instrument='electric',players=2,kind='mixed',**changes):
        selection=selection_fields({'instrument':instrument},players)
        spec=dict(kind=kind,asset_id=self.asset['id'],sha256=self.asset['sha256'],
                  performers=players,start_seconds=90.,end_seconds=91.,pipeline_version=PIPELINE_VERSION,
                  runtime_version=RUNTIME['manifest']['runtime_version'],renderer_version=renderer_version(selection),**selection)
        spec.update(changes);identity=str(uuid.uuid4())
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity,'queued',0,json.dumps(spec),None,None,time.time()))
        return identity,spec

    def source_performance(self,origin=90*44100):
        return Performance(self.asset['source_id'],44100,[
            Note(role,origin+1000,origin+20000,64 if role=='melody' else 45,
                 80 if role=='melody' else 64,role,.6,contour=((origin+1200,64.1),) if role=='melody' else ())
            for role in self.analysis_roles])

    def analyze(self,module,request,**kwargs):
        self.calls.append(('analysis',deepcopy(request)))
        self.assertEqual(module,'audio.mixed_analysis');self.assertFalse(kwargs['cancel']())
        origin=round(request['start_seconds']*44100)
        envelope=dict(performance=asdict(self.source_performance(origin)),report={'warnings':['Synthetic estimates only']},render_origin_sample=origin)
        raw=json.dumps(envelope).encode();path=Path(request['result_path']);path.write_bytes(raw)
        path.with_suffix('.decoded.wav').write_bytes(b'decoded-stub')
        path.with_suffix('.decoded.wav.decode.partial').write_bytes(b'partial-stub')
        return dict(result={'result_bytes':len(raw),'result_sha256':hashlib.sha256(raw).hexdigest()},metrics={'silent_stub':True})

    def render(self,instrument,arrangement,output,**kwargs):
        self.calls.append(('render',instrument,deepcopy(arrangement)))
        output.write_bytes(('silent-output-'+instrument).encode())
        if instrument=='electric':
            output.with_suffix('.wav.dry.f32').write_bytes(b'raw-stub')
            output.with_suffix('.wav.receipt.json').write_text(json.dumps(PROTOTYPE),encoding='utf-8')
            for suffix in ('.wav.partial','.wav.float.partial','.wav.receipt.partial'):
                output.with_suffix(suffix).write_bytes(b'partial-stub')
        if self.render_failure:raise self.render_failure
        if self.cancel_after_render:self.cancel_now=True
        return {'renderer_version':renderer_version(selection_fields({'instrument':instrument},arrangement['performers'])),
                'clipped_samples':0,'wrapper_metrics_only':True}

    def publish_evidence(self,raw,receipt,*,wav_path,arrangement,cancel):
        self.calls.append(('evidence',deepcopy(arrangement)))
        self.assertTrue(raw.exists());self.assertTrue(wav_path.exists());self.assertFalse(cancel())
        self.assertEqual(json.loads(receipt.read_text(encoding='utf-8')),PROTOTYPE)
        self.assertEqual(raw,wav_path.with_suffix('.wav.dry.f32'))
        self.assertEqual(receipt,wav_path.with_suffix('.wav.receipt.json'))
        self.assertEqual(self.library.job(wav_path.stem)['state'],'running')
        if self.evidence_failure:raise self.evidence_failure
        if self.cancel_after_evidence:self.cancel_now=True
        return deepcopy(REFERENCE)

    def execute(self,identity):
        # Same lock ownership contract as the real worker entry point.
        with self.library.exclusive('worker.lock'):
            worker.run(self.temp.name,identity,parent_gone=lambda:self.cancel_now)
        self.assertEqual((self.library.media/self.asset['sha256']).read_bytes(),self.original)
        self.assertEqual(list(self.library.stage.iterdir()),[])
        return self.library.job(identity)

    def test_nonzero_origin_duo_roles_explicit_dispatch_and_evidence_before_media(self):
        identity,spec=self.create();result=self.execute(identity)
        self.assertEqual(result['state'],'complete',result['error'])
        self.assertEqual([x[0] for x in self.calls],['analysis','render','evidence','media'])
        self.assertEqual(self.calls[0][1]['required_roles'],['melody','bass'])
        self.assertNotIn('guidance',self.calls[0][1]);self.acoustic.assert_not_called();self.bass.assert_not_called()
        saved=result['result'];a=saved['arrangement'];self.assertEqual(a['render_origin_sample'],90*44100)
        self.assertEqual(a['source'],json.loads(json.dumps(asdict(self.source_performance()))))
        self.assertEqual({n['player'] for n in a['notes']},{1,2})
        self.assertEqual(saved['render_evidence'],REFERENCE)
        self.assertEqual(saved['asset']['kind'],'electric-preview-2');self.assertIn('Electric',saved['asset']['filename'])
        self.assertNotIn('Jazz Bass',saved['notice']);self.assertIn('No source audio',saved['notice'])
        self.assertTrue(saved['metrics']['wrapper_metrics_only']);self.assertEqual(saved['metrics']['renderer_version'],spec['renderer_version'])

    def test_electric_duo_requires_analyzed_bass_before_planning_or_render(self):
        self.analysis_roles=('melody',);identity,_=self.create();result=self.execute(identity)
        self.assertEqual(result['state'],'failed');self.assertIn('analysis result failed validation',result['error'])
        self.electric.assert_not_called();self.evidence.assert_not_called();self.media.assert_not_called()

    def test_stale_renderer_and_profile_fail_before_analysis(self):
        for field in ('renderer_version','electric_planner_version','electric_profile_version'):
            with self.subTest(field=field):
                identity,_=self.create(**{field:'stale'});result=self.execute(identity)
                self.assertEqual(result['state'],'failed')
        self.analyzer.assert_not_called();self.electric.assert_not_called();self.evidence.assert_not_called()

    def test_refusal_and_late_origin_assignment_never_reach_native_or_evidence(self):
        from audio.preview_instruments import arrange_selected as actual
        for changes in ({'renderable':False},{'render_origin_sample':0}):
            captured=[]
            def changed(*args,**kwargs):
                arrangement=actual(*args,**kwargs)|changes
                if 'renderable' in changes:
                    arrangement['notes']=[n for n in arrangement['notes'] if n['role']!='bass']
                    arrangement['adaptations'].append({'note_id':'bass','kind':'omission','reason':'Frozen test: no available foundation string','essential':False})
                    arrangement['issues'].append({'kind':'performer-without-material','player':2,'reason':'No assigned foundation material'})
                captured.append(deepcopy(arrangement));return arrangement
            with self.subTest(changes=changes),patch('worker.arrange_selected',side_effect=changed):
                identity,_=self.create();result=self.execute(identity);self.assertEqual(result['state'],'failed')
                failed=result['result'];self.assertEqual(set(failed),{'arrangement','analysis','refusal'})
                self.assertEqual(failed['arrangement'],json.loads(json.dumps(captured[0])))
                self.assertEqual(failed['analysis']['warnings'],['Synthetic estimates only'])
                self.assertEqual(failed['analysis']['pipeline_version'],PIPELINE_VERSION)
                refusal=failed['refusal'];self.assertEqual((refusal['stage'],refusal['status']),('arrangement','violates'))
                self.assertEqual(refusal['expected'],dict(renderable=True,render_origin_sample=90*44100,performers=2,players=[1,2]))
                self.assertEqual(refusal['source_clock'],dict(sample_rate=44100,origin_sample=90*44100,end_sample=91*44100))
                self.assertEqual(refusal['units']['render_origin_sample'],'absolute source samples')
                self.assertIn('no musical judgment',refusal['scope']);self.assertIn('No rendering occurred',refusal['scope'])
                self.assertTrue(refusal['next_discriminating_check'])
                if 'renderable' in changes:
                    self.assertFalse(refusal['observed']['renderable']);self.assertEqual(refusal['observed']['players'],[1])
                    self.assertEqual(refusal['observed']['omitted_note_ids'],['bass']);self.assertTrue(refusal['observed']['issues'])
                else:
                    self.assertTrue(refusal['observed']['renderable']);self.assertEqual(refusal['observed']['render_origin_sample'],0)
                    self.assertIn('origin supplied before planning',refusal['next_discriminating_check'])
                self.assertEqual(len(self.library.items()),1)
        self.electric.assert_not_called();self.evidence.assert_not_called();self.media.assert_not_called()

    def test_render_or_evidence_failure_publishes_no_media_and_cleans_sidecars(self):
        for phase in ('render','evidence'):
            self.render_failure=ValueError('Rejected native receipt') if phase=='render' else None
            self.evidence_failure=ValueError('Evidence geometry differs') if phase=='evidence' else None
            with self.subTest(phase=phase):
                identity,_=self.create();result=self.execute(identity);self.assertEqual(result['state'],'failed')
        self.media.assert_not_called();self.assertEqual(len(self.library.items()),1)

    def test_cancel_before_analysis_after_render_and_after_evidence_publishes_nothing(self):
        for phase in ('before','render','evidence'):
            self.cancel_now=phase=='before';self.cancel_after_render=phase=='render';self.cancel_after_evidence=phase=='evidence'
            with self.subTest(phase=phase):
                identity,_=self.create();result=self.execute(identity);self.assertEqual(result['state'],'cancelled')
        self.media.assert_not_called();self.assertEqual(len(self.library.items()),1)

    def test_evidence_cooperative_cancel_is_not_published(self):
        self.evidence_failure=InterruptedError('cancel');identity,_=self.create();result=self.execute(identity)
        self.assertEqual(result['state'],'cancelled');self.media.assert_not_called()

    def test_wav_publication_failure_leaves_no_completed_reference_or_staged_audio(self):
        self.media.side_effect=ValueError('Stored media integrity failure')
        identity,_=self.create();result=self.execute(identity)
        self.assertEqual(result['state'],'failed');self.assertIsNone(result['result'])
        self.assertEqual([row[0] for row in self.calls],['analysis','render','evidence'])
        self.media.assert_called_once();self.gc.assert_called_once()
        self.assertEqual(len(self.library.items()),1)

    def test_maintenance_failures_preserve_completed_status_and_record_private_attention(self):
        for failure in (ValueError('Malformed prior reference'),OSError('Private reader busy'),sqlite3.DatabaseError('Unreadable prior result')):
            self.gc.side_effect=failure
            with self.subTest(failure=type(failure).__name__):
                identity,_=self.create();result=self.execute(identity)
                self.assertEqual(result['state'],'complete',result['error'])
                record=json.loads((self.library.root/(identity+'.cleanup-attention.json')).read_text())
                self.assertEqual(record['attention'],[{'operation':'render-evidence','error_type':type(failure).__name__}])
                self.assertEqual(result['result']['render_evidence'],REFERENCE)

    def test_acoustic_and_bass_dispatch_keep_legacy_kinds_without_render_evidence(self):
        self.gc.side_effect=ValueError('Unrelated prior render reference is malformed')
        for instrument,expected_kind in [('acoustic','mixed-preview-2'),('jazz-bass','bass-preview-2-melody-and-bass')]:
            with self.subTest(instrument=instrument):
                identity,spec=self.create(instrument=instrument);result=self.execute(identity)
                self.assertEqual(result['state'],'complete',result['error'])
                self.assertEqual(result['result']['asset']['kind'],expected_kind)
                self.assertNotIn('render_evidence',result['result'])
                self.assertEqual(result['result']['metrics']['renderer_version'],spec['renderer_version'])
        self.electric.assert_not_called();self.evidence.assert_not_called()
        self.assertNotIn('required_roles',self.calls[0][1])

    def test_refinement_and_selected_candidates_dispatch_electric_without_analysis(self):
        for kind,expected_kind in [('refine','electric-refinement-1'),('instrumental-arrange','instrumental-selected-electric')]:
            identity,spec=self.create(players=1,kind=kind,parent_job_id=str(uuid.uuid4()),operations=[],
                                      interpretation='published_default',note_ids=['melody'],accept_as_lead=True)
            source=self.source_performance();origin=90*44100
            arrangement=arrange_selected(source,1,None,selection_fields(spec,1),render_origin_sample=origin)
            prepared=dict(spec=spec,source=self.asset,origin=origin,bundle={'origin':origin},parent={'result':{'analysis':{'retained':True}}})
            module='preview_refinement' if kind=='refine' else 'instrumental_preview'
            prepare_name='prepare_refinement' if kind=='refine' else 'prepare_arrangement'
            arrange_name='arrange_refinement' if kind=='refine' else 'arrange_review'
            with self.subTest(kind=kind),patch(module+'.'+prepare_name,return_value=prepared),patch(module+'.'+arrange_name,return_value=arrangement):
                result=self.execute(identity);self.assertEqual(result['state'],'complete',result['error'])
                self.assertEqual(result['result']['asset']['kind'],expected_kind)
                self.assertEqual(result['result']['render_evidence'],REFERENCE)
                self.assertIn('Electric',result['result']['asset']['filename'])
                self.assertEqual(result['result']['arrangement']['render_origin_sample'],origin)
        self.analyzer.assert_not_called();self.runtime.assert_not_called();self.acoustic.assert_not_called();self.bass.assert_not_called()


if __name__=='__main__':unittest.main()
