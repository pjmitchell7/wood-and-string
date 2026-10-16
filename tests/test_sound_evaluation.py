"""Silent synthetic controls: evidence semantics, exact clocks and raw byte metrics."""
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio import sound_evaluation as s

CLOCK={'sample_rate':44100,'origin_sample':1000,'end_sample':2000}
WINDOW={'start':1100,'end':1500}
SHA='1'*64


def ref(identity='plan',kind='control_plan',clock=None):
    value=dict(id=identity,kind=kind,sha256=SHA,bytes=17)
    if clock is not None: value['clock']=deepcopy(clock)
    return value


def contract():
    return dict(version=s.VERSION,comparison_id='synthetic-transport',
        analysis_mode='retrospective_diagnostic',
        registration=dict(reference_id='plan',declared_before_observation=False,
                          scope='Test criteria; no chronology proof.'),
        clock=deepcopy(CLOCK),references=[ref()],expectations=[dict(
            id='tail',stage='dry_render',claim_scope='measured_signal',
            reference_basis='control_render',description='Old-tail residual is zero in the registered window.',
            metric='peak_residual',unit='linear_amplitude',window=deepcopy(WINDOW),
            criterion=dict(operator='equal',expected=0,absolute_tolerance=1e-7),
            reference_ids=['plan'],next_discriminating_check='Compare the isolated incoming note over the same window.')])


def observation(value=0):
    return dict(expectation_id='tail',stage='dry_render',metric='peak_residual',
        unit='linear_amplitude',window=deepcopy(WINDOW),clock=deepcopy(CLOCK),value=value,
        evidence_refs=[ref('measurement','measurement',CLOCK)],method='Retained comparison receipt.',
        uncertainty=['No perceptual onset or audibility measurement.'],proximate_failure=None,
        mechanism_hypotheses=[],alternatives=[])


def finding(c=None,o=None):
    return s.evaluate(s.freeze_contract(c or contract()),[o or observation()])['findings'][0]


def event(identity='held',start=1100,end=1800,**extra):
    return dict(id=identity,start=start,end=end,pitch=64,velocity=80)|extra


def compare(left,right,**extra):
    return s.compare_events(left,right,clock=CLOCK,reference_basis='synthetic_commands',
        tolerances=dict(start_samples=0,end_samples=0,pitch_semitones=0,velocity_units=0,
                        registration_sha256=SHA),**extra)


