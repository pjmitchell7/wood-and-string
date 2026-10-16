"""Silent authored PCM tests; no samples, model, renderer or playback."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import stem_evidence as s


def write_wav(path, values):
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<'+'h'*len(values), *values))


def identity(path, frames):
    raw = path.read_bytes()
    return {'name': path.name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'format': 'pcm_s16le', 'channels': 2, 'sample_rate': 44100, 'frames': frames}


def binding(m):
    return {k: deepcopy(m[k]) for k in s.BINDING_KEYS}


def manifest(folder, tracks=None, gains=None, audible=None):
    tracks = tracks or [[0, 0, 1001, -801, 800, -600, 503, -301, 200, -100, 90, -70, 60, -50, 30, -20],
                        [0, 0, -401, 601, -200, 400, 101, 201, 80, -40, 60, -20, 40, -10, 20, -4]]
    gains = gains or [.5, .25]
    audible = audible if audible is not None else [True]*len(tracks)
    frames = len(tracks[0])//2
    stems = []
    for i, values in enumerate(tracks):
        path = folder/f'part-{i+1}.wav';write_wav(path, values)
        stems.append({'id': f'part-{i+1}', 'instrument': 'acoustic' if i < 2 else 'drums',
            'role': 'melody and harmony' if i == 0 else 'backing', 'gain': gains[i],
            'default_audible': audible[i], 'file': identity(path, frames),
            'provenance': {'source_ids': [f'evidence-{i+1}'], 'assignment': 'redistributed',
                'adaptations': [], 'newly_composed': False, 'source_kind': 'estimated'}})
    values = [round(sum(t[j]*g for t, g, on in zip(tracks, gains, audible) if on)) for j in range(frames*2)]
    combined = folder/'combined.wav';write_wav(combined, values)
    return {'version': 'stem-bundle-v1', 'source': {'id': 'original-1', 'sha256': 'a'*64},
        'clock': {'sample_rate': 44100, 'origin_sample': 5336117, 'body_frames': frames-3,
                  'tail_frames': 3, 'frames': frames},
        'arrangement': {'revision': 'duo-revision-1', 'sha256': 'b'*64,
                        'performers': min(len(tracks), 2), 'label': 'Keep close Duo'},
        'renderer': {'id': 'authored-fixture', 'version': '1', 'receipt_sha256': 'c'*64,
                     'source_kind': 'synthesized', 'content_exclusion': 'declared_not_verified'},
        'stems': stems, 'combined': {'file': identity(combined, frames),
            'mix_point': 'linear_pre_master', 'active_stem_ids': [x['id'] for x in stems if x['default_audible']],
            'quantization': 'sum_retained_pcm16_then_nearest_ties_even', 'tolerance_lsb': 1}}


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.m = manifest(self.root)

    def test_detached_strict_json_and_orthogonal_provenance(self):
        original = deepcopy(self.m)
        v = s.parse_manifest(json.dumps(self.m).encode())
        v['stems'][0]['provenance']['adaptations'].append('octave +1')
        self.assertEqual(self.m, original)
        self.m['arrangement']['label'] = 'Essentials: riff focus'
        self.m['stems'][0]['provenance']['source_kind'] = 'unknown source instrument'
        self.assertEqual(s.validate_manifest(self.m)['arrangement']['label'], 'Essentials: riff focus')
        for raw in (b'{"version":1,"version":2}', b'{} trailing', b'{"x":NaN}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):s.parse_manifest(raw)

    def test_bad_fields_numbers_paths_and_clock_refuse(self):
        changes = [lambda m:m['clock'].update(origin_sample=True),
            lambda m:m['clock'].update(frames=7), lambda m:m['clock'].update(origin_sample=2**53-1),
            lambda m:m['stems'][0].update(gain=float('nan')), lambda m:m['stems'][0].update(gain=10**1000),
            lambda m:m['stems'][0].update(gain=True), lambda m:m['stems'][0].update(default_audible=1),
            lambda m:m['stems'][0]['file'].update(name='../outside.wav'),
            lambda m:m['stems'][0]['file'].update(name='CON.wav'),
            lambda m:m['stems'][1]['file'].update(name='PART-1.WAV'),
            lambda m:m['renderer'].update(source_kind='analysis other stem'),
            lambda m:m['stems'][0].update(offset=5), lambda m:m['combined'].update(active_stem_ids=['missing']),
            lambda m:m['combined'].update(tolerance_lsb=2),
            lambda m:m['stems'][0]['provenance'].update(newly_composed=1)]
        for change in changes:
            value = deepcopy(self.m);change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):s.validate_manifest(value)

    def test_policy_is_separate_from_manifest_and_has_hard_ceiling(self):
        frames = s.DEFAULT_MAX_FRAMES+1
        self.m['clock'].update(body_frames=frames-3, frames=frames)
        for f in [x['file'] for x in self.m['stems']]+[self.m['combined']['file']]:
            f.update(frames=frames, bytes=44+frames*4)
        with self.assertRaises(ValueError):s.validate_manifest(self.m)
        self.assertEqual(s.validate_manifest(self.m, max_frames=frames)['clock']['frames'], frames)
        with self.assertRaises(ValueError):s.validate_manifest(self.m, max_frames=s.HARD_MAX_FRAMES+1)


@unittest.skipUnless(os.name == 'nt', 'Read-share-only verification is Windows-specific')
class FileTests(MetadataTests):
    # Avoid inheriting/re-running metadata cases in this subclass.
    test_detached_strict_json_and_orthogonal_provenance = None
    test_bad_fields_numbers_paths_and_clock_refuse = None
    test_policy_is_separate_from_manifest_and_has_hard_ceiling = None

    def verify(self, m=None, **kwargs):
        m = self.m if m is None else m
        return s.verify_bundle(self.root, m, expected_binding=binding(m), **kwargs)

    def test_actual_linear_mix_tail_and_identity(self):
        result = self.verify();v = result['verification']
        self.assertEqual(v['maximum_quantized_error_lsb'], 0)
        self.assertLessEqual(v['maximum_linear_residual_lsb'], .5)
        self.assertTrue(v['all_mute_subsets_and_gain_reductions_within_pcm_range'])
        self.assertEqual(v['clock'], self.m['clock'])
        self.assertFalse(v['source_or_vocal_content_exclusion_verified'])
        self.assertFalse(v['source_clock_acoustic_alignment_verified'])
        result['manifest']['source']['id'] = 'changed caller copy'
        self.assertEqual(v['source']['id'], 'original-1')

    def test_muted_duet_keeps_complete_duet_and_default_export(self):
        m = manifest(self.root, audible=[True, False])
        result = self.verify(m)
        self.assertEqual(result['manifest']['arrangement']['performers'], 2)
        self.assertEqual(result['manifest']['arrangement']['label'], 'Keep close Duo')
        self.assertEqual(result['manifest']['combined']['active_stem_ids'], ['part-1'])

    def test_exact_resource_boundary_before_files_open(self):
        total = sum(x['file']['bytes'] for x in self.m['stems'])+self.m['combined']['file']['bytes']
        self.verify(max_frames=8, max_total_audio_bytes=total)
        with patch.object(s, '_protected_reader', side_effect=AssertionError('Must refuse before opening')):
            with self.assertRaises(ValueError):self.verify(max_frames=7)
            with self.assertRaises(ValueError):self.verify(max_total_audio_bytes=total-1)

    def test_known_original_or_master_bytes_blocked(self):
        self.m['source']['sha256'] = self.m['stems'][0]['file']['sha256']
        with self.assertRaises(ValueError):self.verify()
        self.m['source']['sha256'] = 'a'*64
        with self.assertRaises(ValueError):self.verify(forbidden_audio_sha256=[self.m['combined']['file']['sha256']])

    def test_tamper_and_saved_full_render_identity(self):
        digest = self.verify()['verification']['manifest_sha256']
        path = self.root/'part-1.wav';raw = path.read_bytes();path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(ValueError):self.verify()
        self.m['stems'][0]['file'] = identity(path, 8)
        with self.assertRaises(ValueError):self.verify(expected_manifest_sha256=digest)

    def test_misaligned_one_frame_and_missing_tail_refuse(self):
        path = self.root/'part-1.wav';raw = path.read_bytes()
        shifted = raw[:44]+b'\0'*4+raw[44:-4];path.write_bytes(shifted)
        self.m['stems'][0]['file'] = identity(path, 8)
        with self.assertRaises(ValueError):self.verify()
        path.write_bytes(raw[:-4])
        with self.assertRaises(ValueError):self.verify()

    def test_rehashed_wrong_header_and_undeclared_gain_refuse(self):
        path = self.root/'part-1.wav';raw = bytearray(path.read_bytes());raw[24:28] = struct.pack('<I', 48000)
        path.write_bytes(raw);self.m['stems'][0]['file'] = identity(path, 8)
        with self.assertRaises(ValueError):self.verify()
        self.m = manifest(self.root)
        path = self.root/'combined.wav';values = list(struct.unpack('<16h', path.read_bytes()[44:]));values[2] += 3
        write_wav(path, values);self.m['combined']['file'] = identity(path, 8)
        with self.assertRaises(ValueError):self.verify()

    def test_opposed_full_mix_cannot_hide_unsafe_muted_subset(self):
        tracks = [[20000]*16, [20000]*16, [-20000]*16, [-20000]*16]
        m = manifest(self.root, tracks=tracks, gains=[1]*4)
        with self.assertRaises(ValueError):self.verify(m)
        m = manifest(self.root, tracks=tracks, gains=[.5]*4)
        v = self.verify(m)['verification']
        self.assertEqual(v['maximum_positive_subset_lsb'], 20000)
        self.assertEqual(v['minimum_negative_subset_lsb'], -20000)

    def test_declared_context_cannot_cross_source_revision_or_origin(self):
        for key in ('source', 'clock', 'arrangement', 'renderer'):
            expected = binding(self.m)
            if key == 'clock':expected[key]['origin_sample'] += 1
            elif key == 'source':expected[key]['id'] = 'other-source'
            elif key == 'arrangement':expected[key]['revision'] = 'another-revision'
            else:expected[key]['receipt_sha256'] = 'd'*64
            with self.subTest(key=key), self.assertRaises(ValueError):
                s.verify_bundle(self.root, self.m, expected_binding=expected)

    def test_handles_block_writers_then_close(self):
        path = self.root/'part-1.wav'
        with s.protect_bundle(self.root, self.m, expected_binding=binding(self.m)):
            with self.assertRaises(OSError):path.open('r+b')
        with path.open('r+b') as f:self.assertEqual(f.read(4), b'RIFF')

    def test_hardlink_refuses(self):
        os.link(self.root/'part-1.wav', self.root/'extra-link.wav')
        with self.assertRaises(ValueError):self.verify()

    def test_cancel_after_open_closes_all_owned_readers(self):
        original = s._protected_reader;opened = [];cancelled = [False]
        def reader(path):
            result = original(path);opened.append(result)
            if len(opened) == 2:cancelled[0] = True
            return result
        with patch.object(s, '_protected_reader', side_effect=reader):
            with self.assertRaises(InterruptedError):self.verify(cancel=lambda:cancelled[0])
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(f.closed for f in opened))


if __name__ == '__main__':unittest.main()
