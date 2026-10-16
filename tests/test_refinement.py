"""Pure manual revision tests: no models, rendering, or audio playback."""
import copy
from dataclasses import asdict, replace
import unittest

from audio.performance import Note, Performance, arrange
from audio.refinement import refine, normalize_operations, performance_from_dict


class Refinement(unittest.TestCase):
    def setUp(self):
        self.source = Performance('full-original-hash', 44100, [
            Note('a', 1000, 1100, 64, 70, confidence=.8, contour=((1000,64.1),)),
            Note('b', 1140, 1300, 64, 85, confidence=.3, contour=((1150,63.9),)),
            Note('bass', 1000, 1400, 40, role='bass'),
            Note('c', 1400, 1500, 67)], analysis_offset_samples=1000,
            beats=[{'sample':1000, 'confidence':.7}])

    def run_ops(self, ops, source=None):
        return refine(source or self.source, ops, passage_start=1000, passage_end=2000)

    def test_join_preserves_source_contours_and_audits_gap(self):
        before = copy.deepcopy(self.source)
        result, ledger = self.run_ops([{'kind':'join','note_ids':['b','a']}])
        self.assertEqual(self.source, before)
        joined = next(n for n in result.notes if n.id.startswith('joined-'))
        self.assertEqual((joined.start,joined.end,joined.pitch,joined.velocity,joined.confidence), (1000,1300,64,70,.3))
        self.assertEqual(joined.contour, ((1000,64.1),(1150,63.9)))
        self.assertEqual(ledger['changes'][0]['unobserved_gaps'], [{'start':1100,'end':1140}])
        self.assertEqual(ledger['changes'][0]['members'], [asdict(n) for n in self.source.notes[:2]])
        self.assertEqual(result.analysis_offset_samples, 1000)
        for ident in ('bass','c'):
            self.assertEqual(next(n for n in result.notes if n.id==ident), next(n for n in self.source.notes if n.id==ident))
        result.beats[0]['sample']=99
        self.assertEqual(self.source.beats[0]['sample'],1000)
        arrange(result).get('adaptations')  # corrected interpretation still satisfies planner contract

    def test_deterministic_identity_and_normalization(self):
        ops=[{'kind':'join','note_ids':['b','a']},{'kind':'omit','note_ids':['c']}]
        rev=[{'kind':'omit','note_ids':['c']},{'kind':'join','note_ids':['a','b']}]
        self.assertEqual(self.run_ops(ops), self.run_ops(rev))
        changed=copy.deepcopy(self.source);changed.notes[0]=replace(changed.notes[0],confidence=.7)
        a,_=self.run_ops(ops);b,_=self.run_ops(ops,changed)
        self.assertNotEqual(a.notes[0].id,b.notes[0].id)

    def test_omit_and_empty_identity(self):
        result,ledger=self.run_ops([{'kind':'omit','note_ids':['a','b','bass','c']}])
        self.assertEqual(result.notes,[])
        self.assertEqual(len(ledger['changes'][0]['members']),4)
        untouched,_=self.run_ops([])
        self.assertEqual(untouched,self.source);self.assertIsNot(untouched,self.source)

    def test_reject_invalid_operations(self):
        bad=[None,{},[{}],[{'kind':'join','note_ids':['a']}],
             [{'kind':'omit','note_ids':[]}], [{'kind':'omit','note_ids':['a','a']}],
             [{'kind':'omit','note_ids':['a']},{'kind':'omit','note_ids':['a']}],
             [{'kind':'join','note_ids':['a','missing']}],
             [{'kind':'omit','note_ids':[True]}],
             [{'kind':'omit','note_ids':['a'],'extra':1}],
             [{'kind':'other','note_ids':['a']}],
             [{'kind':'omit','note_ids':[str(i)]} for i in range(129)]]
        for ops in bad:
            with self.subTest(ops=ops),self.assertRaises(ValueError):self.run_ops(ops)

    def test_join_rejects_pitch_role_overlap_and_intervening_even_if_omitted(self):
        for alteration in (replace(self.source.notes[1],pitch=65),
                           replace(self.source.notes[1],role='bass'),
                           replace(self.source.notes[1],start=1090)):
            source=copy.deepcopy(self.source);source.notes[1]=alteration
            with self.assertRaises(ValueError):self.run_ops([{'kind':'join','note_ids':['a','b']}],source)
        for start,end in ((1110,1130),(900,1050),(1250,1350)):
            source=copy.deepcopy(self.source)
            source.notes.append(Note('intervening',max(start,1000),end,70))
            with self.assertRaises(ValueError):
                self.run_ops([{'kind':'join','note_ids':['a','b']},{'kind':'omit','note_ids':['intervening']}],source)

    def test_touching_notes_have_no_gap_and_other_role_is_allowed(self):
        source=copy.deepcopy(self.source);source.notes[1]=replace(source.notes[1],start=1100)
        _,ledger=self.run_ops([{'kind':'join','note_ids':['a','b']}],source)
        self.assertEqual(ledger['changes'][0]['unobserved_gaps'],[])

    def test_serialized_roundtrip_and_strict_types(self):
        self.assertEqual(performance_from_dict(asdict(self.source)),self.source)
        for key,value in [('sample_rate',True),('version',True),('analysis_offset_samples',-1),('source_id',''),('beats',[float('nan')]),('extra',1)]:
            data=asdict(self.source);data[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):performance_from_dict(data)
        for key,value in [('start',True),('confidence',True),('confidence',float('inf')),('role',[]),('phrase',None),('contour',[(True,64)]),('contour',[(1000,float('nan'))]),('contour',[(1050,64),(1000,64)]),('extra',1)]:
            data=asdict(self.source);data['notes'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):performance_from_dict(data)

    def test_bounds_are_absolute_and_fail_before_changes(self):
        for start,end in ((-1,2000),(True,2000),(1000,1200),(1000,1000)):
            with self.assertRaises(ValueError):refine(self.source,[],passage_start=start,passage_end=end)
        source=copy.deepcopy(self.source);source.notes.append(replace(source.notes[0]))
        with self.assertRaises(ValueError):self.run_ops([],source)

    def test_nonzero_viewport_is_independent_of_analysis_offset(self):
        source=Performance('actual-style',44100,[Note('a',11025,12000,64),
            Note('b',12100,13000,64)],analysis_offset_samples=0)
        result,ledger=refine(source,[{'kind':'join','note_ids':['a','b']}],
                             passage_start=11025,passage_end=275625)
        self.assertEqual(result.analysis_offset_samples,0)
        self.assertEqual((result.notes[0].start,result.notes[0].end),(11025,13000))
        self.assertEqual(ledger['passage_start'],11025)
        self.assertEqual(source.notes[0].end,12000)


if __name__=='__main__':unittest.main()
