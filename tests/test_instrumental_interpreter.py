"""Silent authored contract, ambiguity, cancellation and bounded-search checks."""
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import instrumental_interpreter as interpreter


def context(rows):
    inventories = {}
    for policy in ('published', 'onset_only'):
        inventories[policy] = [dict(id=f'{policy}-{i}', start=start, end=end, pitch=pitch,
                                    activation=.3, detail={'uninterpreted': ['retained']})
                               for i, (start, end, pitch) in enumerate(rows)]
    return {'version': 'instrumental-focus-input-v1',
        'source': {'id': 'authored', 'sha256': 'a'*64, 'sample_rate': 44100, 'origin': 0, 'end': 100000},
        'scope': {'start': 0, 'end': 100000}, 'interpretations': inventories,
        'coverage': {'start': 0, 'end': 100000, 'pitches': sorted({r[2] for r in rows}),
                     'left_censored': False, 'right_censored': False},
        'binding': {'authority': 'authored, no media or archive association'}}


def moving():
    return [(offset+t, offset+t+700, pitch) for offset in (0, 20000)
            for t, pitch in ((1500, 63), (3500, 65), (7200, 62), (9000, 68))]


def selected_keys(result, name='selected'):
    families = {f['id']: (f['start_sample'], f['pitch']) for f in result['attack_families']}
    return sorted(families[key] for key in result['ledger'][name])


