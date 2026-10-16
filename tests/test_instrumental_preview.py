"""Pure saved-candidate/storage tests. No model, decoder or native renderer."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from library import Library
from analysis_evidence import AnalysisEvidence, validate_reference, work_directory, cleanup_work, reconcile_evidence
from instrumental_preview import validate_bundle, checked_candidates, prepare_arrangement, arrange_review, source_anchors, PIPELINE_VERSION, MODEL_SHA


class InstrumentalPreview(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.library=Library(Path(self.temp.name)/'library')
        self.original=b'Original bytes: never analyzed in these unit tests'
        stage=self.library.stage/'original';stage.write_bytes(self.original)
        self.source=self.library.publish(stage,'original.wav','audio/wav')
        self.raw=b'NPZ byte-hash placeholder: array validation belongs to pipeline'
        self.reference={'sha256':hashlib.sha256(self.raw).hexdigest(),'bytes':len(self.raw),'format':'numpy-npz-v1'}
        stage=self.library.stage/'activations.npz';stage.write_bytes(self.raw)
        self.store=AnalysisEvidence(self.library);self.store.publish(stage,self.reference)
        self.origin=44100;self.end=132300
        self.anchors=source_anchors(self.origin,self.end);frames=len(self.anchors)-1
        def event(ident,first,last,pitch,activation):
            return {'id':ident,'start':self.anchors[first],'end':self.anchors[last],'pitch':pitch,
                    'start_frame':first,'end_frame_exclusive':last,
                    'mean_note_activation':activation,'contour_candidates':[{'sample':self.anchors[first]+100,'midi':pitch+.1}]}
        clock={'sample_rate':44100,'origin':self.origin,'end':self.end,'frame_samples':self.anchors[:-1],
               'official_frame_samples':list(self.anchors[:-1]),
               'source_coverage':{'first_anchor':self.origin,'last_boundary':self.anchors[-1],
                                  'passage_end':self.end,'uncovered_tail_samples':self.end-self.anchors[-1]}}
        self.bundle={'version':1,'source_id':self.source['source_id'],'source_sha256':self.source['sha256'],
            'warnings':['Uncertain source instrument; fixture has no model inference'],
            'provenance':{'pipeline_version':PIPELINE_VERSION,'model_sha256':MODEL_SHA,'recognition':{'fixture':'not-run'}},
            'sample_rate':44100,'origin':self.origin,'end':self.end,
            'source_part':{'id':'umx:other','instrument':'unknown','label':'Estimated instrumental mixture'},
            'interpretations':{'published':{'source_clock':copy.deepcopy(clock),
                'policy':'published_default','settings':{'onset_thresh':.5,'frame_thresh':.3,'min_note_len':11,
                    'infer_onsets':True,'melodia_trick':True,'energy_tol':11},
                'events':[event('common',2,15,64,.17),event('p2',25,40,67,.4)]},
                'onset_only':{'source_clock':copy.deepcopy(clock),'version':'basic-pitch-onset-only-research-v1',
                    'model_sha256':MODEL_SHA,'policy':{'onset_threshold':.5,'frame_threshold':.3,'minimum_duration_frames':0,
                        'gap_tolerance_frames':0,'infer_onsets':False,'melodia_trick':False},
                    'events':[event('common',2,9,65,.31),event('o2',35,42,69,.6)]}},
            'raw_activations':{'file':'activations.npz','sha256':self.reference['sha256'],'bytes':len(self.raw),
                'finite':True,'range':[0,1],'sample_rate':44100,'origin':self.origin,'end':self.end,
                'frame_samples':list(self.anchors[:-1]),
                'arrays':{k:{'shape':[frames,width],'dtype':'float32'} for k,width in [('note',88),('onset',88),('contour',264)]}}}
        self.parent_id=str(uuid.uuid4())
        self.parent_spec={'kind':'instrumental','asset_id':self.source['id'],'sha256':self.source['sha256'],'start_seconds':1.,'end_seconds':3.}
        self.result={'candidates':self.bundle,'evidence':self.reference}
        self.save_parent()

    def save_parent(self):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                (self.parent_id,'complete',1,json.dumps(self.parent_spec),json.dumps(self.result),None,time.time()))

    def request(self,**changes):
        return {'parent_job_id':self.parent_id,'interpretation':'published','note_ids':['common'],
                'instrument':'acoustic','accept_as_lead':True,**changes}

    def validate(self,bundle):
        return validate_bundle(bundle,source_id=self.source['source_id'],source_sha256=self.source['sha256'],origin=self.origin,end=self.end)

    def test_detached_validation_retains_unknown_and_both_interpretations(self):
        result=self.validate(self.bundle);self.assertEqual(result,self.bundle)
        result['interpretations']['published']['events'][0]['pitch']=90
        self.assertEqual(self.bundle['interpretations']['published']['events'][0]['pitch'],64)
        self.assertEqual(result['source_part']['instrument'],'unknown')

    def test_optional_electric_tone_changes_only_selection_not_candidate_evidence(self):
        before=copy.deepcopy(self.result)
        dry=prepare_arrangement(self.library,self.request(instrument='electric'))
        wet=prepare_arrangement(self.library,self.request(instrument='electric',electric_tone={
            'mode':'clean','gain_db':-3,'ambience':'room','ambience_mix':.1}))
        self.assertNotEqual(dry['spec']['renderer_version'],wet['spec']['renderer_version'])
        self.assertEqual(wet['spec']['electric_tone']['gain_db'],-3)
        self.assertEqual(arrange_review(dry),arrange_review(wet))
        self.assertEqual(self.library.job(self.parent_id)['result'],before)
        with self.assertRaises(ValueError):prepare_arrangement(self.library,self.request(electric_tone={'mode':'clean'}))

    def test_invalid_identity_clock_instrument_and_nonfinite_metadata(self):
        changes=[('source_id','wrong'),('source_sha256','0'*64),('sample_rate',44100.),('origin',False),
                 ('end',self.end+1),('source_part',{'id':'umx:other','instrument':'guitar'}),
                 ('extra',{'confidence':float('nan')})]
        for key,value in changes:
            b=copy.deepcopy(self.bundle);b[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.validate(b)
        b=copy.deepcopy(self.bundle);del b['interpretations']['onset_only']
        with self.assertRaises(ValueError):self.validate(b)

    def test_source_clock_requires_strict_integers_not_float_equivalence(self):
        for field in ('sample_rate','origin','end'):
            b=copy.deepcopy(self.bundle);clock=b['interpretations']['published']['source_clock'];clock[field]=float(clock[field])
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate(b)

    def test_event_duplicates_bounds_activation_contour_and_count(self):
        for field,value in [('start',True),('end',self.end+1),('pitch',64.),('mean_note_activation',True),
                            ('mean_note_activation',float('inf')),('mean_note_activation',1.1),
                            ('contour_candidates',[{'sample':44000,'midi':64}])]:
            b=copy.deepcopy(self.bundle);b['interpretations']['published']['events'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate(b)
        b=copy.deepcopy(self.bundle);rows=b['interpretations']['published']['events'];rows[1]['id']=rows[0]['id']
        with self.assertRaises(ValueError):self.validate(b)
        b=copy.deepcopy(self.bundle);one=b['interpretations']['published']['events'][0]
        b['interpretations']['published']['events']=[{**one,'id':str(i)} for i in range(4001)]
        with self.assertRaises(ValueError):self.validate(b)

    def test_malformed_contour_is_controlled_validation_failure(self):
        for malformed in (None,7,'not-a-list'):
            b=copy.deepcopy(self.bundle);b['interpretations']['published']['events'][0]['contour_candidates']=malformed
            with self.subTest(malformed=malformed),self.assertRaises(ValueError):self.validate(b)

    def test_consent_is_literal_true_and_request_schema_exact(self):
        for value in (False,None,1,'true'):
            with self.subTest(value=value),self.assertRaises(ValueError):prepare_arrangement(self.library,self.request(accept_as_lead=value))
        missing=self.request();del missing['accept_as_lead']
        for value in (missing,self.request(extra=True),self.request(instrument='piano'),self.request(parent_job_id='../parent')):
            with self.assertRaises(ValueError):prepare_arrangement(self.library,value)

    def test_electric_candidate_selection_keeps_unknown_source_and_origin_before_planning(self):
        before=copy.deepcopy(self.result)
        prepared=prepare_arrangement(self.library,self.request(instrument='electric'))
        arrangement=arrange_review(prepared)
        self.assertEqual(prepared['spec']['instrument'],'electric')
        self.assertTrue(arrangement['renderable'])
        self.assertEqual(arrangement['render_origin_sample'],self.origin)
        self.assertEqual(arrangement['candidate_review']['source_part']['instrument'],'unknown')
        self.assertEqual(arrangement['candidate_review']['role_assignment'],
                         'User-selected lead; not instrument identification or analyzer truth')
        note=arrangement['notes'][0]
        source=self.bundle['interpretations']['published']['events'][0]
        self.assertEqual((note['id'],note['start'],note['end'],note['pitch'],note['velocity']),
                         ('common',source['start'],source['end'],source['pitch'],80))
        self.assertEqual(self.library.job(self.parent_id)['result'],before)

    def test_selection_unknown_duplicate_empty_and_cross_policy_ids(self):
        for ids in ([],['common','common'],['missing'],['o2'],[True]):
            with self.subTest(ids=ids),self.assertRaises(ValueError):prepare_arrangement(self.library,self.request(note_ids=ids))
        with self.assertRaises(ValueError):prepare_arrangement(self.library,self.request(interpretation='merged'))
        first=prepare_arrangement(self.library,self.request(note_ids=['p2','common']))
        second=prepare_arrangement(self.library,self.request(note_ids=['common','p2']))
        self.assertEqual(first['spec'],second['spec'])
        other=prepare_arrangement(self.library,self.request(interpretation='onset_only'))
        self.assertNotEqual(first['spec'],other['spec'])
        self.assertEqual(other['spec']['role_assignment'],'user-selected-lead')

    def test_arrange_preserves_parent_original_uncertainty_and_exclusions(self):
        before=self.library.job(self.parent_id)
        prepared=prepare_arrangement(self.library,self.request());arrangement=arrange_review(prepared)
        self.assertEqual(self.library.job(self.parent_id),before)
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(),self.original)
        source=arrangement['source']['notes'][0]
        expected=self.bundle['interpretations']['published']['events'][0]
        self.assertEqual((source['id'],source['start'],source['end'],source['pitch'],source['velocity'],source['confidence']),
                         (expected['id'],expected['start'],expected['end'],expected['pitch'],80,expected['mean_note_activation']))
        self.assertNotEqual(source['confidence'],1.)
        receipt=arrangement['candidate_review'];self.assertEqual(receipt['source_part']['instrument'],'unknown')
        self.assertEqual(receipt['unselected_note_ids'],['p2']);self.assertTrue(receipt['other_interpretation_retained_in_parent'])
        self.assertIn('uncalibrated',receipt['confidence_semantics']);self.assertEqual(arrangement['render_origin_sample'],44100)
        excluded={e['id'] for e in self.bundle['interpretations']['published']['events'] if e['id']!=expected['id']}
        self.assertEqual({a['note_id'] for a in arrangement['adaptations'] if a['kind']=='candidate-not-selected'},excluded)
        self.assertEqual([(n['start'],n['end']) for n in arrangement['notes']],[(expected['start'],expected['end'])])
        other=arrange_review(prepare_arrangement(self.library,self.request(interpretation='onset_only')))
        self.assertEqual(other['source']['notes'][0]['pitch'],65)

    def test_empty_fitted_plan_fails(self):
        prepared=prepare_arrangement(self.library,self.request())
        with patch('instrumental_preview.arrange_selected',return_value={'notes':[],'adaptations':[]}):
            with self.assertRaisesRegex(ValueError,'None of the selected'):arrange_review(prepared)

    def test_bass_selection_changes_renderer_identity_not_source_evidence(self):
        acoustic=prepare_arrangement(self.library,self.request())
        bass=prepare_arrangement(self.library,self.request(instrument='jazz-bass'))
        self.assertEqual(bass['spec']['bass_part'],'melody')
        self.assertNotEqual(acoustic['spec']['renderer_version'],bass['spec']['renderer_version'])
        self.assertEqual(acoustic['spec']['parent_result_sha256'],bass['spec']['parent_result_sha256'])
        result=arrange_review(bass)
        self.assertEqual(result['instrument'],'jazz-bass')
        self.assertEqual(result['source']['notes'][0]['pitch'],64)
        self.assertEqual(result['source']['notes'][0]['velocity'],80)
        self.assertEqual(result['candidate_review']['source_part']['instrument'],'unknown')

    def test_raw_receipt_path_hash_and_bytes_are_strict(self):
        for key,value in [('file','../activations.npz'),('sha256','g'*64),('bytes',True),('bytes',0)]:
            b=copy.deepcopy(self.bundle);b['raw_activations'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.validate(b)
        b=copy.deepcopy(self.bundle);b['interpretations']['published']['events'].reverse()
        with self.assertRaises(ValueError):self.validate(b)

    def test_pinned_model_pipeline_policy_and_frame_semantics(self):
        cases=[(('provenance','pipeline_version'),'different'),
               (('provenance','model_sha256'),'0'*64),
               (('interpretations','published','policy'),'duration_matched'),
               (('interpretations','published','settings','min_note_len'),0),
               (('interpretations','published','settings','infer_onsets'),1),
               (('interpretations','onset_only','version'),'changed'),
               (('interpretations','onset_only','model_sha256'),'0'*64),
               (('interpretations','onset_only','policy','gap_tolerance_frames'),11),
               (('interpretations','published','events',0,'start_frame'),3),
               (('interpretations','published','events',0,'end_frame_exclusive'),10),
               (('interpretations','published','source_clock','frame_samples',3),self.anchors[3]+1),
               (('interpretations','published','source_clock','source_coverage','uncovered_tail_samples'),999),
               (('raw_activations','frame_samples',3),self.anchors[3]+1),
               (('raw_activations','arrays','note','shape',1),87),
               (('raw_activations','arrays','contour','dtype'),'float64'),
               (('raw_activations','finite'),False),
               (('raw_activations','range'),[-1,1])]
        for path,value in cases:
            b=copy.deepcopy(self.bundle);target=b
            for key in path[:-1]:target=target[key]
            target[path[-1]]=value
            with self.subTest(path=path),self.assertRaises(ValueError):self.validate(b)

    def test_published_duration_twelve_frames_pass_eleven_fail(self):
        b=copy.deepcopy(self.bundle);event=b['interpretations']['published']['events'][0]
        event['end_frame_exclusive']=event['start_frame']+12
        event['end']=self.anchors[event['end_frame_exclusive']]
        self.assertEqual(self.validate(b)['interpretations']['published']['events'][0],event)
        event['end_frame_exclusive']=event['start_frame']+11
        event['end']=self.anchors[event['end_frame_exclusive']]
        with self.assertRaises(ValueError):self.validate(b)
        # The independently defined onset-only policy retains its short event.
        self.assertEqual(self.validate(self.bundle)['interpretations']['onset_only']['events'][0]['end_frame_exclusive'],9)

    def test_reconcile_preserves_shared_references_and_removes_only_verified_orphans(self):
        second=str(uuid.uuid4())
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                (second,'complete',1,json.dumps(self.parent_spec),json.dumps(self.result),None,time.time()))
        blob=b'valid but unpublished analysis result'
        orphan_ref={'sha256':hashlib.sha256(blob).hexdigest(),'bytes':len(blob),'format':'numpy-npz-v1'}
        stage=self.library.stage/'orphan.npz';stage.write_bytes(blob);self.store.publish(stage,orphan_ref)
        orphan=self.store.root/orphan_ref['sha256']
        unknown=self.store.root/'unknown-not-a-hash';unknown.write_bytes(b'keep')
        corrupted=self.store.root/('f'*64);corrupted.write_bytes(b'not matching its name')
        with self.library.connect() as db:db.execute('DELETE FROM jobs WHERE id=?',(self.parent_id,))
        with self.library.exclusive('worker.lock'):reconcile_evidence(self.library)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.store.verify(self.reference).read_bytes(),self.raw)
        self.assertEqual(unknown.read_bytes(),b'keep');self.assertEqual(corrupted.read_bytes(),b'not matching its name')
        with self.library.connect() as db:db.execute('DELETE FROM jobs WHERE id=?',(second,))
        with self.library.exclusive('worker.lock'):reconcile_evidence(self.library)
        self.assertFalse((self.store.root/self.reference['sha256']).exists())
        self.assertEqual(unknown.read_bytes(),b'keep');self.assertTrue(corrupted.exists())

    def test_hash_mismatch_parent_raw_and_original_are_rejected(self):
        b=copy.deepcopy(self.bundle);b['raw_activations']['sha256']='0'*64
        self.result['candidates']=b;self.save_parent()
        with self.assertRaises(ValueError):prepare_arrangement(self.library,self.request())
        self.result['candidates']=self.bundle;self.save_parent()
        path=self.store.verify(self.reference);path.write_bytes(b'x'*len(self.raw))
        with self.assertRaises(ValueError):prepare_arrangement(self.library,self.request())
        path.write_bytes(self.raw);(self.library.media/self.source['sha256']).write_bytes(b'changed')
        with self.assertRaises(ValueError):prepare_arrangement(self.library,self.request())

    def test_evidence_dedup_is_nonplayable_and_never_overwrites_bad_target(self):
        stage=self.library.stage/'duplicate.npz';stage.write_bytes(self.raw)
        self.assertEqual(self.store.publish(stage,self.reference),self.reference)
        with self.library.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0],1)
        path=self.store.verify(self.reference);self.assertEqual(path.read_bytes(),self.raw)
        path.write_bytes(b'x'*len(self.raw))
        with self.assertRaises(ValueError):self.store.publish(stage,self.reference)
        self.assertEqual(path.read_bytes(),b'x'*len(self.raw));self.assertEqual(stage.read_bytes(),self.raw)

    def test_evidence_rejects_outside_staging_wrong_size_and_cancellation(self):
        outside=Path(self.temp.name)/'outside';outside.write_bytes(self.raw)
        with self.assertRaises(ValueError):self.store.publish(outside,self.reference)
        for value in ({**self.reference,'bytes':True},{**self.reference,'format':'wav'}, {**self.reference,'sha256':'../file'}):
            with self.assertRaises(ValueError):validate_reference(value)
        stage=self.library.stage/'cancel.npz';stage.write_bytes(self.raw)
        with self.assertRaises(InterruptedError):self.store.publish(stage,self.reference,cancel=lambda:True)
        self.assertTrue(stage.exists());self.assertEqual(self.store.verify(self.reference).read_bytes(),self.raw)
        stage.write_bytes(b'too short')
        with self.assertRaises(ValueError):self.store.publish(stage,self.reference)

    def test_cleanup_targets_only_valid_owned_work_directory(self):
        identity=str(uuid.uuid4());directory=work_directory(self.library,identity);directory.mkdir();(directory/'file').write_bytes(b'partial')
        other=self.library.stage/'unrelated';other.write_bytes(b'keep')
        cleanup_work(self.library,identity);self.assertFalse(directory.exists());self.assertEqual(other.read_bytes(),b'keep')
        with self.assertRaises(ValueError):work_directory(self.library,'../other')


if __name__=='__main__':unittest.main()