class ContractTests(unittest.TestCase):
    def test_freeze_is_detached_and_never_proves_chronology(self):
        c=contract();f=s.freeze_contract(c);c['expectations'][0]['description']='Changed later'
        self.assertNotEqual(c,f['contract']);self.assertIn('does not establish',f['chronology_limit'])
        f['contract']['comparison_id']='substitution'
        with self.assertRaises(ValueError): s.evaluate(f,[])
        c=contract();c['analysis_mode']='prospective_frozen'
        with self.assertRaises(ValueError): s.freeze_contract(c)
        c['registration']['declared_before_observation']=True
        self.assertIn('does not establish',s.freeze_contract(c)['chronology_limit'])

    def test_numeric_finding_retains_exact_binding_without_quality_score(self):
        c=contract();o=observation();before=deepcopy((c,o));result=s.evaluate(s.freeze_contract(c),[o])
        self.assertEqual(result['findings'][0]['status'],'supports')
        self.assertEqual(result['findings'][0]['observation_binding']['window'],WINDOW)
        self.assertFalse(result['listening_performed']);self.assertNotIn('grade',result)
        self.assertEqual((c,o),before)
        f=finding(o=observation(.25));self.assertEqual((f['status'],f['delta']),('violates',.25))

    def test_observation_metric_units_window_rate_and_stage_cannot_be_substituted(self):
        for key,value in [('metric','rms'),('unit','dB'),('window',{'start':1101,'end':1500}),
                          ('clock',{**CLOCK,'sample_rate':48000}),('stage','effects')]:
            with self.subTest(key=key):
                o=observation();o[key]=value
                self.assertEqual(finding(o=o)['status'],'inconclusive')

    def test_registered_and_observed_reference_clocks_must_cover_expected_window(self):
        for bad in ({**CLOCK,'sample_rate':48000},{**CLOCK,'origin_sample':1600}):
            c=contract();c['references'].append(ref('truth','annotation',bad))
            c['expectations'][0].update(reference_basis='human_annotation',claim_scope='source_transcription',reference_ids=['truth'])
            self.assertEqual(finding(c=c)['status'],'inconclusive')
            o=observation();o['evidence_refs'][0]['clock']=bad
            self.assertEqual(finding(o=o)['status'],'inconclusive')

    def test_missing_truth_estimated_notes_and_perception_never_become_accuracy(self):
        for basis,scope in [('unavailable','measured_signal'),('unavailable','arrangement_intent'),
                            ('estimated_source','source_transcription'),('synthetic_commands','source_transcription'),
                            ('human_annotation','source_transcription'),('control_render','perceptual')]:
            with self.subTest(basis=basis,scope=scope):
                c=contract();c['expectations'][0].update(reference_basis=basis,claim_scope=scope)
                self.assertEqual(finding(c=c)['status'],'inconclusive')
        self.assertEqual(s.evaluate(s.freeze_contract(contract()),[])['findings'][0]['status'],'inconclusive')
        self.assertEqual(finding(o=observation(None))['status'],'inconclusive')

    def test_human_annotation_can_support_only_declared_numeric_comparison(self):
        c=contract();c['references'].append(ref('truth','annotation',CLOCK))
        c['expectations'][0].update(reference_basis='human_annotation',claim_scope='source_transcription',reference_ids=['truth'])
        self.assertEqual(finding(c=c)['status'],'supports')

    def test_dry_wet_separation_and_report_only_capacity_observation(self):
        o=observation();o['evidence_refs'][0]['kind']='wet_render'
        self.assertEqual(finding(o=o)['status'],'inconclusive')
        c=contract();c['expectations'][0]['criterion']=dict(operator='report_only',expected=None,absolute_tolerance=0)
        o=observation(0);o['proximate_failure']=dict(statement='The late raw window contains exact zeros.',reference_ids=['measurement'])
        o['mechanism_hypotheses']=[dict(statement='A finite source recording may be exhausted; inspect source and loop/envelope configuration.',reference_ids=['plan'])]
        f=finding(c,o);self.assertEqual(f['status'],'inconclusive')
        self.assertNotIn('established_proximate_failure',f)
        self.assertEqual(f['reported_proximate_observation'],o['proximate_failure'])
        self.assertIn('author assertion',f['causal_limit'])

    def test_derived_overflow_is_inconclusive_and_json_stays_finite(self):
        for op,target,tol,value in [('equal',-1e308,0,1e308),('at_most',1e308,1e308,0),
                                    ('between',[-1e308,1e308],0,1e308)]:
            c=contract();c['expectations'][0]['criterion']=dict(operator=op,expected=target,absolute_tolerance=tol)
            f=finding(c,observation(value));self.assertEqual(f['status'],'inconclusive');self.assertIsNone(f['delta'])
            s.canonical(f)
        for value in (True,float('nan'),float('inf'),10**1000):
            with self.assertRaises(ValueError): finding(o=observation(value))

    def test_between_and_thresholds_use_registered_tolerance(self):
        for op,target,tol,value,status in [('between',[1,2],.1,2.05,'supports'),('between',[1,2],.1,2.2,'violates'),
                                          ('at_least',1,.1,.9,'supports'),('at_most',1,.1,1.2,'violates')]:
            c=contract();c['expectations'][0]['criterion']=dict(operator=op,expected=target,absolute_tolerance=tol)
            self.assertEqual(finding(c,observation(value))['status'],status)

    def test_evidence_identity_and_causal_references_are_not_silently_rebound(self):
        o=observation();o['evidence_refs']=[ref()];o['evidence_refs'][0]['sha256']='2'*64
        with self.assertRaises(ValueError): finding(o=o)
        o=observation();o['proximate_failure']=dict(statement='Unbound claim',reference_ids=['unknown'])
        with self.assertRaises(ValueError): finding(o=o)