class Proposer(unittest.TestCase):
    def test_polyphonic_focus_and_exact_omission_ledger(self):
        line = moving()
        chords = [(t, t+1800, p) for t in range(0, 40000, 5000) for p in (42, 49, 54)]
        value = context(chords+line)
        result = interpreter.propose_focus(value)
        self.assertEqual(result['state'], 'proposed')
        self.assertEqual(selected_keys(result), sorted((s, p) for s, _, p in line))
        self.assertEqual(selected_keys(result, 'secondary'), sorted((s, p) for s, _, p in chords))
        self.assertEqual(result['unchanged']['duration_repairs'], 0)
        self.assertEqual(len(result['attack_families']), len(line)+len(chords))
        self.assertEqual(len(result['path_covers'][0]), 1)

    def test_aliases_and_gate_variants_do_not_become_extra_attacks(self):
        value = context(moving())
        alternative = value['interpretations']['onset_only'][1]
        alternative['end'] += 2000
        alias = deepcopy(value['interpretations']['published'][0]); alias['id'] = 'alias'
        value['interpretations']['published'].append(alias)
        original = deepcopy(value)
        result = interpreter.propose_focus(value)
        self.assertEqual(value, original)
        self.assertEqual(len(result['attack_families']), 8)
        family = next(f for f in result['attack_families'] if f['start_sample'] == 3500)
        self.assertEqual([r['end_sample'] for r in family['gate_variants']], [4200, 6200])
        self.assertEqual(family['boundary_state'], 'competing_decoder_endings')
        self.assertEqual(len(selected_keys(result)), 8)
        result['attack_families'][0]['observations'][0]['event']['detail']['uninterpreted'].append('changed output')
        self.assertEqual(value, original)

    def test_two_distinct_moving_hooks_remain_alternatives(self):
        rows = moving(); value = context(rows+[(s, e, p+12) for s, e, p in rows])
        result = interpreter.propose_focus(value)
        self.assertEqual(result['state'], 'competing_moving_lines')
        self.assertEqual(result['ledger']['selected'], [])
        groups = [h for h in result['hypotheses'] if h['kind'] == 'recurring_moving_line']
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(len(result['ledger']['unresolved']), 16)

    def test_order_ids_activation_and_register_cannot_choose_a_hook(self):
        value = context(moving()); expected = selected_keys(interpreter.propose_focus(value))
        changed = deepcopy(value)
        for policy, events in changed['interpretations'].items():
            for i, event in enumerate(events):
                event['id'] = f'opaque-{policy}-{100-i}'; event['activation'] = .99
                event['pitch'] += 7
            events.reverse()
        changed['coverage']['pitches'] = [p+7 for p in changed['coverage']['pitches']]
        actual = selected_keys(interpreter.propose_focus(changed))
        self.assertEqual(actual, [(s, p+7) for s, p in expected])

    def test_identical_chords_do_not_fabricate_a_woven_melody(self):
        value = context([(t, t+1800, p) for t in range(0, 40000, 5000) for p in (48, 52, 55)])
        result = interpreter.propose_focus(value)
        self.assertFalse(result['ledger']['selected'])
        self.assertFalse(result['hypotheses'])
        self.assertEqual(len(result['ledger']['unresolved']), 24)

    def test_distinct_repeat_candidates_and_identical_rest_dropout_evidence(self):
        value = context([(s, s+7000, 60) for s in (1000, 3100, 6400, 10800, 21000, 23100, 26400, 30800)])
        value['binding']['cells'] = [0., .8, .8, 0., 0., .8]
        result = interpreter.propose_focus(value)
        self.assertEqual(len(result['attack_families']), 8)
        self.assertFalse(result['ledger']['selected'])
        self.assertTrue(any(h['kind'] == 'recurring_single_pitch_rhythm' for h in result['hypotheses']))
        self.assertEqual(result['trace_coverage']['physical_rest_or_dropout'], 'unresolved')
        self.assertEqual(result, interpreter.propose_focus(deepcopy(value)))

    def test_incomplete_coverage_censoring_and_scope_stay_explicit(self):
        value = context(moving()+[(50000, 52000, 79)])
        value['scope']['end'] = 40000
        value['coverage'].update(pitches=[63], end=30000, left_censored=True)
        result = interpreter.propose_focus(value)
        self.assertEqual(result['state'], 'proposed')
        self.assertFalse(result['trace_coverage']['scope_covered'])
        self.assertTrue(result['trace_coverage']['context_censored'])
        self.assertEqual(result['trace_coverage']['missing_pitches'], [62, 65, 68])
        self.assertEqual(selected_keys(result, 'outside_scope'), [(50000, 79)])
        self.assertEqual(result['source_evidence']['binding'], value['binding'])

    def test_invalid_or_prelabelled_evidence_is_rejected_without_truncation(self):
        for kind in ('duplicate_id', 'nonfinite', 'role', 'bool_clock', 'outside', 'rate', 'oversized', 'unicode', 'numeric_key', 'tuple'):
            with self.subTest(kind=kind):
                value = context(moving()); event = value['interpretations']['published'][0]
                if kind == 'duplicate_id': value['interpretations']['published'][1]['id'] = event['id']
                elif kind == 'nonfinite': event['activation'] = float('nan')
                elif kind == 'role': event['role_hint'] = 'main'
                elif kind == 'bool_clock': event['start'] = True
                elif kind == 'outside': event['end'] = 100001
                elif kind == 'rate': value['source']['sample_rate'] = 16000
                elif kind == 'oversized': value['binding']['padding'] = 'x'*(513*1024)
                elif kind == 'unicode': value['binding']['invalid'] = '\ud800'
                elif kind == 'numeric_key': value['binding'][1] = 'non-JSON key'
                else: value['binding']['tuple'] = (1, 2)
                with self.assertRaises(ValueError): interpreter.propose_focus(value)

    def test_empty_inventory_and_explicit_small_motif_limitation(self):
        result = interpreter.propose_focus(context([]))
        self.assertFalse(result['attack_families'])
        self.assertEqual(result['state'], 'no_unique_moving_focus')
        value = context([(1000, 2000, 60), (3000, 4000, 64), (21000, 22000, 60), (23000, 24000, 64)])
        self.assertFalse(interpreter.propose_focus(value)['ledger']['selected'])

    def test_search_exhaustion_defers_focus_and_preserves_every_observation(self):
        value = context(moving())
        with patch.object(interpreter, 'MAX_ASSIGNMENTS', 1): result = interpreter.propose_focus(value)
        self.assertEqual(result['state'], 'search_incomplete')
        self.assertEqual(result['ledger']['selected'], [])
        self.assertEqual(len(result['attack_families']), 8)
        self.assertIn('assignments_bound', result['search']['incomplete_reasons'])
        with patch.object(interpreter, 'MAX_MOTIF_WINDOWS', 1): result = interpreter.propose_focus(value)
        self.assertEqual(result['state'], 'search_incomplete')
        self.assertFalse(result['ledger']['selected'])
        busy = context([(1000, 2000, p) for p in range(40, 49)])
        result = interpreter.propose_focus(busy)
        self.assertEqual(result['state'], 'search_incomplete')
        self.assertEqual(len(result['ledger']['unresolved']), 9)

    def test_cancellation_before_and_during_computation(self):
        with self.assertRaises(ValueError): interpreter.propose_focus(context(moving()), cancel=None)
        with self.assertRaises(InterruptedError): interpreter.propose_focus(context(moving()), cancel=lambda: True)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 30
        with self.assertRaises(InterruptedError): interpreter.propose_focus(context(moving()), cancel=cancel)
        self.assertEqual(calls, 30)

    def test_large_valid_input_cannot_expand_into_an_unbounded_output(self):
        value = context([(i*100, i*100+50, 60) for i in range(256)])
        for policy, events in value['interpretations'].items():
            for i, event in enumerate(events):
                event['id'] = f'{policy}-{i}-' + 'x'*100
                event['detail'] = {'padding': 'x'*309}
            aliases = deepcopy(events)
            for event in aliases: event['id'] = 'a'+event['id']
            events.extend(aliases)
        original = deepcopy(value)
        # This meets the input/family/observation bounds. Its repeated source
        # references expand the complete output beyond the retained ceiling.
        self.assertLess(len(interpreter._canonical(value)), interpreter.MAX_INPUT_BYTES)
        with self.assertRaisesRegex(ValueError, 'output bound'):
            interpreter.propose_focus(value)
        self.assertEqual(value, original)


