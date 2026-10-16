import copy
import json
import unittest
from audio.reference_notes import score_events


def note(ident,start,end,pitch=60,**kwargs):
    return dict(id=ident,start=start,end=end,pitch=pitch,**kwargs)


class ReferenceMetrics(unittest.TestCase):
    def score(self,p,r,**kwargs):
        return score_events(p,r,**({'start_sample':0,'end_sample':20000,'sample_rate':10000}|kwargs))

    def test_augmenting_path_beats_greedy(self):
        # First prediction can take either reference; second can only take A.
        r=[note('a',1000,2000),note('b',1600,2200)]
        p=[note('p',1200,2000),note('q',1250,2000,59)]
        r[0]['pitch']=59.5
        result=self.score(p,r)['onset_50ms']
        self.assertEqual(result['matched'],2)
        self.assertEqual({(x['event_id'],x['reference_id']) for x in result['pairs']},{('p','b'),('q','a')})

    def test_inclusive_pitch_onset_and_duration_tolerances(self):
        r=[note('r',1000,6000,60.5,confidence=None)]
        # 20ms onset boundary; 20% duration offset boundary.
        result=self.score([note('p',1200,7000)],r)
        self.assertEqual(result['onset_20ms']['matched'],1)
        self.assertEqual(result['full_note_50ms']['matched'],1)
        self.assertEqual(self.score([note('p',1201,7001)],r)['onset_20ms']['matched'],0)
        self.assertEqual(self.score([note('p',1200,7001)],r)['full_note_50ms']['matched'],0)
        r[0]['pitch']=60.50001
        self.assertEqual(self.score([note('p',1200,7000)],r)['onset_50ms']['matched'],0)

    def test_censoring_keeps_occupancy_and_reference_metadata(self):
        r=[note('left',0,1500,confidence=None),note('right',2000,4000,confidence='annotated'),note('outside',5000,6000)]
        p=[note('l',0,1500),note('p',2000,4000)]
        before=copy.deepcopy(r)
        out=self.score(p,r,start_sample=1000,end_sample=3000)
        self.assertEqual(out['left_censored_reference_ids'],['left'])
        self.assertEqual(out['right_censored_reference_ids'],['right'])
        self.assertEqual(out['onset_50ms']['matched'],1)
        self.assertEqual(out['full_note_50ms']['reference_count'],0)
        self.assertEqual(out['full_note_50ms']['unmatched_to_reference'],1)
        self.assertEqual(out['occupancy']['totals_pitch_samples']['intersection_samples'],1500)
        self.assertEqual(r,before);self.assertIsNone(out['references'][0]['confidence'])
        json.dumps(out,allow_nan=False)

    def test_union_rest_and_duplicate_unisons_are_separate(self):
        p=[note('a',1000,3000),note('b',2000,4000)]
        r=[note('r',1000,2000),note('s',3000,4000),note('unison',1000,2000)]
        out=self.score(p,r)['occupancy']['totals_pitch_samples']
        self.assertEqual(out,dict(event_union_samples=3000,reference_union_samples=2000,
            intersection_samples=2000,missing_samples=0,outside_annotation_samples=1000,
            event_duplicate_overlap_samples=1000,reference_duplicate_overlap_samples=1000))

    def test_iou_errors_and_unmatched_label(self):
        out=self.score([note('p',1200,2400),note('otherinstrument',3000,4000,72)], [note('r',1000,2000)])
        pair=out['onset_50ms']['pairs'][0]
        self.assertEqual(pair['onset_error_samples'],200)
        self.assertEqual(pair['offset_error_samples'],400)
        self.assertAlmostEqual(pair['interval_iou'],800/1400)
        self.assertEqual(out['onset_50ms']['unmatched_event_ids'],['otherinstrument'])

    def test_fractional_pitch_binning_is_declared_not_match_equivalence(self):
        out=self.score([note('p',0,1000,60)],[note('r',0,1000,60.5)])
        self.assertEqual(out['onset_50ms']['matched'],1)
        self.assertEqual(out['occupancy']['totals_pitch_samples']['intersection_samples'],0)
        self.assertIn('distinct',out['occupancy']['pitch_bin_policy'])

    def test_validation_and_empty(self):
        self.assertEqual(self.score([],[])['onset_50ms']['matched'],0)
        for rows in ([note('a',0,100),note('a',100,200)], [note('a',True,100)],
                     [note('a',0,100,float('nan'))],[note('a',0,100,60.1)],
                     [note('a',0,100,confidence=float('inf'))], [note(str(i),0,10) for i in range(1001)]):
            with self.subTest(rows=rows[:2]),self.assertRaises(ValueError):self.score(rows,[])
        with self.assertRaises(ValueError):self.score([],[],sample_rate=True)


if __name__=='__main__':unittest.main()
