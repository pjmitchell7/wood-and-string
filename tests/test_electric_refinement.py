"""Real Electric refinement preparation with private synthetic render evidence.

No analyzer or renderer is stubbed into the preparation path; neither runs.
Synthetic PCM validates only persistence/identity plumbing, not music quality.
"""
import copy
from dataclasses import asdict
import json
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from audio.performance import Note, Performance
from audio.preview_instruments import arrange_selected, renderer_version, selection_fields
from audio.electric_preview_render import RENDER_VERSION as WRAPPER_VERSION
from audio.electric_render import RENDER_VERSION as PROTOTYPE_VERSION
from library import Library
from preview_refinement import prepare_refinement, arrange_refinement, result_digest
from render_evidence import RenderEvidence
from tests.electric_render_fixture import write_synthetic_render


def plain(value):
    return json.loads(json.dumps(value, allow_nan=False))


class ElectricRefinementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Library(self.temp.name)
        self.store = RenderEvidence(self.library)
        self.origin = 5336117  # Deliberately beyond the planner's zero-origin 30s window.
        staged = self.library.stage/(str(uuid.uuid4())+'.partial')
        staged.write_bytes(b'Preserved synthetic original bytes; never decoded or played.')
        self.source = self.library.publish(staged, 'synthetic-original.wav', 'audio/wav')
        self.performance = Performance(self.source['source_id'], 44100, [
            Note('m-a', self.origin+10, self.origin+90, 64, 70, confidence=.8,
                 phrase='source-phrase', contour=((self.origin+20, 64.1),)),
            Note('m-b', self.origin+120, self.origin+220, 64, 100, confidence=.4,
                 phrase='source-phrase', contour=((self.origin+180, 63.9),)),
            Note('m-c', self.origin+250, self.origin+350, 67, 85, phrase='source-phrase'),
            Note('b-a', self.origin, self.origin+400, 40, 80, role='bass')],
            analysis_offset_samples=0, beats=[{'sample': self.origin+10, 'uncertainty': [.2, .4]}])

    def publish_parent(self, arrangement, request):
        identity = str(uuid.uuid4())
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity, 'running', 0, json.dumps(request), None, None, 1))
        # Exact real store + media + job commit sequence under worker ownership.
        with self.library.exclusive('worker.lock'):
            files = write_synthetic_render(self.library, identity, arrangement)
            reference = self.store.publish(files['raw_path'], files['receipt_path'], wav_path=files['wav_path'],
                                           arrangement=arrangement)
            asset = self.library.publish(files['wav_path'], 'synthetic-electric.wav', 'audio/wav',
                                         kind='synthetic-electric-'+request['kind'], source_id=self.source['source_id'])
            result = {'asset': asset, 'arrangement': arrangement, 'render_evidence': reference,
                      'metrics': {'renderer_version': WRAPPER_VERSION, 'dry_adapter_version': PROTOTYPE_VERSION,
                                  'musical_acceptance': False}}
            self.library.update_job(identity, 'complete', 1, result)
        return self.library.job(identity)

    def mixed_parent(self, performers=1):
        selection = selection_fields({'instrument': 'electric'}, performers)
        arrangement = arrange_selected(self.performance, performers, None, selection, render_origin_sample=self.origin)
        self.assertTrue(arrangement['renderable'])
        request = {'kind': 'mixed', 'asset_id': self.source['id'], 'sha256': self.source['sha256'],
                   'start_seconds': self.origin/44100, 'end_seconds': (self.origin+1000)/44100,
                   'performers': performers, 'renderer_version': renderer_version(selection), **selection}
        return self.publish_parent(arrangement, request)

    def assert_original_preserved(self, before):
        self.assertEqual(self.library.get(self.source['id']), before['asset'])
        self.assertTrue(self.library.verify(self.source['id']))
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(), before['bytes'])

    def test_nonzero_origin_join_uses_actual_planner_and_preserves_original_analysis(self):
        parent = self.mixed_parent()
        before = copy.deepcopy(parent)
        source_before = {'asset': dict(self.source), 'bytes': (self.library.media/self.source['sha256']).read_bytes()}
        prepared = prepare_refinement(self.library, parent['id'], [{'kind': 'join', 'note_ids': ['m-b', 'm-a']}])
        arrangement = arrange_refinement(prepared)
        self.assertEqual(prepared['origin'], self.origin)
        self.assertEqual(prepared['spec']['renderer_version'], WRAPPER_VERSION)
        self.assertEqual(prepared['spec']['parent_result_sha256'], result_digest(parent['result']))
        self.assertEqual(arrangement['render_origin_sample'], self.origin)
        self.assertTrue(arrangement['renderable'])
        self.assertEqual(arrangement['instrument'], 'electric')
        self.assertEqual(plain(arrangement['source']), parent['result']['arrangement']['source'])
        self.assertEqual(plain(arrangement['refinement_source']), plain(asdict(prepared['corrected'])))
        self.assertEqual(arrangement['source']['analysis_offset_samples'], 0)
        self.assertEqual(arrangement['source']['beats'], self.performance.beats)
        joined = next(note for note in arrangement['notes'] if note['id'].startswith('joined-'))
        self.assertEqual((joined['start'], joined['end'], joined['velocity'], joined['confidence']),
                         (self.origin+10, self.origin+220, 70, .4))
        self.assertEqual(joined['contour'], ((self.origin+20, 64.1), (self.origin+180, 63.9)))
        ledger = arrangement['refinement']
        self.assertEqual(ledger['evidence'], 'Explicit user edits; not new analyzer observations')
        self.assertEqual(ledger['passage_start'], self.origin)
        self.assertEqual(ledger['edits'][0]['unobserved_gaps'], [{'start': self.origin+90, 'end': self.origin+120}])
        self.assertEqual(ledger['edits'][0]['members'], [asdict(note) for note in self.performance.notes[:2]])
        self.assertNotEqual(plain(arrangement['refinement_source']), plain(arrangement['source']))
        self.assertEqual(self.library.job(parent['id']), before)
        self.assert_original_preserved(source_before)
        # Returned evidence and edit metadata are detached from the saved parent.
        arrangement['source']['beats'][0]['uncertainty'].append(9)
        arrangement['refinement']['edits'][0]['members'][0]['velocity'] = 1
        self.assertEqual(self.library.job(parent['id']), before)

    def test_refine_parent_keeps_original_source_separate_from_two_successive_edits(self):
        mixed = self.mixed_parent()
        first = prepare_refinement(self.library, mixed['id'], [{'kind': 'join', 'note_ids': ['m-a', 'm-b']}])
        revision = self.publish_parent(arrange_refinement(first), first['spec'])
        before = copy.deepcopy(revision)
        second = prepare_refinement(self.library, revision['id'], [{'kind': 'omit', 'note_ids': ['m-c']}])
        arrangement = arrange_refinement(second)
        self.assertEqual(plain(asdict(second['original'])), mixed['result']['arrangement']['source'])
        self.assertEqual(plain(asdict(second['current'])), revision['result']['arrangement']['refinement_source'])
        self.assertEqual(plain(arrangement['source']), mixed['result']['arrangement']['source'])
        self.assertEqual(len(arrangement['source']['notes']), 4)
        self.assertEqual(len(arrangement['refinement_source']['notes']), 2)
        self.assertEqual([entry['kind'] for entry in arrangement['refinement']['edits']], ['omit'])
        self.assertEqual(arrangement['refinement']['parent_job_id'], revision['id'])
        self.assertEqual(arrangement['render_origin_sample'], self.origin)
        self.assertEqual(second['spec']['renderer_version'], WRAPPER_VERSION)
        self.assertEqual(self.library.job(revision['id']), before)

    def test_missing_or_corrupt_raw_and_receipt_refuse_preparation_before_render(self):
        for kind in ('raw', 'receipt'):
            for change in ('missing', 'corrupt'):
                with self.subTest(kind=kind, change=change):
                    parent = self.mixed_parent()
                    before = copy.deepcopy(parent)
                    checked = self.store.verify(parent['result']['render_evidence'],
                                                wav_sha256=parent['result']['asset']['sha256'],
                                                arrangement=parent['result']['arrangement'])
                    path = checked['raw_path' if kind == 'raw' else 'receipt_path']
                    original = path.read_bytes()
                    if change == 'missing': path.unlink()
                    else: path.write_bytes(b'corrupt private evidence')
                    try:
                        with patch('audio.electric_preview_render.render', side_effect=AssertionError('Rendering forbidden')) as render:
                            with self.assertRaisesRegex(ValueError, 'Electric dry evidence failed verification'):
                                prepare_refinement(self.library, parent['id'], [{'kind': 'omit', 'note_ids': ['m-c']}])
                            render.assert_not_called()
                        self.assertEqual(self.library.job(parent['id']), before)
                        self.assertTrue(self.library.verify(self.source['id']))
                    finally:
                        path.write_bytes(original)

    def test_duo_must_keep_observed_melody_and_foundation_roles(self):
        parent = self.mixed_parent(2)
        for ids in (['b-a'], ['m-a', 'm-b', 'm-c']):
            with self.subTest(removed=ids), self.assertRaisesRegex(ValueError, 'each selected Electric part'):
                prepare_refinement(self.library, parent['id'], [{'kind': 'omit', 'note_ids': ids}])
        prepared = prepare_refinement(self.library, parent['id'], [{'kind': 'join', 'note_ids': ['m-a', 'm-b']}])
        arrangement = arrange_refinement(prepared)
        self.assertEqual(arrangement['performers'], 2)
        self.assertTrue(arrangement['renderable'])
        self.assertEqual({note['role'] for note in arrangement['notes']}, {'melody', 'bass'})
        self.assertEqual({note['player'] for note in arrangement['notes']}, {1, 2})
        self.assertTrue(all(note['player'] == (1 if note['role'] == 'melody' else 2) for note in arrangement['notes']))
        self.assertEqual(arrangement['render_origin_sample'], self.origin)

    def test_cached_preview_hash_cannot_validate_a_different_valid_catalog_wav(self):
        parent = self.mixed_parent()
        original_before = (self.library.media/self.source['sha256']).read_bytes()
        other_path = self.library.stage/(str(uuid.uuid4())+'.wav')
        with wave.open(str(other_path), 'wb') as stream:
            stream.setparams((2, 2, 44100, 2, 'NONE', 'not compressed'))
            stream.writeframes(b'\0'*8)
        with self.library.exclusive('worker.lock'):
            other = self.library.publish(other_path, 'different-valid-preview.wav', 'audio/wav',
                                         kind='synthetic-other-electric', source_id=self.source['source_id'])
            damaged = copy.deepcopy(parent['result'])
            # Only the cached ID changes: cached SHA and source metadata still
            # describe the original WAV and its valid private evidence pair.
            damaged['asset']['id'] = other['id']
            self.library.update_job(parent['id'], 'complete', 1, damaged)
        self.assertTrue(self.library.verify(other['id']))
        self.assertNotEqual(other['sha256'], damaged['asset']['sha256'])
        self.assertEqual(other['source_id'], damaged['asset']['source_id'])
        # The retained pair itself is still sound. The rejection must come
        # from binding to the actual catalog asset, not corrupted evidence.
        self.store.verify(damaged['render_evidence'], wav_sha256=damaged['asset']['sha256'],
                          arrangement=damaged['arrangement'])
        before = self.library.job(parent['id'])
        with patch('audio.electric_preview_render.render', side_effect=AssertionError('Rendering forbidden')) as render:
            with self.assertRaisesRegex(ValueError, 'does not match its stored audio asset'):
                prepare_refinement(self.library, parent['id'], [{'kind': 'omit', 'note_ids': ['m-c']}])
            render.assert_not_called()
        self.assertEqual(self.library.job(parent['id']), before)
        self.assertTrue(self.library.verify(other['id']))
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(), original_before)


if __name__ == '__main__':
    unittest.main()
