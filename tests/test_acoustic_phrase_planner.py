"""Pure source-event controls; no sample decoding, renderer or model."""
import unittest
import random
from dataclasses import asdict
from unittest.mock import patch

from audio import acoustic_phrase_planner as candidate
from audio.performance import Note, Performance, arrange, sampled_pitches, _candidates


class AcousticPhrasePlannerTests(unittest.TestCase):
    def plan(self, notes, performers=1):
        source = Performance('self-created-controls', 44100, notes,
                             analysis_offset_samples=5336117)
        before = asdict(source)
        result = candidate.arrange(source, performers)
        self.assertEqual(asdict(source), before)
        self.assertEqual(result['source'], before)
        originals = {n.id: n for n in notes}
        assigned = {n['id']: n for n in result['notes']}
        omitted = {a['note_id'] for a in result['adaptations'] if a['kind'] == 'omission'}
        self.assertFalse(set(assigned) & omitted)
        self.assertEqual(set(assigned) | omitted, set(originals))
        coverage = sampled_pitches()
        replay = []
        for source_note in sorted(notes, key=lambda n: (candidate.ROLE_ORDER[n.role], n.start, n.id)):
            if source_note.id not in assigned:
                continue
            out = assigned[source_note.id]
            self.assertEqual((out['start'], out['end'], out['velocity'], out['role'],
                              out['phrase'], out['confidence'], out['contour']),
                             (source_note.start, source_note.end, source_note.velocity,
                              source_note.role, source_note.phrase, source_note.confidence,
                              source_note.contour))
            self.assertEqual(out['source_pitch'], source_note.pitch)
            self.assertIn(out['pitch'], coverage[out['string']])
            self.assertIn((out['string'], out['fret'], out['pitch']),
                          [(c[1], c[2], c[3]) for c in _candidates(
                              source_note, replay, out['player'], coverage, 44100)])
            replay.append(out)
        return result

    def test_fixed_policy(self):
        self.assertEqual((candidate.BEAM_WIDTH, candidate.MAX_SUCCESSORS, candidate.MAX_NOTES),
                         (128, 250000, 4000))

    def test_future_ringing_reservation_without_source_strings(self):
        notes = [Note('a', 5336117, 5346117, 45), Note('b', 5341117, 5351117, 47)]
        self.assertEqual(arrange(Performance('known', 44100, notes))['notes'][1]['pitch'], 59)
        result = self.plan(notes)
        self.assertEqual([n['pitch'] for n in result['notes']], [45, 47])
        self.assertEqual(result['adaptations'], [])
        self.assertTrue(result['search']['complete'])

    def test_more_than_one_following_note(self):
        notes = [Note(str(i), i * 5000, 20000 + i * 5000, pitch)
                 for i, pitch in enumerate([66, 49, 74])]
        result = self.plan(notes)
        self.assertEqual([n['pitch'] for n in result['notes']], [66, 49, 74])

    def test_nonuniform_clock_contour_and_intentional_rest_preserved(self):
        origin = 5336117
        notes = [Note('held', origin + 113, origin + 4517, 64, 59,
                      confidence=.61, phrase='one', contour=((origin + 200, 64.2),)),
                 Note('reattack', origin + 4999, origin + 8013, 64, 95, phrase='two')]
        result = self.plan(notes)
        self.assertEqual(result['notes'][1]['start'] - result['notes'][0]['end'], 482)
        self.assertEqual([a['kind'] for a in result['adaptations']], ['contour-not-rendered'])

    def test_source_priority_and_duet_role_routing(self):
        notes = [Note('bass', 0, 20000, 40, role='bass'),
                 Note('harmony', 500, 15000, 52, role='harmony'),
                 Note('melody', 5000, 10000, 64)]
        result = self.plan(notes, 2)
        self.assertEqual({n['id']: n['player'] for n in result['notes']},
                         {'melody': 1, 'bass': 2, 'harmony': 2})
        self.assertEqual(result['adaptations'], [])

    def test_later_melody_reservation_is_kept_for_earlier_accompaniment(self):
        notes = [Note('melody', 10000, 20000, 40),
                 Note('bass', 0, 15000, 40, role='bass')]
        result = self.plan(notes)
        self.assertEqual(next(n for n in result['notes'] if n['id'] == 'melody')['pitch'], 40)
        self.assertTrue(any(a['note_id'] == 'bass' for a in result['adaptations']))

    def test_impossible_essential_coverage_has_explicit_omission(self):
        result = self.plan([Note('uncovered', 0, 10000, 0), Note('kept', 0, 10000, 64)])
        omission = next(a for a in result['adaptations'] if a['kind'] == 'omission')
        self.assertEqual((omission['note_id'], omission['essential']), ('uncovered', True))
        self.assertIn('not a proof', omission['reason'])

    def test_octave_shift_is_disclosed(self):
        result = self.plan([Note('low', 0, 1000, 28)])
        self.assertEqual(result['notes'][0]['source_pitch'], 28)
        self.assertEqual(result['notes'][0]['pitch'], 40)
        self.assertEqual(result['adaptations'][0]['semitones'], 12)

    def test_touching_attacks_are_separate_but_overlapping_notes_use_separate_strings(self):
        result = self.plan([Note('a', 0, 5000, 64), Note('b', 5000, 10000, 64),
                            Note('ring', 6000, 12000, 64)])
        self.assertEqual(len(result['notes']), 3)
        by_id = {n['id']: n for n in result['notes']}
        self.assertNotEqual(by_id['b']['string'], by_id['ring']['string'])
        self.assertEqual(by_id['a']['end'], by_id['b']['start'])

    def test_search_cap_is_explicit_nonrenderable_no_fallback(self):
        notes = [Note('a', 0, 10000, 45), Note('b', 5000, 15000, 47)]
        with patch.object(candidate, 'MAX_SUCCESSORS', 1):
            result = self.plan(notes)
        self.assertFalse(result['renderable'])
        self.assertFalse(result['search']['complete'])
        self.assertTrue(result['search']['exhausted'])
        self.assertEqual(result['search']['processed_notes'], 0)
        self.assertEqual(result['notes'], [])
        self.assertEqual(len(result['adaptations']), 2)
        self.assertTrue(all(a['unresolved'] for a in result['adaptations']))

    def test_search_cap_preserves_only_a_completed_prefix_and_accounts_for_suffix(self):
        notes = [Note('a', 0, 10000, 45), Note('b', 5000, 15000, 47)]
        first_layer = len(_candidates(notes[0], [], 1, sampled_pitches(), 44100)) + 1
        with patch.object(candidate, 'MAX_SUCCESSORS', first_layer):
            result = self.plan(notes)
        self.assertFalse(result['renderable'])
        self.assertEqual(result['search']['processed_notes'], 1)
        self.assertEqual([n['id'] for n in result['notes']], ['a'])
        self.assertEqual(result['adaptations'][0]['note_id'], 'b')
        self.assertTrue(result['adaptations'][0]['unresolved'])
        self.assertEqual(result['search']['cost_scope'], 'Completed prefix only')

    def test_context_pruning_preserves_full_history_candidate_constraints_and_costs(self):
        # Includes future reservations and same-end ties. Random contexts need
        # not be playable: this specifically checks the pruning identity against
        # the unchanged reference function, not a musical-quality distribution.
        rng = random.Random(43817)
        coverage = sampled_pitches()
        for case in range(160):
            boundary = rng.randrange(1, 20000)
            history = []
            for i in range(24):
                start = rng.randrange(0, 25000)
                history.append({'id': str(i), 'player': rng.choice((1, 2)),
                                'string': rng.randrange(6), 'fret': rng.randrange(21),
                                'start': start, 'end': start + rng.choice((1000, 2000, 4000))})
            # Deliberately equal predecessor end times with different frets.
            history.extend([{'id': 'tie-' + str(i), 'player': 1, 'string': i,
                             'fret': 1 + i * 3, 'start': 0, 'end': boundary}
                            for i in range(2)])
            compact = candidate._context(tuple(history), boundary)
            start = boundary + rng.randrange(0, 15000)
            note = Note(str(case), start, start + rng.randrange(1, 10000), rng.randrange(35, 90))
            for player in (1, 2):
                self.assertEqual(_candidates(note, history, player, coverage, 44100),
                                 _candidates(note, compact, player, coverage, 44100))

    def test_essential_material_is_not_displaced_by_optional_roles(self):
        notes = [Note('essential', 5000, 15000, 40),
                 Note('optional', 0, 20000, 40, role='harmony')]
        result = self.plan(notes)
        self.assertEqual(next(n for n in result['notes'] if n['id'] == 'essential')['pitch'], 40)
        self.assertTrue(all(a.get('essential') is not True for a in result['adaptations']))
        self.assertEqual(result['search']['cost']['essential_omissions'], 0)
        self.assertEqual(result['search']['cost']['essential_octave_semitones'], 0)

    def test_deterministic_and_no_source_mutation(self):
        notes = [Note('a', 0, 10000, 45), Note('b', 5000, 15000, 47)]
        first = self.plan(notes)
        second = self.plan(list(reversed(notes)))
        for key in ('notes', 'adaptations', 'search'):
            self.assertEqual(first[key], second[key])
        self.assertEqual(candidate.arrange(Performance('empty', 44100, []))['notes'], [])
        with self.assertRaises(ValueError): candidate.arrange(Performance('x', 44100, notes), True)
        with self.assertRaises(ValueError): candidate.arrange(Performance('x', 44100, notes * 2))
        with self.assertRaises(ValueError):
            candidate.arrange(Performance('x', 44100, [Note(str(i), i, i + 1, 64) for i in range(4001)]))


if __name__ == '__main__':
    unittest.main()