class SavedFocus(unittest.TestCase):
    """Authored SQLite/source/NPZ fixtures, including real protected reads."""
    def setUp(self):
        import instrumental_phrase as phrase
        from library import Library
        from tests.test_instrumental_phrase import fixture
        self.phrase = phrase
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        phase = Path(__file__).resolve().parents[1]/'work/acoustic-parts-integration-001/interpretation-001/saved-loader-001'
        phase.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=phase, prefix='authored-')))
        self.library = Library(self.root/'library')
        self.bundle, self.arrays, _, self.raw = fixture()
        self.anchors = phrase.source_anchors(self.bundle['origin'], self.bundle['end'])
        for policy, report in self.bundle['interpretations'].items():
            template = report['events'][0]
            for index in range(12):
                first = 5+3*index; last = first+12
                report['events'].append({**deepcopy(template), 'id': f'extra-{policy}-{index}',
                    'start': self.anchors[first], 'end': self.anchors[last], 'pitch': 30+index,
                    'start_frame': first, 'end_frame_exclusive': last})
            report['events'].sort(key=lambda e: (e['start'], e['pitch']))
        staged = self.library.stage/'authored'; staged.write_bytes(b'original')
        self.source = self.library.publish(staged, 'authored.wav', 'audio/wav')
        self.bundle.update(source_id=self.source['source_id'], source_sha256=self.source['sha256'])
        self.identity = str(uuid.uuid4())
        self.spec = dict(kind='instrumental', asset_id=self.source['id'], sha256=self.source['sha256'],
                         start_seconds=self.bundle['origin']/44100, end_seconds=self.bundle['end']/44100)
        folder = self.library.root/'analysis-evidence'; folder.mkdir()
        self.archive = folder/self.bundle['raw_activations']['sha256']; self.archive.write_bytes(self.raw)
        self.result = {'candidates': self.bundle, 'evidence': {'sha256': self.archive.name, 'bytes': len(self.raw), 'format': 'numpy-npz-v1'}}
        self.scope = {'start_sample': self.bundle['origin'], 'end_sample': self.bundle['end']}
        self.save()
        self.sentinel = self.root/'unrelated'; self.sentinel.write_bytes(b'preserve')

    def save(self, state='complete'):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                (self.identity, state, 1., json.dumps(self.spec), json.dumps(self.result), None, 123.))
        self.parent_hash = hashlib.sha256(self.phrase.parent_canonical(self.result)).hexdigest()

    def load(self, **changes):
        return interpreter.load_focus_context(self.library, self.identity, self.parent_hash,
                                               changes.pop('scope', self.scope), **changes)

    def test_complete_saved_context_automatic_all_pitch_batches_and_zero_writes(self):
        database = self.library.db.read_bytes(); source = (self.library.media/self.source['sha256']).read_bytes()
        before = deepcopy(self.result); requests = []; reports = []
        reader = self.phrase.load_phrase_boundary_evidence
        def observed(*args, **kwargs):
            requests.append(deepcopy(args[3])); report = reader(*args, **kwargs)
            reports.append(deepcopy(report)); return report
        with patch.object(self.phrase, 'load_phrase_boundary_evidence', side_effect=observed):
            context_value = self.load()
        expected = sorted({e['pitch'] for p in self.bundle['interpretations'].values() for e in p['events']})
        self.assertEqual([p for r in requests for p in r['pitches']], expected)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(1 <= len(r['pitches']) <= 8 and r['context_frames'] == 2 for r in requests))
        self.assertEqual(context_value['interpretations'], {p: r['events'] for p, r in self.bundle['interpretations'].items()})
        binding = context_value['binding']
        self.assertEqual(binding['authority'], 'verified_saved_focus_collection')
        self.assertEqual(binding['parent']['result_sha256'], self.parent_hash)
        for receipt, report in zip(binding['collection_reports'], reports):
            self.assertEqual(receipt['report_sha256'], hashlib.sha256(interpreter._canonical(report)).hexdigest())
            self.assertEqual(receipt['trace_pitches'], report['request']['pitches'])
        self.assertEqual(context_value['coverage']['pitches'], expected)
        self.assertTrue(context_value['coverage']['left_censored'])
        self.assertTrue(context_value['coverage']['right_censored'])
        actual = interpreter.propose_focus(context_value)
        self.assertEqual(actual, interpreter.load_focus_proposal(self.library, self.identity, self.parent_hash, self.scope))
        self.assertEqual(self.library.db.read_bytes(), database)
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(), source)
        self.assertEqual(self.archive.read_bytes(), self.raw)
        self.assertEqual(self.result, before); self.assertEqual(self.sentinel.read_bytes(), b'preserve')
        self.assertFalse(any(Path(str(self.library.db)+suffix).exists() for suffix in ('-wal', '-shm', '-journal')))

    def test_scope_keeps_complete_outside_inventory_without_manual_pitch_selection(self):
        scope = {'start_sample': self.anchors[45], 'end_sample': self.anchors[82]}
        value = self.load(scope=scope)
        expected = sorted({e['pitch'] for report in self.bundle['interpretations'].values() for e in report['events']
                           if e['start'] < scope['end_sample'] and e['end'] > scope['start_sample']})
        self.assertEqual(value['coverage']['pitches'], expected)
        self.assertEqual(sum(map(len, value['interpretations'].values())), 36)
        result = interpreter.propose_focus(value)
        self.assertTrue(result['ledger']['outside_scope'])
        self.assertEqual(len(result['attack_families']), 18)

    def test_untrusted_hash_state_scope_or_identity_stops_before_trace_reads(self):
        with patch.object(self.phrase, 'load_phrase_boundary_evidence') as reader:
            for identity, checksum, scope in ((self.identity, '0'*64, self.scope),
                    ('../outside', self.parent_hash, self.scope),
                    (self.identity, self.parent_hash, {**self.scope, 'start_sample': True}),
                    (self.identity, self.parent_hash, {**self.scope, 'end_sample': self.bundle['end']+1})):
                with self.subTest(identity=identity, scope=scope), self.assertRaises(ValueError):
                    interpreter.load_focus_context(self.library, identity, checksum, scope)
            self.save('running')
            with self.assertRaises(ValueError): self.load()
            reader.assert_not_called()

    def test_source_and_archive_tampering_cannot_create_a_context(self):
        source = self.library.media/self.source['sha256']; source.write_bytes(b'changed!')
        with self.assertRaises(ValueError): self.load()
        source.write_bytes(b'original')
        self.archive.write_bytes(self.raw[:-1]+b'X')
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.sentinel.read_bytes(), b'preserve')

    def test_missing_trace_changed_matrix_identity_and_bad_geometry_refuse(self):
        reader = self.phrase.load_phrase_boundary_evidence
        for kind in ('trace', 'matrix', 'geometry', 'event'):
            calls = 0
            def tampered(*args, **kwargs):
                nonlocal calls
                calls += 1; result = reader(*args, **kwargs)
                if calls == 2:
                    if kind == 'trace': result['traces'].pop(next(iter(result['traces'])))
                    elif kind == 'matrix': result['binding']['array_sha256']['note'] = '0'*64
                    elif kind == 'geometry': result['frames']['samples'][0] += 1
                    else: result['interpretations']['published']['events'].pop()
                return result
            with self.subTest(kind=kind), patch.object(self.phrase, 'load_phrase_boundary_evidence', side_effect=tampered), self.assertRaises(ValueError):
                self.load()

    def test_parent_change_between_batches_refuses_composite_proposal(self):
        reader = self.phrase.load_phrase_boundary_evidence; changed = False
        def concurrent_update(*args, **kwargs):
            nonlocal changed
            result = reader(*args, **kwargs)
            if not changed:
                with self.library.connect() as db: db.execute('UPDATE jobs SET progress=.5 WHERE id=?', (self.identity,))
                changed = True
            return result
        with patch.object(self.phrase, 'load_phrase_boundary_evidence', side_effect=concurrent_update), self.assertRaisesRegex(ValueError, 'parent changed'):
            self.load()

    def test_cancellation_between_batches_preserves_files_and_database(self):
        database = self.library.db.read_bytes(); calls = 0; reader = self.phrase.load_phrase_boundary_evidence
        def observed(*args, **kwargs):
            nonlocal calls
            result = reader(*args, **kwargs); calls += 1; return result
        with patch.object(self.phrase, 'load_phrase_boundary_evidence', side_effect=observed), self.assertRaises(InterruptedError):
            self.load(cancel=lambda: calls >= 1)
        self.assertEqual(calls, 1)
        self.assertEqual(self.library.db.read_bytes(), database)
        self.assertEqual(self.archive.read_bytes(), self.raw)
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(), b'original')

    def test_empty_overlap_refuses_without_inventing_a_verification_pitch(self):
        scope = {'start_sample': self.anchors[150], 'end_sample': self.anchors[160]}
        with patch.object(self.phrase, 'load_phrase_boundary_evidence') as reader, self.assertRaisesRegex(ValueError, 'No observed pitch'):
            self.load(scope=scope)
        reader.assert_not_called()


if __name__ == '__main__': unittest.main()
