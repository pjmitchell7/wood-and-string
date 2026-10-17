"""Silent in-memory authored PCM; no assets, native engines or output files."""
import array
import ast
from copy import deepcopy
import itertools
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from audio import band_plan as p
from audio import band_mix as m

ORIGIN = 2**32+133


def provenance(ref='source:note', composed=False):
    return dict(source_refs=[ref] if ref else [],newly_composed=composed)


def plan(mode='electric',count=2,kit=False):
    instruments = ['acoustic','electric','bass'] if mode == 'ensemble' else [mode]*count
    parts = [dict(id='part-'+str(i+1),instrument=instrument,role='melody' if i == 0 else 'harmony',
        content_end_sample=ORIGIN+2,complete_end_sample=ORIGIN+4+i,
        effects=[],provenance=provenance('source:note-'+str(i+1))) for i,instrument in enumerate(instruments)]
    if kit:
        event = dict(id='hat-1',start=ORIGIN+1,end=ORIGIN+2,note=42,velocity=80)
        parts.append(dict(id='kit',instrument='drums',role='drums',content_end_sample=event['end'],
            complete_end_sample=event['start']+p.SUPPORT[42]+p.GUARD,effects=[],
            provenance=provenance('source:drums'),events=[event],event_provenance={'hat-1':provenance('source:hat-1')}))
    return dict(version=p.VERSION,source=dict(id='original-1',sha256='a'*64),revision='revision-1',
        mode=mode,sample_rate=p.RATE,origin_sample=ORIGIN,body_end_sample=ORIGIN+4,parts=parts)


def stems(value,patterns=None):
    patterns = patterns or {}
    result = {}
    for index,part in enumerate(value['parts']):
        frames = part['complete_end_sample']-ORIGIN
        values = array.array('d',[0.])* (frames*2)
        initial = patterns.get(part['id'],[.125/(index+1),-.0625/(index+1),.03125,0.])
        values[:len(initial)] = array.array('d',initial)
        result[part['id']] = m.float_stem(values,origin_sample=ORIGIN,effects=part['effects'])
    return result


