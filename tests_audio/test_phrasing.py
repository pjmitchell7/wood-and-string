import math
import unittest
from audio.phrasing import energy_evidence, segment, segment_with_attack_association


def report(pitches, energies=None, attacks=(), origin=0):
    return {'role': 'melody', 'hop_samples': 10, 'input': {'frames': origin+len(pitches)*10+10},
        'frames': [{'sample': origin+10+i*10, 'pitch': p, 'confidence': .9,
                    'rms': energies[i] if energies else .1, 'attack': i in attacks}
                   for i,p in enumerate(pitches)]}


class PhrasingTests(unittest.TestCase):
    def test_same_frames_production_vibrato_fragmentation_and_genuine_changes(self):
        from audio.mixed_analysis import segment_frames
        pitches=[60+.6*math.sin(i*math.pi/4) for i in range(80)]
        rows=[dict(sample=1024+i*440,pitch=p,confidence=.9,rms=.1,attack=False) for i,p in enumerate(pitches)]
        baseline,dropped=segment_frames(rows,'melody',0,40000)
        data=dict(role='melody',hop_samples=440,input=dict(frames=40000),frames=rows)
        candidate,_=segment_with_attack_association(data)
        self.assertEqual(len(candidate),1)
        self.assertTrue(len(baseline)>1 or len(dropped)>0)
        for i,r in enumerate(rows):r['pitch']=60 if i<40 else 62
        candidate,_=segment_with_attack_association(data)
        self.assertEqual([n.pitch for n in candidate],[60,62])
        self.assertEqual(candidate[1].start,1024+40*440-220)
    def test_early_unvoiced_attack_association_retains_ends_contour_velocity(self):
        data=report([None]*5+[60]*10,attacks=(2,),origin=5336117)
        data['analysis_offset_samples']=5336117
        old,_=segment(data);new,audit=segment_with_attack_association(data)
        self.assertEqual(new[0].start,5336117+25)
        self.assertEqual((new[0].end,new[0].contour),(old[0].end,old[0].contour))
        self.assertEqual(new[0].velocity,80)
        self.assertEqual(audit['events'][0]['attack_association']['pitch_unobserved_interval'],[5336142,5336172])

    def test_association_does_not_cross_voiced_pitch_or_missing_frames(self):
        data=report([62]*3+[None]*2+[60]*5,attacks=(1,))
        old,_=segment(data);new,_=segment_with_attack_association(data)
        self.assertEqual([n.start for n in new],[n.start for n in old])
        data=report([None]*5+[60]*5,attacks=(2,));data['frames'][3]['sample']+=1
        old,_=segment(data);new,_=segment_with_attack_association(data)
        self.assertEqual(new[0].start,old[0].start)

    def test_rest_and_dropout_remain_ambiguous_not_filled(self):
        data=report([60]*8+[None]*5+[60]*8,attacks=(11,))
        notes,audit=segment_with_attack_association(data)
        self.assertEqual(len(notes),2)
        self.assertLess(notes[0].end,notes[1].start)
        self.assertIn('dropout',audit['events'][1]['resume_uncertainty'])

    def test_unstable_short_pitch_not_backfilled_and_no_attack_no_change(self):
        data=report([None]*4+[60,60,None,62,62,62],attacks=(2,))
        old,_=segment(data);new,_=segment_with_attack_association(data)
        self.assertEqual([n.start for n in new],[n.start for n in old])
        data=report([None]*4+[60]*8)
        old,_=segment(data);new,_=segment_with_attack_association(data)
        self.assertEqual([(n.start,n.end,n.pitch) for n in new],[(n.start,n.end,n.pitch) for n in old])

    def test_unrelated_attack_separated_by_energy_silence_is_not_associated(self):
        data=report([None]*6+[60]*8,energies=[.1,.4,.4,0,0,.1]+[.1]*8,attacks=(1,))
        old,_=segment(data);new,audit=segment_with_attack_association(data)
        self.assertEqual(new[0].start,old[0].start)
        self.assertIsNone(audit['events'][0]['attack_association'])

    def test_noisy_consonant_leadin_is_only_uncertain_association(self):
        data=report([None]*4+[60]*8,attacks=(1,))
        _,audit=segment_with_attack_association(data)
        association=audit['events'][0]['attack_association']
        self.assertIn('uncertain',association['evidence'])
        self.assertIn('not certified',audit['events'][0]['onset_status'])

    def test_close_real_note_and_rest_cannot_be_overwritten(self):
        data=report([60]*4+[None]*2+[62]*6,attacks=(3,))
        old,_=segment(data);new,_=segment_with_attack_association(data)
        self.assertEqual([(n.start,n.end) for n in new],[(n.start,n.end) for n in old])
    def test_sustained_vibrato_does_not_become_repeated_semitones(self):
        data = report([60+.6*math.sin(i*math.pi/4) for i in range(80)])
        notes, audit = segment(data)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].contour, tuple((r['sample'],r['pitch']) for r in data['frames']))
        self.assertTrue(audit['decisions'])

    def test_deliberate_fast_trill_and_change_timing_survive(self):
        notes, audit = segment(report([p for p in [60,61]*8 for _ in range(2)], origin=9000))
        self.assertEqual([n.pitch for n in notes], [60,61]*8)
        self.assertEqual([n.start for n in notes], [9005+20*i for i in range(16)])
        self.assertTrue(all(n.end-n.start == 20 for n in notes))

    def test_repeated_same_pitch_attack_vs_legato(self):
        legato, _ = segment(report([60]*40))
        attacks, audit = segment(report([60]*40, attacks=(12,25)))
        self.assertEqual(len(legato), 1)
        self.assertEqual(len(attacks), 3)
        self.assertEqual([n.start for n in attacks], [5,125,255])
        self.assertEqual(sum(d['kind']=='reattack' for d in audit['decisions']), 2)

    def test_breath_rests_are_never_extended_and_dynamics_are_measured(self):
        notes, audit = segment(report([60]*10+[None]*5+[60]*10,
                                      [.02]*10+[0]*5+[.1]*10))
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[1].start-notes[0].end, 50)
        self.assertLess(notes[0].velocity, notes[1].velocity)
        self.assertEqual(notes[1].velocity, 85)
        self.assertEqual(len(audit['unvoiced_frame_indexes']), 5)

    def test_waveform_energy_attack_detector_and_slow_crescendo(self):
        frames = report([60]*40)['frames']
        # Piecewise envelope; each frame RMS window samples only its own plateau.
        samples = [0.]*420
        for i,row in enumerate(frames):
            amplitude = .02 if i < 12 or 22 <= i < 28 else .15
            for j in range(row['sample']-5,row['sample']+5): samples[j] = amplitude*(-1)**j
        measured = energy_evidence(samples, frames, 10)
        self.assertEqual([i for i,r in enumerate(measured) if r['attack']], [12,28])
        slow = [float(i)/10000 for i in range(420)]
        self.assertFalse(any(r['attack'] for r in energy_evidence(slow, frames, 10)))

    def test_every_voiced_frame_accounted_without_duplicate_or_hidden_discard(self):
        data = report([60,60,61,60,None,62,62,62])
        notes,audit = segment(data)
        indexes = [i for event in audit['events'] for i in event['raw_frame_indexes']]
        self.assertEqual(indexes, [0,1,2,3,5,6,7])
        self.assertEqual(len(indexes), len(set(indexes)))
        self.assertTrue(any(d['sample']==30 for d in audit['decisions']))


if __name__ == '__main__': unittest.main()