class EventTests(unittest.TestCase):
    def test_fragmented_hold_and_intentional_reattack_are_distinct(self):
        fragmented=[event(end=1400),event('second',1450,1800)]
        rows=compare([event()],fragmented)['events']
        self.assertEqual([r['status'] for r in rows],['changed','extra'])
        self.assertEqual(rows[0]['deltas']['end_samples'],-400)
        self.assertEqual([r['status'] for r in compare(fragmented,fragmented)['events']],['preserved','preserved'])
        edits=[dict(operation='split',source_ids=['held'],result_ids=['held','second'],reason='Explicit user reattack edit.',registration_sha256=SHA)]
        rows=compare([event()],fragmented,edit_lineage=edits)['events']
        self.assertEqual([r['status'] for r in rows],['registered_edit_unassessed']*2)

    def test_explicit_omissions_do_not_hide_extra_ids_or_claim_recognizer_error(self):
        intent=[dict(note_id='held',reason='User chose to omit this source candidate.',registration_sha256=SHA)]
        result=compare([event()],[event('new')],omissions=intent)
        self.assertEqual([r['status'] for r in result['events']],['declared_omission','extra'])
        self.assertFalse(result['accuracy_claim'])
        with self.assertRaises(ValueError): compare([event()],[event()],omissions=intent)
        with self.assertRaises(ValueError): compare([event(),event()],[])

    def test_assignment_contour_and_metadata_changes_are_visible(self):
        before=event(role='melody',player=1,string=0,fret=4,articulation='sustain',phrase='a',confidence=.3,
                     contour=[[1100,64],[1800,64]])
        for key,value in [('role','bass'),('player',2),('string',1),('fret',5),('articulation','mute'),
                          ('phrase','b'),('confidence',.4),('contour',[[1100,64],[1800,64.1]])]:
            after={**before,key:value};row=compare([before],[after])['events'][0]
            self.assertEqual(row['status'],'changed');self.assertIn(key,row['changed_fields'])

    def test_contour_payload_bounds_shape_and_nonfinite_event_delta(self):
        for contour in ({'payload':'x'*300000},[[1000,64]],[[1500,64],[1400,64]],[[1100,float('inf')]],[[1100,64]]*4097):
            with self.assertRaises(ValueError): compare([event(contour=contour)],[])
        with self.assertRaises(ValueError): compare([event(pitch=-1e308)],[event(pitch=1e308)])
        with self.assertRaises(ValueError): compare([event(unknown='unsupported')],[])
        with self.assertRaises(ValueError): compare([],[],omissions=[{}]*4001)

    def test_first_changed_stage_is_not_a_mechanism_claim(self):
        snapshots=[dict(stage='source_observation',events=[event()],reference=ref('a','command_events',CLOCK)),
                   dict(stage='interpretation',events=[event()],reference=ref('b','estimated_events',CLOCK)),
                   dict(stage='arrangement',events=[event(end=1600)],reference=ref('c','command_events',CLOCK))]
        tolerances=dict(start_samples=0,end_samples=0,pitch_semitones=0,velocity_units=0,registration_sha256=SHA)
        result=s.trace_event_changes(snapshots,clock=CLOCK,tolerances=tolerances,reference_basis='synthetic_commands')
        self.assertEqual(result['first_observed_change_by_id'],{'held':'arrangement'})
        self.assertIn('unproven',result['causal_limit'])

    def test_stage_reference_clock_must_cover_comparison_clock(self):
        tolerances=dict(start_samples=0,end_samples=0,pitch_semitones=0,velocity_units=0,registration_sha256=SHA)
        for bad in ({'sample_rate':48000,'origin_sample':0,'end_sample':10},
                    {**CLOCK,'sample_rate':48000},{**CLOCK,'origin_sample':1100}):
            snapshots=[dict(stage='interpretation',events=[event()],reference=ref('a','estimated_events',bad)),
                       dict(stage='arrangement',events=[event()],reference=ref('b','command_events',CLOCK))]
            with self.assertRaises(ValueError):
                s.trace_event_changes(snapshots,clock=CLOCK,tolerances=tolerances,reference_basis='estimated_source')


class FloatMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'synthetic.f32'
        self.raw=struct.pack('<8f',0,0,1,-1,.5,0,0,0);self.path.write_bytes(self.raw)
        self.kw=dict(expected_sha256=hashlib.sha256(self.raw).hexdigest(),sample_rate=44100,
                     origin_sample=123456,frames=4,windows=[dict(id='all',start=123456,end=123460),
                     dict(id='middle',start=123457,end=123459),dict(id='silent',start=123459,end=123460)],
                     reference_id='raw',stage='dry_render')

    def test_stereo_energy_and_half_open_absolute_windows(self):
        result=s.measure_f32(self.path,**self.kw);all_,middle,zero=result['windows']
        self.assertEqual((all_['peak'],all_['sum_squares'],all_['nonzero_frames']),(1,2.25,2))
        self.assertEqual(all_['rms'],math.sqrt(2.25/8))
        self.assertEqual((all_['first_nonzero_sample'],all_['last_nonzero_sample']),(123457,123458))
        self.assertEqual((middle['frames'],middle['sum_squares'],middle['rms']),(2,2.25,.75))
        self.assertEqual((zero['peak'],zero['rms'],zero['nonzero_frames']),(0,0,0));self.assertIsNone(zero['first_nonzero_sample'])
        self.assertIn('not audibility',result['metric_scope']);self.assertFalse(result['listening_performed'])
        self.assertEqual(result['reference']['sha256'],hashlib.sha256(self.raw).hexdigest())

    def test_wrong_hash_size_nonfinite_and_bounds_reject(self):
        for patch_kw in (dict(expected_sha256='0'*64),dict(frames=5),dict(frames=s.MAX_BYTES//8+1),
                         dict(windows=[dict(id='bad',start=0,end=1)]),dict(stage='interpretation')):
            with self.subTest(patch_kw=patch_kw),self.assertRaises(ValueError): s.measure_f32(self.path,**{**self.kw,**patch_kw})
        self.path.write_bytes(struct.pack('<8f',float('nan'),0,0,0,0,0,0,0))
        with self.assertRaises(ValueError): s.measure_f32(self.path,**{**self.kw,'expected_sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()})

    def test_changed_final_handle_stat_rejects_even_same_read_hash(self):
        original=s.os.fstat;calls=[]
        def changed(fd):
            info=original(fd);calls.append(info)
            if len(calls)==1: return info
            return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino,st_size=info.st_size,st_mtime_ns=info.st_mtime_ns+1)
        with patch.object(s.os,'fstat',side_effect=changed),self.assertRaises(ValueError): s.measure_f32(self.path,**self.kw)


if __name__=='__main__': unittest.main()
