"""Silent synthetic DSP checks; no files, renderer, native process or listening."""
import array
import copy
import hashlib
import json
import math
import unittest
from unittest.mock import patch

from audio import electric_tone as tone


def stereo(frames, *, kind='d'):
    return array.array(kind, (value for pair in frames for value in pair))


def direct(samples, cutoff, length, *, highpass=False):
    """Independent, deliberately slow direct finite convolution of tiny input."""
    a = math.exp(-2*math.pi*cutoff/tone.SAMPLE_RATE)
    weights = [(1-a)*a**k/(1-a**length) for k in range(length)]
    if highpass:
        weights = [-value for value in weights]
        weights[0] += 1
    result = [0.]*(len(samples)+2*(length-1))
    for frame in range(len(samples)//2):
        for k, weight in enumerate(weights):
            for channel in (0, 1):
                result[2*(frame+k)+channel] += samples[2*frame+channel]*weight
    return result


class ElectricTone(unittest.TestCase):
    def assertSamplesClose(self, actual, expected, tolerance=2e-14):
        self.assertEqual(len(actual), len(expected))
        self.assertLessEqual(max((abs(a-b) for a, b in zip(actual, expected)), default=0), tolerance)

    def test_exact_default_bypass_both_types_signed_zero_and_negative(self):
        for kind in ('f', 'd'):
            with self.subTest(kind=kind):
                source = stereo([(0., -0.), (-.125, .25), (.1, -.2)], kind=kind)
                before = source.tobytes()
                result = tone.process(source, source_origin_sample=5336117)
                self.assertIs(result['samples'], source)
                self.assertEqual(result['samples'].tobytes(), before)
                meta = result['metadata']
                self.assertEqual(meta['dry_input'], meta['output'])
                self.assertEqual(meta['dry_input']['sha256'], hashlib.sha256(before).hexdigest())
                self.assertEqual(meta['added_tail_samples'], 0)
                self.assertEqual(meta['input_end_sample'], 5336120)
                self.assertEqual(meta['output_end_sample'], 5336120)
                self.assertEqual(meta['latency']['phase'], 'unchanged')

    def test_strict_geometry_clock_type_and_preallocation_refusal(self):
        malformed = [[], array.array('h', [0, 0]), array.array('d'),
                     array.array('d', [0]), array.array('d', [0, 0, 0])]
        malformed.append(array.array('f', [0.])*(2*tone.MAX_FRAMES+2))
        for source in malformed:
            with self.subTest(length=len(source)), patch.object(tone, '_lowpass') as allocate:
                with self.assertRaises(ValueError): tone.process(source, settings={'mode': 'clean'})
                allocate.assert_not_called()
        source = array.array('d', [0., 0.])
        for field, values in [('sample_rate', [True, 44100., 48000]),
                              ('source_origin_sample', [True, -1, .5, 2**53])]:
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    tone.process(source, **{field: value})

    def test_invalid_samples_refuse_before_output_or_energy_overflow(self):
        for value in (float('nan'), float('inf'), -float('inf'), 16.0001, -16.0001, 1e308, -1e308):
            with self.subTest(value=value), patch.object(tone, '_lowpass') as allocate:
                with self.assertRaises(ValueError):
                    tone.process(array.array('d', [0., value]), settings={'mode': 'clean'})
                allocate.assert_not_called()

    def test_exact_duration_limit_and_numeric_limit_are_accepted_in_bypass(self):
        source = array.array('f', [0.])*(2*tone.MAX_FRAMES)
        result = tone.process(source)
        self.assertIs(result['samples'], source)
        self.assertEqual(result['metadata']['input_frames'], tone.MAX_FRAMES)
        source = array.array('d', [16., -16.])
        result = tone.process(source, settings={'overload': 'report'})
        self.assertIs(result['samples'], source)
        self.assertEqual(result['metadata']['output_levels']['samples_above_full_scale'], 2)

    def test_settings_bounds_bool_unknown_and_silently_ignored_refuse(self):
        invalid = [[], {'gain_db': 1}, {'mode': 'tube'}, {'drive': 1},
                   {'ambience_mix': .1}, {'ambience': 'room'}, {'overload': 'clip'}]
        for name, values in [('gain_db', [-24.01, 6.01, True, 1e308]),
                             ('low_cut_hz', [59.9, 160.1, float('nan')]),
                             ('high_cut_hz', [3499, 7501, '5000']),
                             ('ambience_mix', [-.01, .251, float('inf')])]:
            invalid.extend(dict(mode='clean', **{name: value}) for value in values)
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                tone.process(array.array('d', [0., 0.]), settings=settings)

    def test_finite_recurrence_matches_direct_convolution_full_tail(self):
        source = stereo([(.2, -.1), (-.3, .17), (.4, -.2), (0., .13)])
        for cutoff, length, high in ((90, 11, True), (5500, 7, False), (60, 2048, True)):
            with self.subTest(cutoff=cutoff, length=length):
                actual = tone._lowpass(source, cutoff, length, lambda: False, highpass=high)
                self.assertSamplesClose(actual, direct(source, cutoff, length, highpass=high))
                self.assertEqual(len(actual)//2, len(source)//2+length-1)

    def test_cascade_matches_independent_finite_impulse_response(self):
        source = stereo([(.1, -.2)])
        result = tone.process(source, settings={'mode': 'clean'})
        expected = direct(source, 90, tone.LOW_LENGTH, highpass=True)
        expected = direct(expected, 5500, tone.HIGH_LENGTH)
        expected = direct(expected, 5500, tone.HIGH_LENGTH)
        self.assertSamplesClose(result['samples'], expected)
        self.assertEqual(result['metadata']['added_tail_samples'], tone.FILTER_TAIL)
        self.assertEqual(len(expected)//2, 1+tone.FILTER_TAIL)

    def test_impulse_no_pre_onset_signal_and_declared_phase_not_zero_latency(self):
        source = stereo([(0., 0.)]*10+[(.1, 0.)]+[(0., 0.)]*3)
        result = tone.process(source, settings={'mode': 'clean'}, source_origin_sample=12345)
        output = result['samples']
        self.assertTrue(all(value == 0 for value in output[:20]))
        self.assertNotEqual(output[20], 0)
        self.assertTrue(all(output[i] == 0 for i in range(1, len(output), 2)))
        latency = result['metadata']['latency']
        self.assertEqual(latency['inserted_alignment_delay_samples'], 0)
        self.assertIn('frequency-dependent', latency['phase'])
        self.assertEqual(result['metadata']['output_end_sample'], 12345+len(output)//2)

    def test_finite_ambience_is_exact_declared_feed_forward_sum(self):
        source = stereo([(.13, -.07), (.08, .03), (-.1, .09)])
        dry = tone.process(source, settings={'mode': 'clean'})['samples']
        for ambience in ('room', 'delay'):
            with self.subTest(ambience=ambience):
                result = tone.process(source, settings=dict(mode='clean', ambience=ambience, ambience_mix=.2))
                expected = [0.]*(len(dry)+2*tone.TAPS[ambience][-1][0])
                for i, value in enumerate(dry):
                    expected[i] += value
                    for delay, weight in tone.TAPS[ambience]:
                        expected[i+2*delay] += value*.2*weight
                self.assertSamplesClose(result['samples'], expected, 0)
                self.assertEqual(result['metadata']['added_tail_samples'], tone.FILTER_TAIL+tone.TAPS[ambience][-1][0])
                self.assertLessEqual(result['metadata']['added_tail_samples'], tone.MAX_TAIL)

    def test_zero_mix_has_no_taps_or_artificial_tail(self):
        source = stereo([(.1, -.1)])
        clean = tone.process(source, settings={'mode': 'clean'})
        disabled = tone.process(source, settings=dict(mode='clean', ambience='delay', ambience_mix=0))
        self.assertEqual(clean['samples'].tobytes(), disabled['samples'].tobytes())
        self.assertEqual(disabled['metadata']['ambience_taps'], [])
        self.assertEqual(disabled['metadata']['added_tail_samples'], tone.FILTER_TAIL)

    def test_silence_stays_exactly_zero_with_full_declared_tail(self):
        for kind in ('f', 'd'):
            result = tone.process(array.array(kind, [0., -0.])*30,
                settings=dict(mode='clean', ambience='delay', ambience_mix=.25, gain_db=6))
            self.assertTrue(all(value == 0 for value in result['samples']))
            self.assertEqual(result['metadata']['output_levels']['sample_peak'], 0)
            self.assertIsNone(result['metadata']['output_levels']['sample_peak_dbfs'])
            self.assertFalse(result['metadata']['sample_overload'])

    def test_deterministic_and_does_not_mutate_input_or_settings(self):
        source = stereo([(math.sin(i*.13)*.1, math.cos(i*.07)*.1) for i in range(100)])
        settings = dict(mode='clean', gain_db=-3, ambience='room', ambience_mix=.1)
        before, original_settings = source.tobytes(), copy.deepcopy(settings)
        first = tone.process(source, settings=settings)
        second = tone.process(source, settings=settings)
        self.assertEqual(first, second)
        self.assertEqual(source.tobytes(), before)
        self.assertEqual(settings, original_settings)
        self.assertFalse(first['metadata']['musical_acceptance'])
        self.assertFalse(first['metadata']['event_data_received'])
        self.assertFalse(first['metadata']['dry_evidence_replaced'])
        json.dumps(first['metadata'], allow_nan=False)

    def test_gain_is_one_explicit_constant_not_peak_normalization(self):
        source = stereo([(.1, -.2), (.03, .04)])
        first = tone.process(source, settings={'mode': 'clean'})
        quieter = tone.process(source, settings={'mode': 'clean', 'gain_db': -6})
        self.assertSamplesClose(quieter['samples'], [value*10**(-6/20) for value in first['samples']], 0)
        self.assertAlmostEqual(quieter['metadata']['output_levels']['sample_peak_dbfs']-
                               first['metadata']['output_levels']['sample_peak_dbfs'], -6, places=12)

    def test_overload_refuses_or_returns_unclipped_float_with_evidence(self):
        source = stereo([(16., -16.)])
        before = source.tobytes()
        with self.assertRaises(tone.ToneOverload) as caught:
            tone.process(source, settings={'mode': 'clean', 'gain_db': 6})
        receipt = caught.exception.metadata
        self.assertTrue(receipt['sample_overload'])
        report = tone.process(source, settings={'mode': 'clean', 'gain_db': 6, 'overload': 'report'})
        self.assertGreater(max(abs(v) for v in report['samples']), 1)
        self.assertTrue(report['metadata']['export_requires_gain_selection'])
        self.assertFalse(report['metadata']['true_peak_measured'])
        self.assertFalse(report['metadata']['pcm_quantized'])
        self.assertEqual(source.tobytes(), before)
        self.assertEqual(receipt['output'], report['metadata']['output'])

    def test_constant_input_decays_to_zero_after_highpass_warmup(self):
        source = array.array('d', [.1, -.2])*5000
        result = tone.process(source, settings={'mode': 'clean'})
        stable = result['samples'][2*tone.FILTER_TAIL:10000]
        self.assertLess(max(abs(v) for v in stable), 1e-13)
        self.assertTrue(all(math.isfinite(v) for v in result['samples']))

    def test_cancel_before_allocation_and_mid_filter(self):
        with patch.object(tone, '_lowpass') as allocate, self.assertRaises(InterruptedError):
            tone.process(array.array('d', [0., 0.]), cancel=lambda: True)
        allocate.assert_not_called()
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls == 5
        source = array.array('d', [.1, -.1])*10000
        before = source.tobytes()
        with self.assertRaises(InterruptedError):
            tone.process(source, settings={'mode': 'clean'}, cancel=cancel)
        self.assertEqual(source.tobytes(), before)

    def test_detected_input_mutation_refuses_result(self):
        source = stereo([(.1, .2), (.2, .1)])
        original = tone._lowpass
        def mutate(*args, **kwargs):
            output = original(*args, **kwargs)
            source[0] = .3
            return output
        with patch.object(tone, '_lowpass', side_effect=mutate), self.assertRaisesRegex(ValueError, 'changed'):
            tone.process(source, settings={'mode': 'clean'})


if __name__ == '__main__':
    unittest.main()