def pcm(raw):
    return struct.unpack('<'+'h'*(len(raw)//2),raw)


def repin(bundle):
    for name,raw in bundle['parts'].items():
        bundle['receipt']['parts'][name] = m._file(raw,len(raw)//4)
    raw = bundle['master']
    bundle['receipt']['master'] = m._file(raw,len(raw)//4)
    bundle['receipt_sha256'] = p.sha256(p.canonical(bundle['receipt']))
    return bundle['receipt_sha256']


class PlanTests(unittest.TestCase):
    def test_all_declared_rosters_detached_and_no_automatic_events(self):
        for mode,count,kit in [('acoustic',1,False),('acoustic',2,False),('electric',1,False),
                               ('electric',2,True),('bass',1,True),('bass',2,False),('ensemble',3,True)]:
            value = plan(mode,count,kit)
            before = deepcopy(value)
            checked = p.validate_plan(value)
            self.assertEqual(value,before)
            checked['parts'][0]['provenance']['source_refs'].append('changed')
            self.assertEqual(value,before)
            request = p.drum_request(value)
            if kit:
                self.assertEqual(set(request),{'sample_rate','render_origin_sample','events'})
                self.assertEqual(request['events'],value['parts'][-1]['events'])
            else:
                self.assertIsNone(request)

    def test_roster_refusals_preserve_acoustic_and_exact_ensemble(self):
        bad = [plan('acoustic',2,True),plan('electric',3),plan('bass',3)]
        value = plan('ensemble')
        value['parts'][2]['instrument']='electric'
        bad.append(value)
        value = plan('electric',1,True)
        value['parts'].append(deepcopy(value['parts'][-1]))
        value['parts'][-1]['id']='other-kit'
        bad.append(value)
        value = plan()
        value['parts'][1]['id']=value['parts'][0]['id']
        bad.append(value)
        value = plan()
        value['parts'][0]['instrument']='bass'
        bad.append(value)
        for value in bad:
            with self.subTest(value=value['mode']),self.assertRaises(ValueError):
                p.validate_plan(value)

    def test_two_same_family_role_labels_must_be_distinct(self):
        for mode in ('acoustic','electric','bass'):
            for role in ('melody','Melody'):
                value=plan(mode)
                value['parts'][1]['role']=role
                with self.subTest(mode=mode,role=role),self.assertRaises(ValueError):
                    p.validate_plan(value)
        # Different instrument families may legitimately carry the same role.
        value=plan('ensemble')
        for part in value['parts']:part['role']='support'
        p.validate_plan(value)

    def test_complete_basic_support_and_exact_existing_renderer_catalog(self):
        source = Path(__file__).parents[1]/'audio/drum_render.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        assignments = {target.id:ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
            for target in node.targets if isinstance(target,ast.Name) and target.id in ('SUPPORT','MAP_SHA','VERSION')}
        self.assertEqual(assignments,dict(SUPPORT=p.SUPPORT,MAP_SHA=p.MAP_SHA256,VERSION=p.KIT_VERSION))
        for note in (36,38,42):
            value = plan('electric',1,True)
            kit = value['parts'][-1]
            kit['events'][0]['note']=note
            kit['complete_end_sample']=ORIGIN+1+p.SUPPORT[note]+p.GUARD
            self.assertEqual(p.validate_plan(value)['parts'][-1]['complete_end_sample'],kit['complete_end_sample'])
            kit['complete_end_sample']-=1
            with self.assertRaises(ValueError):p.validate_plan(value)
        value = plan('electric',1,True)
        value['body_end_sample']=ORIGIN+30*p.RATE
        event=value['parts'][-1]['events'][0]
        event.update(start=value['body_end_sample']-1,end=value['body_end_sample'],note=36)
        kit=value['parts'][-1]
        kit.update(content_end_sample=event['end'],complete_end_sample=event['start']+p.SUPPORT[36]+p.GUARD)
        self.assertGreater(kit['complete_end_sample']-ORIGIN,31*p.RATE)
        p.validate_plan(value)

    def test_exact_drum_order_off_retrigger_and_lineage(self):
        value=plan('electric',1,True)
        kit=value['parts'][-1]
        second=dict(id='hat-2',start=ORIGIN+2,end=ORIGIN+3,note=42,velocity=81)
        kit['events'].append(second)
        kit['event_provenance']['hat-2']=provenance(None,True)
        kit['provenance']['newly_composed']=True
        kit.update(content_end_sample=ORIGIN+3,complete_end_sample=second['start']+p.SUPPORT[42]+p.GUARD)
        self.assertEqual(p.drum_request(value)['events'][1],second)
        mutations=[lambda v:v['parts'][-1]['events'].reverse(),
                   lambda v:v['parts'][-1]['events'][1].update(start=ORIGIN+1),
                   lambda v:v['parts'][-1]['events'][0].update(note=44),
                   lambda v:v['parts'][-1]['events'][0].update(note=46),
                   lambda v:v['parts'][-1]['event_provenance'].pop('hat-1'),
                   lambda v:v['parts'][-1]['event_provenance'].update(extra=provenance()),
                   lambda v:v['parts'][-1]['provenance'].update(newly_composed=False)]
        for mutate in mutations:
            bad=deepcopy(value);mutate(bad)
            with self.assertRaises(ValueError):p.validate_plan(bad)

    def test_missing_lineage_bool_clocks_unknown_fields_and_shared_effects_refused(self):
        mutations=[lambda v:v.update(sample_rate=True),lambda v:v.update(origin_sample=1.0),
                   lambda v:v.update(body_end_sample=ORIGIN+30*p.RATE+1),
                   lambda v:v['source'].update(sha256='bad'),lambda v:v.update(extra=1),
                   lambda v:v['parts'][0]['provenance'].update(source_refs=[],newly_composed=False),
                   lambda v:v['parts'][0]['provenance'].update(newly_composed=1),
                   lambda v:v['parts'][0]['provenance'].update(source_refs=['C:/source.wav']),
                   lambda v:v['parts'][-1].update(effects=['guitar-overdrive'])]
        for mutate in mutations:
            value=plan('electric',1,True);mutate(value)
            with self.assertRaises(ValueError):p.validate_plan(value)
        with self.assertRaises(InterruptedError):p.validate_plan(plan(),cancel=lambda:True)


class MixTests(unittest.TestCase):
    def test_entire_roster_preflight_precedes_all_float_snapshots(self):
        value=plan()
        supplied=stems(value)
        self.assertEqual([s['frames']*16 for s in supplied.values()],[64,80])
        with patch.object(m,'_copy_samples',side_effect=AssertionError('snapshot taken')) as snapshot:
            with self.assertRaises(ValueError):m.compose(value,supplied,max_input_bytes=100)
            snapshot.assert_not_called()
        mutations=[lambda v:v['part-2'].update(origin_sample=ORIGIN+1),
                   lambda v:v['part-2'].update(frames=4),
                   lambda v:v['part-2'].update(gain_one_declared=1),
                   lambda v:v['part-2'].update(effects=['different-chain']),
                   lambda v:v['part-2'].update(sha256='bad'),
                   lambda v:v['part-2'].update(samples=array.array('h',[0]*10))]
        for mutate in mutations:
            bad=deepcopy(supplied);mutate(bad)
            with patch.object(m,'_copy_samples',side_effect=AssertionError('snapshot taken')) as snapshot:
                with self.assertRaises(ValueError):m.compose(value,bad)
                snapshot.assert_not_called()

    def test_huge_numeric_gain_and_headroom_refuse_consistently(self):
        value=plan(count=1);supplied=stems(value)
        bundle=m.compose(value,supplied)
        for huge in (10**1000,-10**1000):
            for options in ({'balance_gains':{'part-1':huge}},{'headroom_db':huge}):
                with self.subTest(options=options),self.assertRaises(ValueError):
                    m.compose(value,supplied,**options)
            for field in ('balance_gains','headroom_db','common_gain'):
                bad=deepcopy(bundle)
                if field=='balance_gains':bad['receipt'][field]['part-1']=huge
                else:bad['receipt'][field]=huge
                expected=repin(bad)
                with self.subTest(field=field,positive=huge>0),self.assertRaises(ValueError):
                    m.verify_mix(value,bad,expected_receipt_sha256=expected)

    def test_identical_unison_and_silent_pcm_do_not_claim_role_independence(self):
        value=plan()
        value['parts'][1].update(role='unison support',complete_end_sample=ORIGIN+4)
        for wave in ([.125,-.125],[0.,0.]):
            supplied=stems(value,{'part-1':wave,'part-2':wave})
            bundle=m.compose(value,supplied)
            self.assertEqual(bundle['parts']['part-1'],bundle['parts']['part-2'])
            self.assertEqual(bundle['receipt']['musical_role_independence'],'not_evaluated')
            self.assertEqual(pcm(bundle['master']),tuple(2*n for n in pcm(bundle['parts']['part-1'])))
            for assurance in (True,False,0,'verified','not evaluated'):
                bad=deepcopy(bundle)
                bad['receipt']['musical_role_independence']=assurance
                with self.assertRaises(ValueError):
                    m.verify_mix(value,bad,expected_receipt_sha256=repin(bad))
            bad=deepcopy(bundle)
            del bad['receipt']['musical_role_independence']
            with self.assertRaises(ValueError):
                m.verify_mix(value,bad,expected_receipt_sha256=repin(bad))

    def test_absolute_clock_complete_tails_zero_padding_and_input_preservation(self):
        value=plan('ensemble')
        supplied=stems(value)
        before=deepcopy((value,supplied))
        bundle=m.compose(value,supplied)
        receipt=bundle['receipt']
        self.assertEqual(receipt['clock'],dict(sample_rate=p.RATE,origin_sample=ORIGIN,body_frames=4,tail_frames=2,frames=6))
        self.assertEqual(bundle['parts']['part-1'][-8:],bytes(8))
        self.assertEqual(bundle['parts']['part-2'][-4:],bytes(4))
        self.assertEqual((value,supplied),before)
        checked=m.verify_mix(value,bundle,expected_receipt_sha256=bundle['receipt_sha256'])
        checked['default_active'].clear()
        self.assertEqual(len(bundle['receipt']['default_active']),3)

    def test_one_persistent_kit_plan_survives_full_support_padding(self):
        value=plan('bass',1,True)
        bundle=m.compose(value,stems(value),default_active=['part-1'])
        self.assertEqual(list(bundle['parts']),['part-1','kit'])
        self.assertEqual(bundle['receipt']['clock']['frames'],value['parts'][-1]['complete_end_sample']-ORIGIN)
        self.assertEqual(pcm(bundle['master']),pcm(bundle['parts']['part-1']))
        self.assertEqual(bundle['receipt']['default_active'],['part-1'])
        self.assertIn('Caller-declared',bundle['receipt']['scope'])

    def test_common_gain_protects_all_subsets_even_when_default_cancels(self):
        value=plan()
        patterns={'part-1':[2.,-2.,1.,-1.],'part-2':[-2.,2.,-1.,1.]}
        bundle=m.compose(value,stems(value,patterns))
        self.assertLess(bundle['receipt']['common_gain'],1)
        self.assertEqual(set(pcm(bundle['master'])),{0})
        tracks={name:pcm(raw) for name,raw in bundle['parts'].items()}
        for mask in itertools.product((False,True),repeat=2):
            mixed=[sum(tracks[name][i] for name,on in zip(tracks,mask) if on) for i in range(10)]
            self.assertTrue(all(-32768<=v<=32767 for v in mixed))
        self.assertTrue(bundle['receipt']['proof']['all_mute_subsets_and_gain_reductions_bounded'])

    def test_recorded_balance_is_not_independent_normalization(self):
        value=plan()
        supplied=stems(value,{'part-1':[.25,-.125],'part-2':[.03125,-.015625]})
        bundle=m.compose(value,supplied,balance_gains={'part-1':1.,'part-2':2.})
        self.assertEqual(bundle['receipt']['common_gain'],1.)
        self.assertEqual(bundle['receipt']['balance_gains'],{'part-1':1.,'part-2':2.})
        self.assertEqual(pcm(bundle['parts']['part-1'])[0],4*pcm(bundle['parts']['part-2'])[0])
        self.assertEqual(pcm(bundle['master'])[0],sum(pcm(raw)[0] for raw in bundle['parts'].values()))

    def test_nearest_even_quantization_exact_default_sum_and_all_muted(self):
        value=plan(count=1)
        numbers=[.5,1.5,2.5,-.5,-1.5,-2.5]
        supplied=stems(value,{'part-1':[v/32768 for v in numbers]})
        bundle=m.compose(value,supplied)
        self.assertEqual(pcm(bundle['master'])[:6],(0,2,2,0,-2,-2))
        muted=m.compose(value,supplied,default_active=[])
        self.assertEqual(muted['master'],bytes(len(muted['master'])))
        self.assertEqual(muted['parts'],bundle['parts'])
        self.assertEqual(muted['receipt']['common_gain'],bundle['receipt']['common_gain'])

    def test_float_input_tamper_types_clock_tail_hash_and_effects(self):
        value=plan(count=1)
        supplied=stems(value)
        mutations=[lambda v:v['part-1']['samples'].__setitem__(0,.9),
                   lambda v:v['part-1'].update(sha256='0'*64),
                   lambda v:v['part-1'].update(origin_sample=ORIGIN+1),
                   lambda v:v['part-1'].update(frames=3),
                   lambda v:v['part-1'].update(gain_one_declared=1),
                   lambda v:v['part-1'].update(complete_tail_declared=False),
                   lambda v:v['part-1'].update(effects=['overdrive']),
                   lambda v:v['part-1'].update(sample_rate=48000)]
        for mutate in mutations:
            bad=deepcopy(supplied);mutate(bad)
            with self.assertRaises(ValueError):m.compose(value,bad)
        for numbers in ([True,0],[float('nan'),0],[float('inf'),0],[1,2,3],['bad',0]):
            with self.assertRaises(ValueError):m.float_stem(numbers,origin_sample=ORIGIN)
        value['parts'][0]['effects']=['electric-tone-v1']
        effected=stems(value)
        result=m.compose(value,effected)
        self.assertEqual(result['receipt']['input_declarations']['part-1']['effects'],['electric-tone-v1'])

    def test_caps_roster_gain_and_active_selection_refuse_before_copy(self):
        value=plan(count=1);supplied=stems(value)
        for options in ({'max_frames':1},{'max_output_bytes':4},{'max_input_bytes':16}):
            with patch.object(m,'_copy_samples',side_effect=AssertionError('copied')),self.assertRaises(ValueError):
                m.compose(value,supplied,**options)
        for options in ({'balance_gains':{}},{'balance_gains':{'part-1':True}},
                        {'balance_gains':{'part-1':float('nan')}},{'balance_gains':{'part-1':-1}},
                        {'default_active':['foreign']},{'default_active':['part-1','part-1']},
                        {'headroom_db':True},{'headroom_db':1}):
            with self.assertRaises(ValueError):m.compose(value,supplied,**options)
        with self.assertRaises(ValueError):m.compose(value,{})
        with self.assertRaises(ValueError):m.compose(value,{**supplied,'foreign':supplied['part-1']})

    def test_cancellation_and_nonfinite_balanced_input_leave_originals(self):
        value=plan();supplied=stems(value)
        before=deepcopy((value,supplied))
        calls=[0]
        def cancel():
            calls[0]+=1
            return calls[0]>15
        with self.assertRaises(InterruptedError):m.compose(value,supplied,cancel=cancel)
        self.assertEqual((value,supplied),before)
        supplied=stems(value,{'part-1':[1e308,0.],'part-2':[1e308,0.]})
        with self.assertRaises(ValueError):m.compose(value,supplied)

    def test_saved_bytes_receipt_plan_identity_and_exact_sum_tamper(self):
        value=plan();bundle=m.compose(value,stems(value))
        expected=bundle['receipt_sha256']
        for target in ('master','part','receipt','plan'):
            bad=deepcopy(bundle);other=deepcopy(value)
            if target=='master':bad['master']=b'\1\0'+bad['master'][2:]
            if target=='part':bad['parts']['part-1']=b'\1\0'+bad['parts']['part-1'][2:]
            if target=='receipt':bad['receipt']['common_gain']=.1
            if target=='plan':other['revision']='other-revision'
            with self.assertRaises(ValueError):m.verify_mix(other,bad,expected_receipt_sha256=expected)
        bad=deepcopy(bundle)
        values=list(pcm(bad['master']));values[0]+=1
        bad['master']=struct.pack('<'+'h'*len(values),*values)
        forged=repin(bad)
        with self.assertRaises(ValueError):m.verify_mix(value,bad,expected_receipt_sha256=forged)

    def test_verifier_checks_padding_and_subsets_even_with_rehashed_artifact(self):
        value=plan();bundle=m.compose(value,stems(value),default_active=['part-1'])
        bad=deepcopy(bundle)
        samples=list(pcm(bad['parts']['part-1']));samples[-1]=1
        bad['parts']['part-1']=struct.pack('<'+'h'*len(samples),*samples)
        with self.assertRaises(ValueError):m.verify_mix(value,bad,expected_receipt_sha256=repin(bad))
        bad=deepcopy(bundle)
        for name in bad['parts']:
            samples=[0]*len(pcm(bad['parts'][name]));samples[0]=20000
            bad['parts'][name]=struct.pack('<'+'h'*len(samples),*samples)
        bad['master']=bad['parts']['part-1']
        with self.assertRaises(ValueError):m.verify_mix(value,bad,expected_receipt_sha256=repin(bad))


if __name__ == '__main__':
    unittest.main()
