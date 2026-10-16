"""Private evidence durability controls using synthetic bytes only, never audio."""
import array
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from library import Library
import render_evidence as evidence


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RenderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Library(self.temp.name)
        self.store = evidence.RenderEvidence(self.library)
        self.identity = str(uuid.uuid4())
        self.job(self.identity)
        self.wav = self.library.stage/(self.identity+'.wav')
        self.raw = self.library.stage/(self.identity+'.wav.dry.f32')
        self.receipt_path = self.library.stage/(self.identity+'.wav.receipt.json')
        origin = 5336117
        self.arrangement = {'sample_rate': 44100, 'render_origin_sample': origin, 'performers': 1, 'notes': [
            {'id': 'observed', 'start': origin+1, 'end': origin+17, 'pitch': 64,
             'velocity': 127, 'player': 1, 'string': 5, 'fret': 0, 'articulation': evidence.ARTICULATION,
             'contour': ((origin+2, 64.1),)}]}
        frames = 44117
        floats = array.array('f', [.125, -.0625])*frames
        if sys.byteorder != 'little': floats.byteswap()
        self.raw.write_bytes(floats.tobytes())
        gain = 10**(-3/20)/.125
        left, right = round(.125*gain*32768), round(-.0625*gain*32768)
        with wave.open(str(self.wav), 'wb') as stream:
            stream.setparams((2, 2, 44100, frames, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<hh', left, right)*frames)
        commands = [{key: note[key] for key in evidence.COMMAND_KEYS} for note in self.arrangement['notes']]
        bank = {'profile_version': evidence.PROFILE_VERSION, 'manifest_sha256': dict(evidence.PINS),
                'mapping_sha256': 'a'*64, 'bank_files_verified': 331, 'source_wav_git_identities_verified': 323,
                'wav_headers_verified': 323, 'integrity_checked': True, 'selected_attack_regions': 64,
                'selected_release_regions': 16, 'mode_enabled': False,
                'scope': 'Pinned bank and observed static engine closure; no sound-quality or OS attestation.',
                'native': [{'path': f'C:/synthetic/never-loaded-{i}.dll', 'bytes': i+1,
                            'sha256': digest(str(i).encode()), 'held_read_share_only': i == 0} for i in range(4)]}
        self.receipt = {
            'renderer_version': evidence.RENDER_VERSION, 'profile_version': evidence.PROFILE_VERSION,
            'instrument': 'Epiphone Emily the Strange direct electric samples', 'samples': 'Karoryfer Emilyguitar v1.001',
            'frames': frames, 'sample_rate': 44100, 'source_origin_sample': origin,
            'peak': max(abs(left), abs(right))/32768, 'raw_peak': .125, 'gain_db': 20*math.log10(gain),
            'level_policy': 'peak', 'level_measurement': 'sample_peak_not_lufs_or_true_peak',
            'target_sample_peak_dbfs': -3, 'clipped_samples': 0, 'quantization': 'PCM16 nearest, no dither',
            'source_commands': commands, 'source_commands_sha256': digest(evidence._canonical(commands)),
            'source_gates_pitch_velocity_changed': False, 'command_transpose_semitones': 0,
            'instances': 1, 'quality_by_instance': [{'sample': 2, 'oscillator': 1}],
            'processing_mode': 'freewheeling', 'preload_floats_per_channel': evidence.PRELOAD,
            'raw_float': {'file': self.raw.name, 'bytes': frames*8, 'sha256': digest(self.raw.read_bytes()),
                          'sample_peak': .125, 'rms': math.sqrt((.125**2+.0625**2)/2),
                          'format': 'stereo little-endian float32 interleaved',
                          'stage': 'dry adapter output after declared string damping, before constant gain'},
            'bank': bank, 'sample_limits': [{'note_id': 'observed', 'region_id': 'basic-001', 'sample': 'notes/synthetic_rr1.wav',
                'stored_frames': 100, 'sample_root_midi': 64, 'transposed_stored_frames_estimate': 100.,
                'gate_exceeds_stored_sample_estimate': False, 'limit': evidence.LIMIT_TEXT}],
            'round_robin_policy': 'fixed first recorded variation only', 'random_amplitude': 0,
            'dynamics': 'Author velocity layers/curves retained; uncalibrated, measured energy inversions remain.',
            'same_string_choke': 'At most5ms half-cosine outgoing output fade followed by exact-onset all_sound_off',
            'maximum_reattack_damping_seconds': .005, 'render_tail_seconds': 1,
            'effects': [], 'loops_added': False, 'original_audio_mixed': False, 'contours_not_rendered': ['observed'],
            'physical_assumptions': {'open_midi': list(evidence.OPEN), 'max_fret': evidence.MAX_FRET,
                                     'source_string_identity_known': False, 'fingering_biomechanics_verified': False},
            'native_adapter_acceptance': False, 'musical_acceptance': False, 'app_mode_enabled': False,
            'wav_sha256': digest(self.wav.read_bytes())}
        self.save_receipt()

    def job(self, identity, result=None, state='running'):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity, state, 0, '{}', json.dumps(result) if result else None, None, 1))

    def save_receipt(self):
        # Noncanonical whitespace verifies that publication preserves exact
        # original receipt bytes instead of reserializing its parsed object.
        self.receipt_path.write_bytes(json.dumps(self.receipt, sort_keys=True, indent=2).encode()+b'\n')

    def publish(self, **changes):
        with self.library.exclusive('worker.lock'):
            return self.store.publish(changes.pop('raw_path', self.raw), changes.pop('receipt_path', self.receipt_path),
                                      wav_path=changes.pop('wav_path', self.wav), arrangement=changes.pop('arrangement', self.arrangement), **changes)

    def reconcile(self, **changes):
        with self.library.exclusive('worker.lock'):
            return evidence.reconcile_render_evidence(self.library, **changes)

    def test_exact_publication_and_private_reverification_preserve_inputs_and_scope(self):
        before = {path: path.read_bytes() for path in (self.raw, self.wav, self.receipt_path)}
        arrangement = copy.deepcopy(self.arrangement)
        with patch.object(self.library, 'space', wraps=self.library.space) as space:
            reference = self.publish()
        space.assert_called_once_with(len(before[self.raw])+len(before[self.receipt_path]))
        self.assertEqual(reference['format'], evidence.FORMAT)
        checked = self.store.verify(reference, wav_sha256=self.receipt['wav_sha256'], arrangement=self.arrangement)
        self.assertEqual(checked['raw_path'].read_bytes(), before[self.raw])
        self.assertEqual(checked['receipt_path'].read_bytes(), before[self.receipt_path])
        self.assertFalse(checked['receipt']['app_mode_enabled'])
        self.assertFalse(checked['receipt']['native_adapter_acceptance'])
        self.assertEqual(self.arrangement, arrangement)
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.library.items(), [])
        self.assertEqual(self.publish(), reference)
        self.assertEqual(len(list(self.store.root.iterdir())), 2)

    def test_verification_rejects_wrong_wav_origin_velocity_and_contour_bindings(self):
        reference = self.publish()
        for field, value in [('render_origin_sample', self.arrangement['render_origin_sample']-1), ('sample_rate', 48000)]:
            altered = {**self.arrangement, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.verify(reference, wav_sha256=self.receipt['wav_sha256'], arrangement=altered)
        for field, value in [('velocity', 126), ('end', self.arrangement['notes'][0]['end']+1), ('contour', ())]:
            altered = copy.deepcopy(self.arrangement); altered['notes'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.verify(reference, wav_sha256=self.receipt['wav_sha256'], arrangement=altered)
        with self.assertRaises(ValueError):
            self.store.verify(reference, wav_sha256='f'*64, arrangement=self.arrangement)

    def test_metadata_versions_quality_warning_and_prototype_flags_are_not_repaired(self):
        pristine = copy.deepcopy(self.receipt)
        mutations = [lambda r: r.update(renderer_version='changed'), lambda r: r.update(profile_version='changed'),
                     lambda r: r['quality_by_instance'][0].update(sample=True),
                     lambda r: r['sample_limits'][0].update(gate_exceeds_stored_sample_estimate=True),
                     lambda r: r['sample_limits'][0].update(transposed_stored_frames_estimate=101.),
                     lambda r: r.update(app_mode_enabled=True), lambda r: r.update(native_adapter_acceptance=True),
                     lambda r: r.update(effects=['reverb']), lambda r: r['source_commands'][0].update(player=True),
                     lambda r: r.update(contours_not_rendered=[]), lambda r: r.pop('bank'),
                     lambda r: r.update(unregistered_metadata='ignored?'),
                     lambda r: r['physical_assumptions'].update(fingering_biomechanics_verified=0)]
        for mutate in mutations:
            self.receipt = copy.deepcopy(pristine); mutate(self.receipt); self.save_receipt()
            with self.subTest(change=str(mutate)), self.assertRaises(ValueError): self.publish()
        self.assertFalse(self.store.root.exists())

    def test_receipt_is_strict_json_without_duplicate_keys_or_nonfinite_values(self):
        for raw in (b'{"frames":1,"frames":2}', b'{"frames":NaN}', b'[]', b'\xff'):
            self.receipt_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError): self.publish()
        self.assertFalse(self.store.root.exists())

    def test_raw_geometry_hash_peak_and_rms_corruption_refuse_publication(self):
        original = self.raw.read_bytes()
        for raw in (original[:-1], b'\0'*len(original), struct.pack('<f', float('nan'))+original[4:],
                    struct.pack('<f', .25)+original[4:]):
            self.raw.write_bytes(raw)
            with self.subTest(length=len(raw)), self.assertRaises(ValueError): self.publish()
        self.raw.write_bytes(original)
        for key in ('sample_peak', 'rms'):
            self.receipt['raw_float'][key] += .001; self.save_receipt()
            with self.subTest(key=key), self.assertRaises(ValueError): self.publish()
            self.receipt['raw_float'][key] -= .001
        self.assertFalse(self.store.root.exists())

    def test_wav_hash_and_geometry_cannot_hide_wrong_quantization(self):
        original = self.wav.read_bytes()
        for raw in (original[:-1], original[:44]+b'\0'*(len(original)-44), original+b'unknown'):
            self.wav.write_bytes(raw); self.receipt['wav_sha256'] = digest(raw); self.save_receipt()
            with self.subTest(length=len(raw)), self.assertRaises(ValueError): self.publish()
        self.assertFalse(self.store.root.exists())

    def test_size_bounds_and_reference_types_fail_before_large_reads(self):
        pristine = copy.deepcopy(self.receipt)
        with self.raw.open('wb') as stream: stream.truncate(evidence.MAX_RAW_BYTES+8)
        with self.assertRaises(ValueError): self.publish()
        self.raw.write_bytes(b'\0'*8)
        with self.receipt_path.open('wb') as stream: stream.truncate(evidence.MAX_RECEIPT_BYTES+1)
        with self.assertRaises(ValueError): self.publish()
        reference = {'format': evidence.FORMAT, 'raw': {'sha256': 'a'*64, 'bytes': 8}, 'receipt': {'sha256': 'b'*64, 'bytes': 1}}
        for key, value in [('bytes', True), ('bytes', evidence.MAX_RAW_BYTES+8), ('sha256', '../unsafe')]:
            bad = copy.deepcopy(reference); bad['raw'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): evidence.validate_reference(bad)
        bad = copy.deepcopy(reference); bad['raw']['bytes'] = 7
        with self.assertRaises(ValueError): evidence.validate_reference(bad)

    def test_known_active_job_direct_siblings_only_and_reserve_failure_preserves_sources(self):
        with self.assertRaises(ValueError): self.publish(raw_path=self.library.root/self.raw.name)
        self.job(self.identity, state='complete')
        with self.assertRaises(ValueError): self.publish()
        self.job(self.identity)
        before = self.raw.read_bytes()
        with patch.object(self.library, 'space', side_effect=ValueError('8 GiB reserve')):
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.raw.read_bytes(), before)
        self.assertFalse(self.store.root.exists())

    def test_hardlinks_and_reparse_paths_are_refused(self):
        alias = self.library.stage/'hardlink-copy'
        os.link(self.raw, alias)
        with self.assertRaises(ValueError): self.publish()
        alias.unlink()
        # Reparse denial is exercised without requiring symlink privileges.
        original = Path.lstat
        def linked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.raw:
                class Changed:
                    st_mode = info.st_mode; st_nlink = info.st_nlink; st_file_attributes = 0x400
                return Changed()
            return info
        with patch.object(Path, 'lstat', linked):
            with self.assertRaises(ValueError): self.publish()

    def test_protected_source_readers_deny_transient_writers_during_copy(self):
        real_space = self.library.space
        def inspect(needed):
            for path in (self.raw, self.wav, self.receipt_path):
                with self.subTest(path=path), self.assertRaises(OSError):
                    with path.open('r+b') as stream: stream.write(b'changed')
            real_space(needed)
        with patch.object(self.library, 'space', side_effect=inspect): self.publish()

    def test_preexisting_corrupt_content_address_is_preserved_without_metadata_overwrite(self):
        self.store.root.mkdir()
        target = self.store.root/(self.receipt['raw_float']['sha256']+'.f32')
        target.write_bytes(b'corrupt')
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(target.read_bytes(), b'corrupt')
        self.assertEqual(list(self.store.root.iterdir()), [target])

    def test_cancel_before_or_during_validation_has_no_publication(self):
        with self.assertRaises(InterruptedError): self.publish(cancel=lambda: True)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls == 3
        with self.assertRaises(InterruptedError): self.publish(cancel=cancel)
        self.assertFalse(self.store.root.exists())
        self.assertTrue(self.raw.exists() and self.wav.exists() and self.receipt_path.exists())

    def test_crash_between_blob_renames_leaves_only_collectible_unreferenced_copy(self):
        rename = evidence.os.rename
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2: raise RuntimeError('Injected publication boundary fault')
            return rename(source, target)
        with patch.object(evidence.os, 'rename', side_effect=fail_second):
            with self.assertRaises(RuntimeError): self.publish()
        paths = list(self.store.root.iterdir())
        self.assertEqual(len(paths), 1); self.assertEqual(paths[0].suffix, '.f32')
        self.job(self.identity, state='failed')
        self.assertEqual(self.reconcile()['files'], 1)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_pending_filename_collision_is_never_deleted_or_overwritten(self):
        self.store.root.mkdir()
        identity = uuid.UUID('00000000-0000-4000-8000-000000000001')
        occupied = self.store.root/('.publish-'+str(identity)+'-'+self.receipt['raw_float']['sha256']+'.partial')
        occupied.write_bytes(b'foreign pending data')
        with patch.object(evidence.uuid, 'uuid4', return_value=identity):
            with self.assertRaises(FileExistsError): self.publish()
        self.assertEqual(occupied.read_bytes(), b'foreign pending data')

    def test_cancel_after_one_rename_never_claims_a_complete_reference(self):
        renamed = False; rename = evidence.os.rename
        def remember(source, target):
            nonlocal renamed
            value = rename(source, target); renamed = True; return value
        with patch.object(evidence.os, 'rename', side_effect=remember):
            with self.assertRaises(InterruptedError): self.publish(cancel=lambda: renamed)
        self.assertEqual(len(list(self.store.root.iterdir())), 1)
        self.assertFalse(any(path.name.startswith('.publish-') for path in self.store.root.iterdir()))

    def test_shared_references_across_all_job_states_retain_then_collect_both_blobs(self):
        reference = self.publish()
        second = str(uuid.uuid4())
        self.job(self.identity, {'render_evidence': reference}, state='complete')
        self.job(second, {'render_evidence': reference}, state='failed')
        self.assertEqual(self.reconcile(), {'files': 0, 'bytes': 0})
        self.job(self.identity, state='failed')
        self.assertEqual(self.reconcile()['files'], 0)
        self.job(second, state='failed')
        result = self.reconcile()
        self.assertEqual(result, {'files': 2, 'bytes': reference['raw']['bytes']+reference['receipt']['bytes']})
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_unknown_corrupt_linked_and_crash_pending_files_survive_reconciliation(self):
        reference = self.publish()
        raw_path = self.store.root/(reference['raw']['sha256']+'.f32')
        raw_path.write_bytes(b'corrupt raw preserved')
        unknown = self.store.root/'user-notes.txt'; unknown.write_bytes(b'keep')
        pretend = json.dumps({'renderer_version': evidence.RENDER_VERSION, 'profile_version': evidence.PROFILE_VERSION,
                              'app_mode_enabled': False}).encode()
        json_path = self.store.root/(digest(pretend)+'.json'); json_path.write_bytes(pretend)
        pending = self.store.root/'.publish-crash.partial'; pending.write_bytes(b'partial')
        hardlinked = self.store.root/(digest(struct.pack('<ff', .1, -.1))+'.f32'); hardlinked.write_bytes(struct.pack('<ff', .1, -.1))
        alias = self.library.stage/'evidence-hardlink'; os.link(hardlinked, alias)
        self.assertEqual(self.reconcile()['files'], 0)
        self.assertTrue(all(path.exists() for path in (unknown, json_path, pending, hardlinked, raw_path)))

    def test_malformed_saved_reference_aborts_collection_before_any_delete(self):
        reference = self.publish()
        self.job(self.identity, {'render_evidence': {'format': 'broken'}}, state='complete')
        with self.assertRaises(ValueError): self.reconcile()
        self.assertEqual(len(list(self.store.root.iterdir())), 2)
        self.assertTrue(self.store.verify(reference, wav_sha256=self.receipt['wav_sha256'], arrangement=self.arrangement))

    def test_mismatched_saved_raw_receipt_pair_cannot_collect_its_actual_partner(self):
        reference = self.publish()
        other = struct.pack('<ff', .25, -.125)
        other_path = self.store.root/(digest(other)+'.f32'); other_path.write_bytes(other)
        damaged = copy.deepcopy(reference)
        damaged['raw'] = {'sha256': digest(other), 'bytes': len(other)}
        self.job(self.identity, {'render_evidence': damaged}, state='complete')
        before = {path.name: path.read_bytes() for path in self.store.root.iterdir()}
        with self.assertRaises(ValueError): self.reconcile()
        self.assertEqual({path.name: path.read_bytes() for path in self.store.root.iterdir()}, before)

    def test_saved_arrangement_or_wav_binding_mismatch_aborts_all_collection(self):
        reference = self.publish()
        for change in ({'asset': {'sha256': 'f'*64}}, {'arrangement': {**self.arrangement, 'render_origin_sample': 0}}):
            self.job(self.identity, {'render_evidence': reference, **change}, state='complete')
            with self.subTest(change=list(change)), self.assertRaises(ValueError): self.reconcile()
            self.assertEqual(len(list(self.store.root.iterdir())), 2)

    def test_huge_valid_json_number_refuses_publication_and_gc_preserves_all(self):
        reference = self.publish()
        self.receipt['raw_peak'] = 10**1000
        self.save_receipt()
        with self.assertRaises(ValueError): self.publish()
        malformed = self.receipt_path.read_bytes()
        malformed_path = self.store.root/(digest(malformed)+'.json')
        malformed_path.write_bytes(malformed)
        damaged = copy.deepcopy(reference)
        damaged['receipt'] = {'sha256': digest(malformed), 'bytes': len(malformed)}
        self.job(self.identity, {'render_evidence': damaged}, state='complete')
        before = {path.name: path.read_bytes() for path in self.store.root.iterdir()}
        with self.assertRaises(ValueError): self.reconcile()
        self.assertEqual({path.name: path.read_bytes() for path in self.store.root.iterdir()}, before)

    def test_cancellation_during_gc_is_not_swallowed_as_an_io_error(self):
        self.publish()
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls == 2
        with self.assertRaises(InterruptedError): self.reconcile(cancel=cancel)
        self.assertEqual(len(list(self.store.root.iterdir())), 2)


if __name__ == '__main__':
    unittest.main()
