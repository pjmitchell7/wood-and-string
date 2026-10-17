"""Silent authored policy-coherence, staggered-pattern and limit controls."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import instrumental_focus_alternatives as alternatives
import instrumental_interpreter as subject
from tests.test_instrumental_interpreter import context, moving, selected_keys


def line(pitches, shift=0):
    return [(start+offset+shift, start+offset+shift+4000, pitch)
            for offset in (0, 10000)
            for start, pitch in zip((1000, 3000, 6000), pitches)]


def policy_context(a, b):
    value = context(a)
    value['interpretations']['onset_only'] = context(b)['interpretations']['onset_only']
    value['coverage']['pitches'] = sorted({r[2] for r in a+b})
    return value


def groups(result):
    keys = {f['id']: (f['start_sample'], f['pitch']) for f in result['attack_families']}
    return {tuple(sorted(keys[key] for key in h['attack_ids']))
            for h in result['hypotheses'] if h['kind'] == 'recurring_moving_line'}


class PolicyAlternatives(unittest.TestCase):
    def preserved(self, value):
        before = deepcopy(value)
        result = subject.propose_focus(value)
        self.assertEqual(value, before)
        expected = sorted((p, subject._canonical(e))
                          for p, rows in value['interpretations'].items() for e in rows)
        retained = sorted((o['policy'], subject._canonical(o['event']))
                          for f in result['attack_families'] for o in f['observations'])
        self.assertEqual(retained, expected)
        partitions = [set(ids) for ids in result['ledger'].values()]
        self.assertEqual(sum(map(len, partitions)), len(result['attack_families']))
        self.assertEqual(set().union(*partitions), {f['id'] for f in result['attack_families']})
        by_id = {f['id']: (f['start_sample'], f['pitch']) for f in result['attack_families']}
        policy_keys = {p: {(e['start'], e['pitch']) for e in rows}
                       for p, rows in value['interpretations'].items()}
        for h in result['hypotheses']:
            self.assertTrue(h['supports'])
            self.assertEqual(set(h['supporting_policies']), {s['policy'] for s in h['supports']})
            for p in h['supporting_policies']:
                self.assertLessEqual({by_id[i] for i in h['attack_ids']}, policy_keys[p])
        return result

    def test_staggered_recurring_lines_keep_both_explanations(self):
        a = line((55, 58, 56))
        for shift in (0, 1, 27, 997, 2204):
            with self.subTest(shift=shift):
                b = line((67, 70, 68), shift)
                result = self.preserved(context(a+b))
                self.assertEqual(result['state'], 'competing_moving_lines')
                self.assertFalse(result['ledger']['selected'])
                for rows in (a, b):
                    self.assertIn(tuple(sorted((s, p) for s, _, p in rows)), groups(result))

    def test_incompatible_complete_policies_are_not_a_composite(self):
        a, b = line((55, 58, 56)), line((67, 70, 68), 1)
        result = self.preserved(policy_context(a, b))
        self.assertEqual(groups(result), {tuple((s, p) for s, _, p in a), tuple((s, p) for s, _, p in b)})
        self.assertFalse(result['ledger']['selected'])
        self.assertEqual(result['state'], 'competing_moving_lines')
        self.assertEqual(sorted(h['supporting_policies'] for h in result['hypotheses']),
                         [['onset_only'], ['published']])

    def test_one_occurrence_per_policy_cannot_manufacture_a_repeat(self):
        a = line((55, 58, 56))
        result = self.preserved(policy_context(a[:3], a[3:]))
        self.assertFalse(result['hypotheses'])
        self.assertFalse(result['ledger']['selected'])
        self.assertEqual(result['state'], 'no_unique_moving_focus')

    def test_one_policy_candidate_is_inspectable_but_not_agreed(self):
        a = moving()
        result = self.preserved(policy_context(a, a[:4]))
        self.assertEqual(result['state'], 'policy_disagreement')
        self.assertEqual(groups(result), {tuple((s, p) for s, _, p in a)})
        self.assertFalse(result['ledger']['selected'])
        self.assertEqual(result['hypotheses'][0]['supporting_policies'], ['published'])

    def test_clear_automatic_choice_and_distinct_fast_attacks_survive(self):
        a = [(start, start+9000, pitch) for start, pitch in
             ((1000, 60), (1001, 62), (1002, 64), (1003, 67),
              (11000, 60), (11001, 62), (11002, 64), (11003, 67))]
        result = self.preserved(context(a))
        self.assertEqual(result['state'], 'proposed')
        self.assertEqual(selected_keys(result), [(s, p) for s, _, p in a])
        self.assertEqual(len(result['attack_families']), 8)

    def test_sequential_register_sections_do_not_imply_overlapping_parts(self):
        a = line((55, 58, 56))
        b = line((67, 70, 68), 30000)
        result = self.preserved(context(a+b))
        self.assertFalse(any(s['method'] == 'pitch_cut_recurrence'
                             for h in result['hypotheses'] for s in h['supports']))

    def test_split_alternative_bound_defers_selection_without_loss(self):
        value = context(line((55, 58, 56))+line((67, 70, 68), 1))
        with patch.object(alternatives, 'MAX_SPLIT_COVERS_PER_POLICY', 0):
            result = self.preserved(value)
        self.assertEqual(result['state'], 'search_incomplete')
        self.assertIn('pitch_cut_cover_bound', result['search']['incomplete_reasons'])
        self.assertFalse(result['ledger']['selected'])
        self.assertEqual(len(result['ledger']['unresolved']), 12)


if __name__ == '__main__':
    unittest.main()
