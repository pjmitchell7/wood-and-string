"""Silent known-event planning regressions; no model, DLL or audio playback."""
import unittest
from audio.performance import Note, Performance, arrange, sampled_pitches


class Fingering(unittest.TestCase):
    def plan(self, notes, planner='phrase', performers=1):
        result = arrange(Performance('known-events', 44100, notes), performers, planner=planner)
        originals = {n.id: n for n in notes}
        coverage = sampled_pitches()
        for n in result['notes']:
            source = originals[n['id']]
            self.assertEqual((n['start'], n['end'], n['velocity']),
                             (source.start, source.end, source.velocity))
            self.assertIn(n['pitch'], coverage[n['string']])
        for a in result['notes']:
            for b in result['notes']:
                if a['id'] == b['id'] or a['player'] != b['player']: continue
                if a['start'] < b['end'] and b['start'] < a['end']:
                    self.assertNotEqual(a['string'], b['string'])
        return result

    def test_ringing_pair_avoids_greedy_octave(self):
        notes = [Note('a', 0, 10000, 45), Note('b', 5000, 15000, 47)]
        self.assertEqual(self.plan(notes, 'greedy')['notes'][1]['pitch'], 59)
        result = self.plan(notes)
        self.assertEqual([n['pitch'] for n in result['notes']], [45, 47])
        self.assertEqual(result['adaptations'], [])

    def test_three_note_horizon_is_needed(self):
        notes = [Note(str(i), i*5000, 20000+i*5000, pitch)
                 for i, pitch in enumerate([66, 49, 74])]
        self.assertEqual(self.plan(notes, 'lookahead')['notes'][-1]['pitch'], 62)
        result = self.plan(notes)
        self.assertEqual([n['pitch'] for n in result['notes']], [66, 49, 74])
        frets = [n['fret'] for n in result['notes'] if n['fret']]
        self.assertLessEqual(max(frets)-min(frets), 4)
        self.assertEqual(result['adaptations'], [])

    def test_touching_same_string_notes_keep_mute_and_reattack(self):
        notes = [Note('pluck-1', 0, 5000, 40), Note('pluck-2', 5000, 9000, 40)]
        result = self.plan(notes)
        self.assertEqual([n['id'] for n in result['notes']], ['pluck-1', 'pluck-2'])
        self.assertEqual([n['string'] for n in result['notes']], [0, 0])
        self.assertEqual(result['notes'], self.plan(notes, 'greedy')['notes'])

    def test_intentional_rest_is_not_filled(self):
        notes = [Note('a', 0, 1000, 45), Note('b', 5000, 9000, 47)]
        result = self.plan(notes)
        self.assertEqual(result['notes'][1]['start']-result['notes'][0]['end'], 4000)
        self.assertEqual(result['notes'], self.plan(notes, 'greedy')['notes'])

    def test_phrase_boundary_does_not_anticipate(self):
        notes = [Note('a', 0, 10000, 45, phrase='one'),
                 Note('b', 5000, 15000, 47, phrase='two')]
        self.assertEqual(self.plan(notes)['notes'], self.plan(notes, 'greedy')['notes'])

    def test_overlapping_repeated_pitch_is_not_merged(self):
        notes = [Note('a', 0, 10000, 64), Note('b', 5000, 15000, 64)]
        result = self.plan(notes)
        self.assertEqual(len(result['notes']), 2)
        self.assertEqual([n['pitch'] for n in result['notes']], [64, 64])

    def test_melody_priority_and_duet_roles_remain(self):
        notes = [Note('melody', 5000, 15000, 64), Note('bass', 0, 20000, 40, role='bass')]
        result = self.plan(notes, performers=2)
        self.assertEqual({n['id']: n['player'] for n in result['notes']}, {'bass': 2, 'melody': 1})

    def test_deterministic_and_unknown_planner_rejected(self):
        notes = [Note('a', 0, 10000, 45), Note('b', 5000, 15000, 47)]
        self.assertEqual(self.plan(notes), self.plan(notes))
        for field in ('notes', 'adaptations'):
            self.assertEqual(self.plan(notes)[field], self.plan(list(reversed(notes)))[field])
        self.assertEqual(arrange(Performance('known-events', 44100, notes)),
                         self.plan(notes, 'greedy'))
        with self.assertRaises(ValueError): self.plan(notes, 'unknown')

    def test_note_limit(self):
        notes = [Note(str(i), i*1000, i*1000+500, 64) for i in range(4001)]
        with self.assertRaises(ValueError): self.plan(notes)


if __name__ == '__main__': unittest.main()
