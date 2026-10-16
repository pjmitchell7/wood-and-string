"""Silent source-evidence/physical constraints; no assets, models or inference."""
import copy
from dataclasses import asdict
import unittest
from unittest.mock import patch

from audio.performance import Note, Performance
from audio.electric_performance import (
    ARTICULATION, MAX_FRET, OPEN, PITCH_RANGE, PROFILE_VERSION, VERSION,
    arrange_electric, string_pitches,
)


class ElectricPerformanceTests(unittest.TestCase):
    def make(self, notes, **changes):
        return Performance('observed-original', 44100, notes, **changes)

    def assert_evidence_and_constraints(self, source, arrangement):
        originals = {note.id: note for note in source.notes}
        placed = arrangement['notes']
        self.assertEqual(len({note['id'] for note in placed}), len(placed))
        omissions = [entry for entry in arrangement['adaptations'] if entry['kind'] == 'omission']
        self.assertEqual({note['id'] for note in placed}.intersection(entry['note_id'] for entry in omissions), set())
        self.assertEqual({note['id'] for note in placed} | {entry['note_id'] for entry in omissions}, set(originals))
        self.assertEqual(len(placed) + len(omissions), len(originals))
        for note in placed:
            original = originals[note['id']]
            for field in ('start', 'end', 'velocity', 'role', 'confidence', 'phrase', 'contour'):
                self.assertEqual(note[field], getattr(original, field), field)
            self.assertEqual(note['source_pitch'], original.pitch)
            self.assertEqual((note['pitch'] - original.pitch) % 12, 0)
            self.assertEqual(note['articulation'], ARTICULATION)
            self.assertEqual(note['pitch'], OPEN[note['string']] + note['fret'])
            self.assertIn(note['pitch'], string_pitches()[note['string']])
            self.assertTrue(0 <= note['fret'] <= MAX_FRET)
            same = [other for other in placed if other['player'] == note['player']]
            overlapping = [other for other in placed if other['start'] < note['end'] and other['end'] > note['start']]
            self.assertFalse(any(other['id'] != note['id'] and other['pitch'] == note['pitch'] for other in overlapping))
            for sample in {note['start'], *(other['start'] for other in overlapping
                                           if note['start'] <= other['start'] < note['end'])}:
                simultaneous = [other for other in same if other['start'] <= sample < other['end']]
                hand = [other['fret'] for other in simultaneous if other['fret']]
                self.assertLessEqual(len(hand), 4)
                self.assertLessEqual(max(hand, default=0) - min(hand, default=0), 4)
                self.assertEqual(len(simultaneous), len({other['string'] for other in simultaneous}))
            if note['role'] == 'bass':
                self.assertTrue(all(note['pitch'] < other['pitch'] for other in overlapping if other['role'] == 'melody'))
            if note['fret']:
                for other in same:
                    if other['fret'] and other['end'] <= note['start']:
                        self.assertLessEqual(abs(note['fret'] - other['fret']),
                                             4 + (note['start'] - other['end']) * 12 // 44100)
            shifts = [entry for entry in arrangement['adaptations'] if entry['kind'] == 'octave' and entry['note_id'] == note['id']]
            self.assertEqual(len(shifts), int(note['pitch'] != original.pitch))
            if shifts:
                self.assertEqual(shifts[0]['semitones'], note['pitch'] - original.pitch)

    def test_full_declared_coverage_does_not_read_acoustic_sfz_or_bank(self):
        with patch('audio.performance.sampled_pitches', side_effect=AssertionError('Unrelated acoustic coverage')):
            coverage = string_pitches()
            self.assertEqual(len(coverage), 6)
            for string, opened in enumerate(OPEN):
                self.assertEqual(coverage[string], frozenset(range(opened, min(opened + 20, 84) + 1)))
            self.assertIn(52, coverage[0])  # E3 absent from the acoustic low-E mapping.
            for pitch in range(40, 85):
                source = self.make([Note('single', 17, 500, pitch, 127)])
                arrangement = arrange_electric(source)
                self.assertTrue(arrangement['renderable'])
                self.assertEqual(arrangement['notes'][0]['pitch'], pitch)
                self.assert_evidence_and_constraints(source, arrangement)

    def test_exact_source_origin_gates_velocity_and_detached_nested_metadata(self):
        origin = 5_336_117
        source = self.make([Note('observed', origin + 13, origin + 400, 64, 127, confidence=.42,
                                 phrase='phrase-a', contour=((origin + 20, 64.125),))],
                           analysis_offset_samples=91, beats=[{'sample': origin + 13, 'evidence': [1, 2]}])
        before = copy.deepcopy(source)
        arrangement = arrange_electric(source, render_origin_sample=origin)
        self.assertEqual(source, before)
        self.assertEqual(arrangement['source'], asdict(source))
        self.assertEqual(arrangement['render_origin_sample'], origin)
        self.assertEqual(arrangement['source']['analysis_offset_samples'], 91)
        self.assertEqual(arrangement['planner_version'], VERSION)
        self.assertEqual(arrangement['profile_version'], PROFILE_VERSION)
        self.assertEqual(arrangement['notes'][0]['velocity'], 127)
        self.assert_evidence_and_constraints(source, arrangement)
        arrangement['source']['beats'][0]['evidence'].append(3)
        arrangement['source']['notes'][0]['start'] = 0
        arrangement['notes'][0]['start'] = 0
        self.assertEqual(source, before)

    def test_duo_uses_distinct_source_roles_and_no_overlapping_unison(self):
        source = self.make([Note('melody', 100, 2000, 64),
                            Note('foundation', 0, 3000, 64, role='bass'),
                            Note('harmony', 500, 1800, 64, role='harmony')])
        arrangement = arrange_electric(source, 2)
        self.assertTrue(arrangement['renderable'])
        self.assertEqual(len(arrangement['notes']), 3)
        for note in arrangement['notes']:
            self.assertEqual(note['player'], 1 if note['role'] == 'melody' else 2)
        self.assert_evidence_and_constraints(source, arrangement)

    def test_complete_melody_reserved_before_earlier_sustained_accompaniment(self):
        source = self.make([Note('earlier-accompaniment', 0, 44100, 84, role='harmony'),
                            Note('later-melody', 22050, 33075, 84)])
        arrangement = arrange_electric(source)
        melody = next(note for note in arrangement['notes'] if note['id'] == 'later-melody')
        self.assertEqual((melody['pitch'], melody['string'], melody['end']), (84, 5, 33075))
        self.assertFalse(any(entry['note_id'] == 'later-melody' for entry in arrangement['adaptations']))
        self.assert_evidence_and_constraints(source, arrangement)

    def test_foundation_stays_below_all_overlapping_melody_in_solo_and_duo(self):
        source = self.make([Note('upper', 1000, 2000, 64), Note('lower', 2500, 3500, 52),
                            Note('early-foundation', 0, 4000, 64, role='bass')])
        for performers in (1, 2):
            arrangement = arrange_electric(source, performers)
            bass = next(note for note in arrangement['notes'] if note['role'] == 'bass')
            self.assertEqual(bass['pitch'], 40)
            self.assertEqual(bass['end'], 4000)
            self.assert_evidence_and_constraints(source, arrangement)

    def test_impossible_lower_foundation_is_omitted_and_empty_player_disclosed(self):
        source = self.make([Note('protected-low-melody', 0, 3000, 40),
                            Note('unplaceable-foundation', 100, 2000, 40, role='bass')])
        arrangement = arrange_electric(source, 2)
        self.assertEqual([note['id'] for note in arrangement['notes']], ['protected-low-melody'])
        self.assertFalse(arrangement['renderable'])
        self.assertTrue(any(issue['kind'] == 'performer-without-material' and issue['player'] == 2
                            for issue in arrangement['issues']))
        self.assert_evidence_and_constraints(source, arrangement)

    def test_crowded_chord_omits_essential_notes_without_truncating_any_gate(self):
        source = self.make([Note(f'melody-{i}', 100, 4000, 60 + i,
                                 contour=((500, 60. + i),)) for i in range(10)])
        arrangement = arrange_electric(source)
        omissions = [entry for entry in arrangement['adaptations'] if entry['kind'] == 'omission']
        self.assertTrue(omissions)
        self.assertTrue(all(entry['essential'] for entry in omissions))
        self.assertFalse(arrangement['renderable'])
        self.assertEqual({issue['note_id'] for issue in arrangement['issues'] if issue['kind'] == 'essential-note-unplaced'},
                         {entry['note_id'] for entry in omissions})
        self.assertEqual({entry['note_id'] for entry in arrangement['adaptations'] if entry['kind'] == 'contour-not-rendered'},
                         {note.id for note in source.notes})
        self.assert_evidence_and_constraints(source, arrangement)

    def test_open_strings_do_not_consume_four_fretted_fingers(self):
        source = self.make([Note(str(pitch), 0, 2000, pitch) for pitch in OPEN])
        arrangement = arrange_electric(source)
        self.assertEqual(len(arrangement['notes']), 6)
        self.assertTrue(all(note['fret'] == 0 for note in arrangement['notes']))
        self.assertTrue(arrangement['renderable'])
        self.assert_evidence_and_constraints(source, arrangement)

    def test_five_fretted_notes_cannot_borrow_a_fifth_finger(self):
        source = self.make([Note(chr(97 + i), 0, 2000, pitch)
                            for i, pitch in enumerate([41, 46, 51, 56, 60])])
        arrangement = arrange_electric(source)
        self.assertEqual(len(arrangement['notes']), 4)
        self.assertTrue(any(entry['kind'] == 'omission' and entry['note_id'] == 'e'
                            for entry in arrangement['adaptations']))
        self.assert_evidence_and_constraints(source, arrangement)

    def test_hand_span_forces_a_disclosed_octave_instead_of_impossible_reach(self):
        source = self.make([Note('low-anchor', 0, 3000, 41), Note('high-entrance', 1000, 2000, 84)])
        arrangement = arrange_electric(source)
        high = next(note for note in arrangement['notes'] if note['id'] == 'high-entrance')
        self.assertEqual(high['pitch'], 60)
        self.assertTrue(any(entry['kind'] == 'octave' and entry['note_id'] == 'high-entrance'
                            and entry['semitones'] == -24 for entry in arrangement['adaptations']))
        self.assert_evidence_and_constraints(source, arrangement)

    def test_long_gate_does_not_count_successive_notes_as_a_simultaneous_chord(self):
        from audio.electric_performance import _candidates
        successive = [{'id': str(i), 'start': i * 10, 'end': (i + 1) * 10,
                       'pitch': OPEN[i] + 5, 'fret': 5, 'string': i, 'player': 1, 'role': 'melody'}
                      for i in range(4)]
        candidates = _candidates(Note('held', 0, 40, 69, role='harmony'), 1, successive, 44100)
        self.assertTrue(any(candidate[1:4] == (5, 5, 69) for candidate in candidates))

    def test_deterministic_assignment_touching_gates_and_real_rests(self):
        notes = [Note('a', 100, 600, 64), Note('b', 600, 900, 64), Note('c', 1700, 2400, 67),
                 Note('foundation', 0, 2500, 40, role='bass')]
        source = self.make(notes)
        first = arrange_electric(source, 2)
        second = arrange_electric(self.make(list(reversed(notes))), 2)
        self.assertEqual(first['notes'], second['notes'])
        self.assertEqual(first['adaptations'], second['adaptations'])
        self.assertEqual([(note['start'], note['end']) for note in first['notes'] if note['role'] == 'melody'],
                         [(100, 600), (600, 900), (1700, 2400)])
        self.assert_evidence_and_constraints(source, first)

    def test_extreme_source_register_changes_are_only_disclosed_octaves(self):
        source = self.make([Note('lowest-midi', 0, 200, 0), Note('highest-midi', 44100, 45000, 127)])
        arrangement = arrange_electric(source)
        self.assertEqual(len(arrangement['notes']), 2)
        self.assertEqual(len([entry for entry in arrangement['adaptations'] if entry['kind'] == 'octave']), 2)
        self.assert_evidence_and_constraints(source, arrangement)

    def test_near_instant_fret_jump_chain_cannot_accumulate_unbounded_movement(self):
        from audio.electric_performance import _candidates
        # Historical reservations leave every other string occupied; the only
        # possible exact pitch on string 5 jumps from fret 1 to 9 in two samples.
        history = [{'id': 'one', 'start': 0, 'end': 100, 'pitch': 65, 'fret': 1, 'string': 5, 'player': 1, 'role': 'melody'},
                   {'id': 'five', 'start': 100, 'end': 101, 'pitch': 69, 'fret': 5, 'string': 5, 'player': 1, 'role': 'melody'}]
        history += [{'id': f'block-{string}', 'start': 101, 'end': 200, 'pitch': opened,
                     'fret': 0, 'string': string, 'player': 1, 'role': 'melody'} for string, opened in enumerate(OPEN[:5])]
        candidates = _candidates(Note('nine', 101, 200, 73), 1, history, 44100)
        self.assertFalse(any(candidate[3] == 73 for candidate in candidates))

    def test_compacted_movement_keeps_the_nearest_boundary_for_each_fret(self):
        from audio.electric_performance import _candidates
        note = Note('candidate', 44100, 44200, 72)
        # Old/distant boundaries permit this reach, but the nearest repeated
        # fret does not. Both directions must preserve that tighter constraint
        # regardless of insertion order.
        before = [{'id': 'old', 'start': 0, 'end': 1, 'pitch': 43, 'fret': 3, 'string': 0, 'player': 1, 'role': 'melody'},
                  {'id': 'near', 'start': 40000, 'end': 44099, 'pitch': 43, 'fret': 3, 'string': 0, 'player': 1, 'role': 'melody'}]
        after = [{'id': 'far', 'start': 88200, 'end': 88300, 'pitch': 43, 'fret': 3, 'string': 0, 'player': 1, 'role': 'melody'},
                 {'id': 'near', 'start': 44201, 'end': 45000, 'pitch': 43, 'fret': 3, 'string': 0, 'player': 1, 'role': 'melody'}]
        self.assertTrue(any(candidate[3] == 72 for candidate in _candidates(note, 1, [before[0]], 44100)))
        self.assertTrue(any(candidate[3] == 72 for candidate in _candidates(note, 1, [after[0]], 44100)))
        for history in (before, before[::-1], after, after[::-1]):
            self.assertFalse(any(candidate[3] == 72 for candidate in _candidates(note, 1, history, 44100)))

    def test_render_event_shape_matches_dry_renderer_without_native_loading(self):
        from audio.electric_render import validate
        origin = 5336117
        source = self.make([Note('m', origin + 3, origin + 333, 64, 127),
                            Note('b', origin, origin + 400, 40, role='bass')])
        arrangement = arrange_electric(source, 2, render_origin_sample=origin)
        self.assertTrue(arrangement['renderable'])
        rate, actual_origin, groups = validate(arrangement)
        self.assertEqual((rate, actual_origin), (44100, origin))
        self.assertEqual({key[0] for key in groups}, {1, 2})

    def test_strict_types_roles_bounds_and_duo_material(self):
        valid = self.make([Note('m', 0, 1000, 64)])
        for performers in (True, False, 0, 3, 1., None):
            with self.subTest(performers=performers), self.assertRaises(ValueError):
                arrange_electric(valid, performers)
        for origin in (True, -1, 0., None):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                arrange_electric(valid, render_origin_sample=origin)
        invalid = [self.make([]), self.make([Note('m', True, 1000, 64)]),
                   self.make([Note('m', 0, 1000, 64, confidence=True)]),
                   self.make([Note('m', 0, 1000, 64, role='foundation')]),
                   self.make([Note('m', 0, 1000, 64, velocity=0)]),
                   self.make([Note('m', 0, 1000, 64, velocity=128)]),
                   self.make([Note('m' * 201, 0, 1000, 64)]),
                   self.make([Note('m', 0, 1000, 64), Note('m', 0, 2000, 65)]),
                   self.make([Note('m', 0, 30 * 44100 + 1, 64)]),
                   self.make([Note('m', 0, 1000, 64, contour=((500, float('nan')),))]),
                   self.make([Note(str(i), 0, 1000, 64) for i in range(4001)])]
        invalid += [Performance('x', 48000, valid.notes), Performance(True, 44100, valid.notes),
                    Performance('x', True, valid.notes), {'source_id': 'x'}]
        for source in invalid:
            with self.subTest(source=type(source)), self.assertRaises(ValueError):
                arrange_electric(source)
        with self.assertRaises(ValueError):
            arrange_electric(valid, 2)
        with self.assertRaises(ValueError):
            arrange_electric(self.make([Note('h', 0, 1000, 64, role='harmony')]), 2)
        with self.assertRaises(ValueError):
            arrange_electric(valid, render_origin_sample=1)


if __name__ == '__main__':
    unittest.main()
