import unittest
import numpy as np
from audio.basic_pitch import decode_events, frame_samples, official_frame_samples, analyze_pcm


def blank(n=400):
    return {k: np.zeros((n,w),np.float32) for k,w in [('note',88),('onset',88),('contour',264)]}


def decode(p, origin=0):
    return decode_events(p, source_rate=44100, source_origin_sample=origin, source_length=441000)


class BasicPitchEvidenceTests(unittest.TestCase):
    def test_overlap_and_repeated_attacks_independent(self):
        p=blank(); p['note'][20:100,39]=.8; p['note'][40:120,43]=.7
        p['onset'][[20,60],39]=.9; p['onset'][40,43]=.9
        e=decode(p)['events']
        self.assertEqual([(r['pitch'],r['start_frame'],r['end_frame_exclusive']) for r in e],
                         [(60,20,60),(64,40,120),(60,60,100)])
        self.assertEqual(len({r['id'] for r in e}),3)

    def test_no_gap_bridging_or_short_note_suppression(self):
        p=blank(); p['note'][10:11,39]=.8; p['note'][12:30,39]=.8
        p['onset'][10,39]=.9; p['onset'][12,39]=.9
        e=decode(p)['events']; self.assertEqual(len(e),2)
        self.assertLess(e[0]['end'],e[1]['start'])

    def test_no_invented_attack_from_note_energy(self):
        p=blank(); p['note'][10:100,39]=1
        self.assertEqual(decode(p)['events'],[])
        self.assertEqual(decode(p)['unassigned_active_frame_pitch_count'],90)
        p['onset'][110,39]=.8
        self.assertEqual(len(decode(p)['rejected_attacks']),1)

    def test_seam_anchor_and_late_source_origin(self):
        origin=44100*121+17; a=frame_samples(1000,44100,origin)
        self.assertTrue(np.all(np.diff(a)>0))
        self.assertEqual(a[142],origin+36164*2)
        self.assertEqual(a[141],origin+141*512)
        self.assertEqual(a[284],origin+2*36164*2)
        self.assertNotEqual(a[284],official_frame_samples(1000,44100,origin)[284])
        p=blank(); p['note'][140:145,39]=.8; p['onset'][140,39]=.9
        e=decode(p,origin)['events'][0]
        self.assertEqual((e['start'],e['end']),(a[140],a[145]))

    def test_contours_preserved_during_polyphony(self):
        p=blank(); p['note'][20:40,[39,43]]=.8; p['onset'][20,[39,43]]=.9
        p['contour'][20:40,39*3+1]=.9; p['contour'][20:40,43*3]=.8
        e=decode(p)['events']; self.assertEqual(len(e),2)
        self.assertAlmostEqual(e[0]['contour_candidates'][0]['midi'],60+1/3)
        self.assertEqual(len(e[1]['contour_candidates']),20)

    def test_reject_invalid_and_cancel_before_runtime(self):
        p=blank(); p['note'][1,1]=np.nan
        with self.assertRaises(ValueError): decode(p)
        with self.assertRaises(InterruptedError):
            analyze_pcm(np.zeros(100),44100,0,'unused',cancel=lambda:True)
        with self.assertRaises(ValueError):
            analyze_pcm(np.zeros((10,2)),44100,0,'unused')


if __name__=='__main__': unittest.main()
