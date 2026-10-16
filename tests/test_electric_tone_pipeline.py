"""Actual worker/store/catalog with authored bytes and fake managed boundary only."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import worker
from library import Library
from audio.performance import Performance,Note
from audio.preview_instruments import selection_fields,renderer_version,validate_saved_selection
from audio.mixed_analysis import PIPELINE_VERSION
from audio import electric_tone_worker as stage
from audio import electric_tone as component
from render_evidence import RenderEvidence,reconcile_render_evidence
from tone_evidence import ToneEvidence,verify_preview,cleanup_work
from preview_refinement import prepare_refinement
from tests.electric_render_fixture import write_synthetic_render


class TonePipeline(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.library=Library(self.temp.name)
        source=self.library.stage/'source';source.write_bytes(b'authored-original-identity-only')
        self.asset=self.library.publish(source,'source.wav','audio/wav')
        self.before=(self.library.media/self.asset['sha256']).read_bytes()
        self.calls=[];self.cancel=False;self.bad_cleanup=False;self.tamper=False;self.cancel_after=False;self.child_error=False
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('audio.analysis_runtime.require_runtime',return_value={'python':'not-launched','modeldir':'not-read','manifest':{'runtime_version':'fake'}}))
        self.stack.enter_context(patch('worker.run_worker',side_effect=self.analysis))
        self.stack.enter_context(patch('worker.render_electric',side_effect=self.render))
        self.stack.enter_context(patch('tone_preview.run_worker',side_effect=self.managed))
        self.stack.enter_context(patch.object(stage,'_assert_owned_worker'))

    def create(self,settings=None,spec=None):
        if spec is None:
            selection=selection_fields({'instrument':'electric','electric_tone':settings or {'mode':'clean','gain_db':-3}},1)
            spec=dict(kind='mixed',asset_id=self.asset['id'],sha256=self.asset['sha256'],performers=1,
                start_seconds=90.,end_seconds=91.,pipeline_version=PIPELINE_VERSION,runtime_version='fake',
                renderer_version=renderer_version(selection),**selection)
        identity=str(uuid.uuid4())
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(identity,'queued',0,json.dumps(spec),None,None,time.time()))
        return identity,spec

    def analysis(self,module,request,**kwargs):
        self.calls.append('analysis');origin=90*44100
        performance=Performance(self.asset['source_id'],44100,[Note('first',origin+10,origin+300,64,80,'melody',.7),
                                                             Note('second',origin+300,origin+600,64,80,'melody',.7)])
        data=json.dumps(dict(performance=asdict(performance),report={'fixture':True},render_origin_sample=origin)).encode()
        Path(request['result_path']).write_bytes(data)
        return {'result':{'result_bytes':len(data),'result_sha256':hashlib.sha256(data).hexdigest()},'metrics':{'fake':True}}

    def render(self,arrangement,output,**kwargs):
        self.calls.append('dry-render')
        write_synthetic_render(self.library,output.stem,arrangement)
        return {'renderer_version':renderer_version(selection_fields({'instrument':'electric'},1)),'musical_acceptance':False}

    def managed(self,module,request,**kwargs):
        self.calls.append('tone');self.assertEqual(module,'audio.electric_tone_worker')
        self.assertLessEqual(kwargs['wall_seconds'],60);self.assertEqual(kwargs['memory_bytes'],512*1024**2)
        # Real Windows protection held by the parent before entering the child.
        if os.name=='nt':
            with self.assertRaises(OSError):
                with (self.library.stage/(request['job_id']+'.wav')).open('r+b'):pass
        if self.child_error:
            error=worker.ManagedProcessError('Authored child processing failure')
            error.metrics={'elapsed_seconds':.1,'cleanup':{'proven':True},'primary_error':{'detail':'Authored child processing failure'}}
            raise error
        value=stage.run(request,kwargs['progress'])
        if self.tamper and value['state']=='processed':
            path=stage.work_path(self.library.root,request['job_id'])/'processed.f64'
            with path.open('r+b') as handle:handle.seek(0);handle.write(b'\0'*8)
        if self.cancel_after:self.cancel=True
        return {'result':value,'metrics':{'fake_boundary':True},'cleanup':{'proven':not self.bad_cleanup}}

    def execute(self,identity):
        with self.library.exclusive('worker.lock'):worker.run(self.temp.name,identity,parent_gone=lambda:self.cancel)
        self.assertEqual((self.library.media/self.asset['sha256']).read_bytes(),self.before)
        self.assertEqual(list(self.library.stage.iterdir()),[])
        return self.library.job(identity)

    def assert_failed_dry(self,job):
        result=job['result'];self.assertNotIn('asset',result);self.assertNotIn('tone_evidence',result)
        self.assertNotIn('effects',result);self.assertEqual(result['effects_failure']['stage'],'effects')
        self.assertEqual(result['effects_failure']['status'],job['state'])
        dry=RenderEvidence(self.library).verify(result['render_evidence'],wav_sha256=result['dry_input']['sha256'],arrangement=result['arrangement'])
        self.assertEqual(result['dry_input']['bytes'],44+dry['receipt']['frames']*4)
        with self.library.exclusive('worker.lock'):self.assertEqual(reconcile_render_evidence(self.library)['files'],0)
        self.assertTrue(list((self.library.root/'render-evidence').glob('*.f32')))

    def test_full_tone_chain_publishes_separate_asset_and_unchanged_dry_evidence(self):
        identity,spec=self.create();job=self.execute(identity)
        self.assertEqual(job['state'],'complete',job['error']);result=job['result']
        self.assertEqual(self.calls,['analysis','dry-render','tone'])
        self.assertEqual(result['asset']['kind'],'electric-preview-1-tone')
        receipt=verify_preview(self.library,result,validate_saved_selection(spec))
        self.assertNotEqual(receipt['binding']['input']['sha256'],result['asset']['sha256'])
        dry=RenderEvidence(self.library).verify(result['render_evidence'],wav_sha256=receipt['binding']['input']['sha256'],arrangement=result['arrangement'])
        self.assertEqual(dry['receipt']['effects'],[])
        self.assertFalse(result['effects']['musical_acceptance'])
        self.assertEqual(result['effects']['processing']['source_origin_sample'],90*44100)
        self.assertEqual(result['effects']['processing']['output_frames'],dry['receipt']['frames']+2173)
        with self.library.exclusive('worker.lock'):self.assertEqual(reconcile_render_evidence(self.library)['files'],0)

    def test_refinement_inherits_settings_and_applies_once_to_new_dry_input(self):
        identity,spec=self.create();parent=self.execute(identity);self.assertEqual(parent['state'],'complete',parent['error'])
        before=deepcopy(parent)
        prepared=prepare_refinement(self.library,identity,[{'kind':'omit','note_ids':['second']}])
        self.assertEqual(prepared['spec']['electric_tone'],spec['electric_tone'])
        revised,_=self.create(spec=prepared['spec']);job=self.execute(revised)
        self.assertEqual(job['state'],'complete',job['error'])
        self.assertEqual(self.calls,['analysis','dry-render','tone','dry-render','tone'])
        self.assertEqual(self.library.job(identity),before)
        self.assertEqual(len(job['result']['arrangement']['notes']),1)
        self.assertNotEqual(job['result']['effects']['binding']['input']['sha256'],parent['result']['asset']['sha256'])

    def test_bad_cleanup_or_float_tamper_publishes_no_asset(self):
        for field in ('bad_cleanup','tamper','child_error'):
            setattr(self,field,True)
            with self.subTest(field=field):
                identity,_=self.create();job=self.execute(identity)
                self.assertEqual(job['state'],'failed');self.assertEqual(len(self.library.items()),1)
                self.assert_failed_dry(job)
                self.assertTrue(job['result']['effects_failure']['observed']['detail'])
                if field=='bad_cleanup':self.assertFalse(job['result']['tone_process']['cleanup']['proven'])
                if field=='child_error':self.assertIn('Authored child',job['error'])
            setattr(self,field,False)

    def test_overload_has_failed_effects_diagnosis_and_keeps_verified_dry(self):
        identity,_=self.create(dict(mode='clean',gain_db=6,low_cut_hz=60,high_cut_hz=7500))
        job=self.execute(identity);self.assertEqual(job['state'],'failed',job['error'])
        result=job['result'];self.assertEqual(result['effects_failure']['stage'],'effects')
        self.assert_failed_dry(job)
        self.assertNotIn('asset',result);self.assertNotIn('tone_evidence',result)
        self.assertIn('render_evidence',result);self.assertEqual(len(self.library.items()),1)
        self.assertTrue(list((self.library.root/'render-evidence').glob('*.f32')))

    def test_cancel_after_child_never_publishes_processed_asset(self):
        self.cancel_after=True;identity,_=self.create();job=self.execute(identity)
        self.assertEqual(job['state'],'cancelled');self.assertEqual(len(self.library.items()),1)
        self.assert_failed_dry(job)

    def test_missing_wet_chain_cannot_be_reclassified_as_dry_by_collection(self):
        identity,spec=self.create();job=self.execute(identity);self.assertEqual(job['state'],'complete',job['error'])
        result=job['result'];dry_sha=result['effects']['binding']['input']['sha256']
        del result['tone_evidence'];del result['effects'];result['asset']['sha256']=dry_sha
        self.library.update_job(identity,'complete',result=result)
        before={p.name:p.read_bytes() for p in (self.library.root/'render-evidence').iterdir()}
        with self.assertRaises((ValueError,KeyError)):
            with self.library.exclusive('worker.lock'):reconcile_render_evidence(self.library)
        self.assertEqual(before,{p.name:p.read_bytes() for p in (self.library.root/'render-evidence').iterdir()})

    def test_failure_missing_or_crossed_dry_diagnosis_aborts_collection(self):
        self.bad_cleanup=True;identity,_=self.create();job=self.execute(identity);self.assert_failed_dry(job)
        for field in ('effects_failure','dry_input'):
            with self.subTest(field=field):
                result=deepcopy(job['result']);del result[field]
                self.library.update_job(identity,'failed',result=result)
                with self.assertRaises((ValueError,KeyError)):
                    with self.library.exclusive('worker.lock'):reconcile_render_evidence(self.library)
                self.assertTrue(list((self.library.root/'render-evidence').glob('*.f32')))

    def test_malformed_processed_chain_prevents_reuse_and_dry_collection(self):
        identity,spec=self.create();job=self.execute(identity);self.assertEqual(job['state'],'complete',job['error'])
        result=job['result'];before={p.name:p.read_bytes() for p in (self.library.root/'render-evidence').iterdir()}
        result['tone_evidence']['receipt']['sha256']='0'*64
        self.library.update_job(identity,'complete',result=result)
        with self.assertRaises((ValueError,OSError)):
            with self.library.exclusive('worker.lock'):reconcile_render_evidence(self.library)
        self.assertEqual({p.name:p.read_bytes() for p in (self.library.root/'render-evidence').iterdir()},before)
        with self.assertRaises(ValueError):prepare_refinement(self.library,identity,[{'kind':'omit','note_ids':['second']}])

    def test_unknown_work_content_is_preserved(self):
        identity,_=self.create();folder=stage.work_path(self.library.root,identity);folder.mkdir()
        (folder/'unknown').write_bytes(b'preserve')
        with self.assertRaises(ValueError):cleanup_work(self.library,identity)
        self.assertTrue((folder/'unknown').exists())


class ToneSelection(unittest.TestCase):
    def test_default_and_explicit_bypass_preserve_exact_dry_selection(self):
        dry=selection_fields({'instrument':'electric'},1)
        self.assertEqual(selection_fields({'instrument':'electric','electric_tone':{'mode':'bypass'}},1),dry)
        self.assertEqual(selection_fields({},1),{})

    def test_reject_cross_instrument_stale_versions_and_export_report(self):
        for instrument in ('acoustic','jazz-bass'):
            with self.assertRaises(ValueError):selection_fields({'instrument':instrument,'electric_tone':{}},1)
        with self.assertRaises(ValueError):selection_fields({'instrument':'electric','electric_tone':{'mode':'clean','overload':'report'}},1)
        selection=selection_fields({'instrument':'electric','electric_tone':{'mode':'clean'}},1)
        for field in ('electric_tone_version','electric_tone_window_version'):
            with self.assertRaises(ValueError):validate_saved_selection({**selection,'performers':1,field:'stale'})


if __name__=='__main__':unittest.main()
