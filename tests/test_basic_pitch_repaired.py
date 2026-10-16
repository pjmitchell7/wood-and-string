"""Synthetic retained matrices only: no model, waveform, renderer or playback."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import numpy as np

from audio.basic_pitch import frame_samples, official_frame_samples
from audio import basic_pitch_repaired as repaired
from audio import basic_pitch_upstream as upstream


def report(count=96, rate=44100, origin=5336117):
    return {'source_rate': rate, 'source_origin_sample': origin,
        'source_length': int(frame_samples(count + 1, rate)[-1]),
        'raw_predictions': {key: np.zeros((count, width), np.float32)
            for key, width in (('note', 88), ('onset', 88), ('contour', 264))}}


def support(value, first, end, *, pitch=60, activation=.8, onset=None):
    value['raw_predictions']['note'][first:end, pitch - 21] = activation
    if onset is not None:
        value['raw_predictions']['onset'][onset, pitch - 21] = .9


def spans(value):
    return [(row['start_frame'], row['end_frame_exclusive'], row['pitch']) for row in value['events']]


class BasicPitchRepairedTests(unittest.TestCase):
    def test_zero_difference_keeps_direct_onset_instead_of_nan_replacement(self):
        value = report(32)
        support(value, 0, 32, onset=4)
        old = upstream.decode_saved(value)
        new = repaired.decode_saved(value)
        self.assertEqual(old['nonfinite_augmented_onset_cells'], 32 * 88)
        self.assertEqual(new['nonfinite_augmented_onset_cells'], 0)
        self.assertEqual(spans(new), [(4, 32, 60)])
        self.assertTrue(new['events'][0]['onset_head_peak_at_start'])
        self.assertEqual(new['warnings'], [])
        self.assertEqual(new['version'], repaired.VERSION)

    def test_positive_difference_onset_map_is_bit_exact_to_pinned_function(self):
        for seed in range(12):
            random = np.random.default_rng(seed)
            frames = random.random((64, 88), dtype=np.float32)
            onsets = random.random((64, 88), dtype=np.float32)
            np.testing.assert_array_equal(repaired._infer_onsets(onsets, frames),
                upstream.get_infered_onsets(onsets, frames))
        frames = np.ones((32, 88), np.float32)
        onsets = np.zeros_like(frames)
        np.testing.assert_array_equal(repaired._infer_onsets(onsets, frames), onsets)

    def test_melodia_half_open_endpoint_covers_last_supported_frame(self):
        value = report(32)
        support(value, 4, 20)
        self.assertEqual(spans(upstream.decode_saved(value)), [(4, 19, 60)])
        result = repaired.decode_saved(value)
        self.assertEqual(spans(result), [(4, 20, 60)])
        self.assertEqual(result['unassigned_active_frame_pitch_count'], 0)

    def test_direct_and_melodia_support_reaching_eof_keep_final_frame(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                value = report(32)
                support(value, 4, 32, onset=4 if direct else None)
                policy = 'onset_head_ablation' if direct else 'published_default'
                old = upstream.decode_saved(value, policy)
                result = repaired.decode_saved(value, policy)
                self.assertEqual(spans(old), [(4, 31 if direct else 30, 60)])
                self.assertEqual(spans(result), [(4, 32, 60)])
                self.assertEqual(result['events'][0]['end'], value['source_origin_sample'] + value['source_length'])

    def test_melodia_backward_scan_keeps_frame_zero_when_peak_is_later(self):
        value = report(40)
        support(value, 0, 20, activation=.7)
        value['raw_predictions']['note'][6, 39] = .95
        self.assertEqual(spans(upstream.decode_saved(value)), [(1, 19, 60)])
        self.assertEqual(spans(repaired.decode_saved(value)), [(0, 20, 60)])

    def test_real_repluck_candidates_remain_distinct_even_without_activation_gap(self):
        value = report()
        support(value, 8, 64, onset=10)
        value['raw_predictions']['onset'][37, 39] = .9
        old = upstream.decode_saved(value, 'onset_head_ablation')
        new = repaired.decode_saved(value, 'onset_head_ablation')
        self.assertEqual(spans(old), [(10, 37, 60), (37, 64, 60)])
        self.assertEqual(new['events'], old['events'])

    def test_separated_repeated_notes_preserve_an_existing_long_rest(self):
        value = report(128)
        support(value, 10, 32, onset=10)
        support(value, 52, 74, onset=52)
        for policy in repaired.POLICIES:
            with self.subTest(policy=policy):
                result = repaired.decode_saved(value, policy)
                self.assertEqual(spans(result), [(10, 32, 60), (52, 74, 60)])
                self.assertEqual(result['events'], upstream.decode_saved(value, policy)['events'])

    def test_short_gap_bridging_is_unchanged_not_a_new_sustain_policy(self):
        value = report()
        support(value, 10, 32, onset=12)
        support(value, 37, 61, onset=39)
        for policy in repaired.POLICIES:
            with self.subTest(policy=policy):
                old = upstream.decode_saved(value, policy)
                new = repaired.decode_saved(value, policy)
                self.assertEqual(new['events'], old['events'])
                expected = ([(10, 12, 60), (12, 32, 60), (37, 39, 60), (39, 61, 60)]
                    if policy == 'duration_matched' else [(12, 39, 60), (39, 61, 60)])
                self.assertEqual(spans(new), expected)
                self.assertEqual(new['settings']['energy_tol'], 11)

    def test_polyphonic_and_adjacent_pitch_consumption_are_unchanged(self):
        value = report()
        support(value, 5, 40, pitch=60, onset=5)
        support(value, 9, 40, pitch=61, onset=9)
        old = upstream.decode_saved(value, 'onset_head_ablation')
        new = repaired.decode_saved(value, 'onset_head_ablation')
        self.assertEqual(new['events'], old['events'])
        self.assertEqual(spans(new), [(5, 9, 60), (9, 40, 61)])

    def test_policy_thresholds_and_strict_duration_rule_remain_exact(self):
        self.assertEqual(repaired.POLICIES, upstream.POLICIES)
        for length in (11, 12):
            value = report(64)
            support(value, 10, 10 + length)
            result = repaired.decode_saved(value)
            self.assertEqual(len(result['events']), int(length > 11))
            self.assertEqual(result['settings'], {'onset_thresh': .5, 'frame_thresh': .3, **upstream.POLICIES['published_default']})
        value = report()
        result = repaired.decode_saved(value)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['nonfinite_augmented_onset_cells'], 0)

    def test_many_interior_direct_cases_are_exact_under_all_unchanged_policies(self):
        for seed in range(20):
            value = report(128)
            random = np.random.default_rng(seed)
            value['raw_predictions']['note'][:] = random.uniform(0, .05, (128, 88))
            for pitch, first, end in ((55, 6, 30), (60, 43, 72), (64, 80, 104)):
                support(value, first, end, pitch=pitch, onset=first)
                value['raw_predictions']['note'][first:end, pitch - 21] += random.uniform(0, .05, end - first)
            for policy in repaired.POLICIES:
                with self.subTest(seed=seed, policy=policy):
                    new, old = repaired.decode_saved(value, policy), upstream.decode_saved(value, policy)
                    self.assertEqual(new['events'], old['events'])
                    self.assertEqual(new['settings'], old['settings'])
                    self.assertEqual(new['unassigned_active_frame_pitch_count'], old['unassigned_active_frame_pitch_count'])

    def test_source_anchor_origin_and_window_boundary_convention_remain_exact(self):
        for rate in (22050, 44100, 48000):
            value = report(180, rate)
            support(value, 140, 160, onset=140)
            result = repaired.decode_saved(value, 'onset_head_ablation')
            note = result['events'][0]
            anchors = frame_samples(181, rate, value['source_origin_sample'])
            official = official_frame_samples(181, rate, value['source_origin_sample'])
            self.assertEqual((note['start'], note['end']), (anchors[140], anchors[160]))
            self.assertEqual((note['official_start'], note['official_end']), (official[140], official[160]))

    def test_near_int64_limit_clips_locally_and_preserves_exact_official_comparison(self):
        for rate in (22050, 44100, 48000):
            for count, first in ((32, 4), (160, 140)):
                with self.subTest(rate=rate, count=count):
                    value = report(count, rate)
                    # The final model anchor lies just inside this passage;
                    # its exclusive endpoint needs clipping before origin adds.
                    value['source_length'] = int(frame_samples(count, rate)[-1]) + 1
                    value['source_origin_sample'] = 2**63 - 1 - value['source_length']
                    support(value, first, count, onset=first)
                    result = repaired.decode_saved(value, 'onset_head_ablation')
                    self.assertEqual(spans(result), [(first, count, 60)])
                    note = result['events'][0]
                    origin = value['source_origin_sample']
                    self.assertEqual(note['start'], origin + int(frame_samples(count + 1, rate)[first]))
                    self.assertEqual(note['end'], 2**63 - 1)
                    official = official_frame_samples(count + 1, rate)
                    self.assertEqual(note['official_start'], origin + int(official[first]))
                    self.assertEqual(note['official_end'], origin + int(official[count]))
                    self.assertGreaterEqual(note['official_end'], note['official_start'])
                    self.assertEqual(result['warnings'], [])

    def test_json_and_tuple_matrices_preserve_the_ndarray_decoding_result(self):
        value = report(64)
        support(value, 5, 30, onset=5)
        expected = repaired.decode_saved(value)
        for outer_type, row_type in ((list, list), (tuple, tuple)):
            with self.subTest(outer_type=outer_type):
                saved = deepcopy(value)
                saved['raw_predictions'] = {key: outer_type(row_type(row) for row in matrix.tolist())
                    for key, matrix in value['raw_predictions'].items()}
                self.assertEqual(repaired.decode_saved(saved), expected)

    def test_all_matrix_shapes_and_scalar_types_are_checked_before_conversion(self):
        class ConversionTrap:
            def __len__(self):
                raise AssertionError('Custom sequences must not be inspected or converted')

            def __array__(self, *args, **kwargs):
                raise AssertionError('Custom arrays must not be converted')

        changes = []
        for key, width in (('note', 88), ('onset', 88), ('contour', 264)):
            changes.extend([
                lambda v, k=key, w=width: v['raw_predictions'].update({k: [[0.] * (w + 1)] * 96}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: np.zeros((96, w + 1))}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: np.zeros((96, w, 1))}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: [[[0.]] * w] * 96}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: [['0'] * w] * 96}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: [np.zeros(w)] * 96}),
                lambda v, k=key: v['raw_predictions'].update({k: ConversionTrap()}),
                lambda v, k=key, w=width: v['raw_predictions'].update({k: [[0.] * w] * 95}),
            ])
        for index, change in enumerate(changes):
            value = report()
            change(value)
            with self.subTest(index=index), patch.object(repaired.np, 'asarray',
                    side_effect=AssertionError('Malformed dimensions must fail before conversion')):
                with self.assertRaises(ValueError):
                    repaired.decode_saved(value)

    def test_matrices_and_source_metadata_are_never_mutated(self):
        value = report()
        support(value, 10, 32, onset=12)
        before = deepcopy(value)
        repaired.decode_saved(value)
        for key in ('note', 'onset', 'contour'):
            np.testing.assert_array_equal(value['raw_predictions'][key], before['raw_predictions'][key])
        self.assertEqual({k: v for k, v in value.items() if k != 'raw_predictions'},
                         {k: v for k, v in before.items() if k != 'raw_predictions'})

    def test_invalid_or_unbounded_matrices_and_clocks_fail_before_decoding(self):
        changes = [lambda v: v['raw_predictions'].pop('contour'),
            lambda v: v['raw_predictions'].update(note=np.zeros((2, 88))),
            lambda v: v['raw_predictions'].update(note=np.zeros((repaired.MAX_FRAMES + 1, 88))),
            lambda v: v['raw_predictions'].update(onset=np.zeros((96, 87))),
            lambda v: v['raw_predictions'].update(contour=np.zeros((95, 264))),
            lambda v: v['raw_predictions'].update(note=np.zeros((96, 88), np.complex64)),
            lambda v: v['raw_predictions'].update(note=[['non-numeric'] * 88] * 96),
            lambda v: v.update(source_rate=True), lambda v: v.update(source_rate=16000),
            lambda v: v.update(source_length=True), lambda v: v.update(source_length=0),
            lambda v: v.update(source_length=31 * 44100), lambda v: v.update(source_length=100),
            lambda v: v.update(source_origin_sample=-1), lambda v: v.update(source_origin_sample=2**63)]
        for key in ('note', 'onset', 'contour'):
            for bad in (float('nan'), float('inf'), -.01, 1.01):
                changes.append(lambda v, k=key, x=bad: v['raw_predictions'][k].__setitem__((0, 0), x))
        for index, change in enumerate(changes):
            with self.subTest(index=index), self.assertRaises(ValueError):
                value = report();change(value);repaired.decode_saved(value)
        for policy in ('unknown', None, [], True):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                repaired.decode_saved(report(), policy)


if __name__ == '__main__':
    unittest.main()
