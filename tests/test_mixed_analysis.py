"""Silent source-time and candidate-accounting checks; no model imports."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import wave
import math
import copy
from types import SimpleNamespace
from unittest.mock import patch, Mock

from audio.mixed_analysis import read_passage, segment_frames, pitch_frames, interpret_frames, extract
from audio.performance import Performance


class MixedAnalysisTests(unittest.TestCase):
    def test_refactored_pitch_frames_preserve_all_rows_and_estimator_settings(self):
        # No actual numpy/librosa imports or inference are needed for this seam.
        vector=Mock();vector.astype.return_value=vector
        stem=Mock();stem.mean.return_value=vector
        frequencies=[440.,440.,440.,float('nan'),0.,440.]
        voiced=[True,False,True,True,True,True]
        probabilities=[.9,.8,.099,.5,.7,.1]
        estimator=Mock(return_value=(frequencies,voiced,probabilities))
        resampler=Mock(return_value='resampled')
        modules={'numpy':SimpleNamespace(float64=float,isfinite=math.isfinite),
                 'librosa':SimpleNamespace(pyin=estimator),
                 'scipy.signal':SimpleNamespace(resample_poly=resampler)}
        with patch.dict('sys.modules',modules):rows,report=pitch_frames(stem,'melody')
        self.assertEqual(len(rows),6)
        self.assertEqual([r['sample'] for r in rows],[1024+i*440 for i in range(6)])
        self.assertEqual([r['pitch'] for r in rows],[69.,None,None,None,None,69.])
        self.assertEqual([r['confidence'] for r in rows],probabilities)
        self.assertEqual(report['total_frames'],6)
        self.assertEqual(report['excluded_frames'],{'pyin-unvoiced':1,'below-unvalidated-probability-floor':3})
        estimator.assert_called_once_with('resampled',sr=22050,hop_length=220,center=False,
                                         fmin=70.,fmax=1200.,frame_length=1024)
        stem.mean.assert_called_once_with(axis=0);vector.astype.assert_called_once_with(float)
        resampler.assert_called_once_with(vector,1,2)

    def test_refactored_pitch_frames_reject_bad_probability_without_dropping_row(self):
        vector=Mock();vector.astype.return_value=vector;stem=Mock();stem.mean.return_value=vector
        for bad in (float('nan'),float('inf'),-.01,1.01):
            modules={'numpy':SimpleNamespace(float64=float,isfinite=math.isfinite),
                     'librosa':SimpleNamespace(pyin=lambda *a,**k:([440.],[True],[bad])),
                     'scipy.signal':SimpleNamespace(resample_poly=lambda *a:vector)}
            with self.subTest(probability=bad),patch.dict('sys.modules',modules),self.assertRaisesRegex(ValueError,'probability'):
                pitch_frames(stem,'melody')

    def test_extract_defaults_to_identical_baseline_interpretation_and_metadata(self):
        rows=[dict(sample=1024+i*440,pitch=p,confidence=.8) for i,p in enumerate([60.,60.,60.,None,62.,62.,62.,64.])]
        before=copy.deepcopy(rows);stem=SimpleNamespace(shape=(2,10000));origin=5336117
        expected,dropped=segment_frames(rows,'melody',origin,10000)
        frame_report=dict(total_frames=len(rows),excluded_frames={'pyin-unvoiced':1})
        with patch('audio.mixed_analysis.pitch_frames',return_value=(rows,frame_report)) as estimator:
            notes,report=extract(stem,'melody',origin)
        estimator.assert_called_once_with(stem,'melody')
        self.assertEqual(notes,expected);self.assertEqual(rows,before)
        self.assertEqual(report,dict(total_frames=8,excluded_frames={'pyin-unvoiced':1},
            minimum_group_frames=3,retained_notes=2,discarded_short_groups=dropped))
        self.assertTrue(all(n.velocity==80 for n in notes))
        self.assertNotIn('interpretation',report)
        self.assertEqual(sum(len(n.contour) for n in notes)+sum(d['frames'] for d in dropped),7)

    def test_opt_in_interpretation_measures_local_waveform_then_adds_origin_once(self):
        rows=[dict(sample=1024+i*440,pitch=None if i<4 else 60.,confidence=.8) for i in range(10)]
        before=copy.deepcopy(rows);origin=5336117
        samples=[.02]*6000
        for i in range(1684,len(samples)):samples[i]=.15
        class Stem:
            shape=(2,6000)
            def mean(self,axis):
                self_axis=axis
                if self_axis!=0:raise AssertionError('Wrong channel axis')
                return samples
        stem=Stem()
        notes,report=interpret_frames(rows,stem,'melody',origin,interpretation='attack-aware-v1')
        self.assertEqual(rows,before)
        self.assertEqual(len(notes),1);self.assertEqual(notes[0].start,origin+1684)
        self.assertEqual(notes[0].end,origin+1024+9*440+220)
        self.assertEqual(notes[0].velocity,80)
        self.assertEqual(notes[0].contour[0],(origin+1024+4*440,60.))
        audit=report['phrasing_audit']
        self.assertEqual(audit['unvoiced_frame_indexes'],[0,1,2,3])
        self.assertEqual(audit['events'][0]['raw_frame_indexes'],list(range(4,10)))
        self.assertEqual(audit['events'][0]['attack_association']['attack_sample'],origin+1904)
        with self.assertRaisesRegex(ValueError,'Unknown'):
            interpret_frames(rows,stem,'melody',origin,interpretation='automatic')

    def test_exact_selected_source_samples_and_readback_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'owned.wav'
            samples = b''.join(struct.pack('<hh', i % 30000, -i % 30000) for i in range(44100))
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
                wav.writeframes(samples)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            raw, info = read_passage(path, digest, .1, .4)
            self.assertEqual(raw, samples[4410*4:17640*4])
            self.assertEqual((info['origin_sample'], info['end_sample']), (4410, 17640))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with self.assertRaisesRegex(ValueError, 'verification'):
                read_passage(path, '0'*64, .1, .4)
            with self.assertRaisesRegex(ValueError, 'beyond'):
                read_passage(path, digest, .9, 1.2)

    def test_format_and_duration_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'owned.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0'*48000)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for start, end in ((float('nan'), 1), (0, float('inf')), (0, 31), (-1, 1), (1, 1), (True, 2)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    read_passage(path, digest, start, end)
            with self.assertRaisesRegex(ValueError, '44.1 kHz'):
                read_passage(path, digest, 0, 1)

    def test_unvoiced_gap_does_not_merge_notes_and_short_groups_are_disclosed(self):
        rows = [{'sample': 1024+i*440, 'pitch': pitch, 'confidence': .8}
                for i, pitch in enumerate([60.1, 60.2, 60.0, None, 60.1, 60.0, 60.1, 62.0, None])]
        notes, dropped = segment_frames(rows, 'melody', 44100, 10000)
        Performance('owned-source', 44100, notes).validate()
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(notes[0].start, 44100+1024-220)
        self.assertEqual(notes[0].contour[0][0], 44100+1024)
        self.assertLess(notes[0].end, notes[1].start)
        self.assertEqual(dropped[0]['pitch'], 62)
        self.assertEqual(dropped[0]['frames'], 1)
        self.assertEqual(sum(len(n.contour) for n in notes)+sum(d['frames'] for d in dropped), 7)


if __name__ == '__main__':
    unittest.main()
