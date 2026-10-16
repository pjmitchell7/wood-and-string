"""Silent authored PCM and fake Library tests; no bank/native/device/DB work."""
import array
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from audio.acoustic_stem_capture import AcousticStemCapture, forecast_capture
from audio.render import _write_pcm16, RENDER_VERSION
import stem_store as store

RATE = 44100
FRAMES = RATE + 7
ORIGIN = 9387


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FakeLibrary:
    def __init__(self, root):
        self.root = root
        self.stage = root / 'staging'
        self.stage.mkdir(parents=True)
        self.space_calls = []

    def space(self, needed=0):
        self.space_calls.append(needed)

    def items(self):
        return []

    def connect(self):
        raise AssertionError('Companion store must not access the job/asset database')

    def update_job(self, *args, **kwargs):
        raise AssertionError('Companion store must not commit job results')


def captured(library, identity, *, performers=2):
    """Author complete stereo PCM including a nonzero last tail sample."""
    source = {'id': 'authored-original', 'sha256': sha(b'unchanged original')}
    notes = [dict(id=f'n{n}', start=ORIGIN, end=ORIGIN + 7, pitch=40 + n,
                  velocity=70, player=n, string=n, fret=0, role='melody' if n == 1 else 'bass')
             for n in range(1, performers + 1)]
    arrangement = {'version': 1, 'source_id': source['id'], 'sample_rate': RATE,
                   'render_origin_sample': ORIGIN, 'performers': performers,
                   'notes': notes, 'adaptations': [],
                   'source': {'source_id': source['id'], 'sample_rate': RATE, 'notes': deepcopy(notes)}}
    plan = forecast_capture(performers=performers, frames=FRAMES)
    capture = AcousticStemCapture(library.stage / (identity + '.stem-capture'), source=source,
        arrangement=arrangement, revision='authored-revision', performers=list(range(1, performers + 1)),
        origin_sample=ORIGIN, total_frames=FRAMES, max_work_bytes=plan['additional_disk_bytes'])
    spool, master = library.root / 'master.f32', library.root / 'master.wav'
    peak = 0.
    with spool.open('wb') as output:
        for start in range(0, FRAMES, 256):
            count = min(256, FRAMES - start)
            assert capture.begin_block(start, count)
            mix = [0.] * (2 * count)
            for number in range(1, performers + 1):
                left = array.array('f', [.04 * number if start + i < 7 else .004 * number
                    if start + i == FRAMES - 1 else 0. for i in range(count)])
                right = array.array('f', [-sample / 2 for sample in left])
                assert capture.add_instance(number, number, left, right)
                for index in range(count):
                    mix[index * 2] += left[index]
                    mix[index * 2 + 1] += right[index]
            values = array.array('f', mix)
            assert capture.end_block(values)
            peak = max(peak, max(map(abs, values)))
            output.write(values.tobytes())
    levels = _write_pcm16(spool, master, RATE, peak, 'peak', lambda: False, lambda _: None)
    gain = 10 ** (-3 / 20) / peak
    metrics = {**levels, 'frames': FRAMES, 'sample_rate': RATE, 'source_origin_sample': ORIGIN,
               'renderer_version': RENDER_VERSION, 'processing_mode': 'freewheeling',
               'instances': performers, 'quality_by_instance': [{'sample': 2, 'oscillator': 1} for _ in notes]}
    assert metrics['gain_db'] == 20 * math.log10(gain)
    result = capture.finish(master, raw_peak=peak, global_gain=gain, renderer_metrics=metrics)
    assert result['status'] == 'admitted', result
    return capture, master, result['bundle']['manifest']


@unittest.skipUnless(os.name == 'nt', 'Private store requires Windows protected readers and no-replace rename')
class StemStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = FakeLibrary(self.root / 'private-library')
        self.store = store.StemStore(self.library)
        self.identity = str(uuid.uuid4())
        self.capture, self.master, self.manifest = captured(self.library, self.identity)
        self.source = self.capture.folder
        self.original = self.library.root / 'original.mp3'
        self.original.write_bytes(b'private original must survive companion refusal')
        self.preserved = {path: path.read_bytes() for path in (self.master, self.original)}
        self.context = self.bound_context()

    def bound_context(self):
        manifest_raw = (self.source / 'manifest.json').read_bytes()
        manifest = json.loads(manifest_raw)
        return {'expected_binding': {key: manifest[key] for key in store.stem_evidence.BINDING_KEYS},
                'expected_manifest_sha256': sha(manifest_raw),
                'expected_renderer_receipt_sha256': sha((self.source / 'renderer-receipt.json').read_bytes()),
                'max_total_audio_bytes': store.MAX_AUDIO_BYTES}

    def publish(self, **changes):
        context = {**self.context, 'max_publish_bytes': store.MAX_BUNDLE_BYTES, **changes}
        return self.store.publish(self.identity, **context)

    def assert_preserved(self):
        for path, raw in self.preserved.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.library.items(), [])

    def test_publish_reload_and_held_media_keep_complete_tail_and_default_mix(self):
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        # Producer verification is deliberately ignored; the store creates its own.
        (self.source / 'verification.json').write_bytes(b'untrusted obsolete verification')
        reference = self.publish()
        checked = self.store.verify(reference, **self.context)
        target = self.store.root / reference['bundle_id']
        self.assertEqual(reference['bundle_id'], self.context['expected_manifest_sha256'])
        self.assertNotIn(str(self.root), canonical(reference).decode())
        self.assertEqual({p.name for p in target.iterdir()}, {'performer-1.wav', 'performer-2.wav',
                         'combined.wav', 'manifest.json', 'renderer-receipt.json', 'verification.json'})
        self.assertEqual((target / 'manifest.json').read_bytes(), before['manifest.json'])
        self.assertEqual((target / 'renderer-receipt.json').read_bytes(), before['renderer-receipt.json'])
        self.assertTrue(checked['verification']['linear_default_mix_verified'])
        self.assertFalse(checked['verification']['musical_acceptance'])
        self.assertEqual(checked['manifest']['clock']['tail_frames'], RATE)
        self.assertEqual(checked['manifest']['combined']['active_stem_ids'], ['performer-1', 'performer-2'])
        with self.store.protect(reference, **self.context) as held:
            for name, stream in held['audio'].items():
                self.assertEqual(stream.read(), before[name])
                self.assertNotEqual(before[name][-4:], b'\0' * 4)
                with self.assertRaises(OSError):
                    (target / name).open('r+b')
        for name, raw in before.items():
            if name != 'verification.json':
                self.assertEqual((self.source / name).read_bytes(), raw)
        self.assert_preserved()

    def test_solo_and_duo_use_full_manifest_address_not_arrangement_hash(self):
        solo_id = str(uuid.uuid4())
        capture, _, manifest = captured(self.library, solo_id, performers=1)
        source = self.source
        self.source = capture.folder
        context = self.bound_context()
        self.source = source
        reference = self.store.publish(solo_id, **context, max_publish_bytes=store.MAX_BUNDLE_BYTES)
        checked = self.store.verify(reference, **context)
        self.assertEqual(len(reference['audio']), 2)
        self.assertEqual(checked['manifest']['arrangement']['label'], 'Acoustic Solo')
        self.assertNotEqual(reference['bundle_id'], manifest['arrangement']['sha256'])

    def test_duplicate_reverifies_existing_bundle_without_writes_or_db_commit(self):
        reference = self.publish()
        before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in (self.store.root / reference['bundle_id']).iterdir()}
        with patch.object(store, '_copy_stream', side_effect=AssertionError('Duplicate must not copy')):
            self.assertEqual(self.publish(), reference)
        self.assertEqual(self.library.space_calls[-1], 0)
        self.assertEqual(before, {p.name: (p.stat().st_ino, p.read_bytes()) for p in (self.store.root / reference['bundle_id']).iterdir()})
        # A caller's later DB failure does not roll back an immutable complete blob.
        try:
            raise RuntimeError('Injected caller DB commit failure')
        except RuntimeError:
            pass
        self.assertEqual(self.store.verify(reference, **self.context)['reference'], reference)
        self.assert_preserved()

    def test_changed_source_manifest_receipt_wav_and_incomplete_capture_refuse(self):
        cases = [('manifest.json', lambda raw: raw + b' '),
                 ('renderer-receipt.json', lambda raw: raw[:-1] + b' '),
                 ('performer-1.wav', lambda raw: raw[:-4] + b'\0' * 4),
                 ('combined.wav', lambda raw: raw[:-1]), ('performer-2.wav', lambda _: None)]
        for name, mutate in cases:
            path = self.source / name
            original = path.read_bytes()
            changed = mutate(original)
            if changed is None:
                path.unlink()
            else:
                path.write_bytes(changed)
            with self.subTest(name=name), self.assertRaises(store.CompanionStoreError) as raised:
                self.publish()
            self.assertLess(len(str(raised.exception)), 200)
            path.write_bytes(original)
            self.assertFalse(self.store.root.exists())
        self.assert_preserved()

    def test_current_acoustic_backend_schema_and_canonical_receipt_are_enforced(self):
        receipt_path = self.source / 'renderer-receipt.json'
        manifest_path = self.source / 'manifest.json'
        receipt_raw, manifest_raw = receipt_path.read_bytes(), manifest_path.read_bytes()
        for mutation in (lambda r: r.update(version='future-capture'), lambda r: r.update(musical_acceptance=True),
                         lambda r: r['performer_float']['1'].update(bytes=True),
                         lambda r: r['instances'][0].__setitem__(0, True),
                         lambda r: r['renderer_metrics']['quality_by_instance'][0].update(sample=True),
                         lambda r: r['commands'][0].update(player=3)):
            receipt = json.loads(receipt_raw)
            mutation(receipt)
            raw = canonical(receipt)
            manifest = json.loads(manifest_raw)
            manifest['renderer']['receipt_sha256'] = sha(raw)
            receipt_path.write_bytes(raw)
            manifest_path.write_bytes(canonical(manifest))
            with self.assertRaises(store.CompanionStoreError):
                self.publish(**self.bound_context())
        receipt_path.write_bytes(receipt_raw + b' ')
        manifest = json.loads(manifest_raw)
        manifest['renderer']['receipt_sha256'] = sha(receipt_raw + b' ')
        manifest_path.write_bytes(canonical(manifest))
        with self.assertRaises(store.CompanionStoreError):
            self.publish(**self.bound_context())
        receipt_path.write_bytes(receipt_raw)
        manifest_path.write_bytes(manifest_raw)
        self.assertFalse(self.store.root.exists())

    def test_hard_duration_current_renderer_and_exact_acoustic_roster_are_required(self):
        manifest_path = self.source / 'manifest.json'
        manifest_raw = manifest_path.read_bytes()
        for mutation in (lambda m: m['clock'].update(frames=store.MAX_FRAMES + 1,
                            body_frames=store.MAX_FRAMES + 1 - RATE),
                         lambda m: m['renderer'].update(id='electric-renderer'),
                         lambda m: m['stems'][0].update(instrument='electric'),
                         lambda m: m['arrangement'].update(performers=3),
                         lambda m: m['stems'][0].update(gain=.5)):
            manifest = json.loads(manifest_raw)
            mutation(manifest)
            manifest_path.write_bytes(canonical(manifest))
            with patch.object(store, '_copy_stream', side_effect=AssertionError('Unsupported schema must not copy')):
                with self.assertRaises(store.CompanionStoreError):
                    self.publish(**self.bound_context())
        manifest_path.write_bytes(manifest_raw)
        with self.assertRaises(store.CompanionStoreError):
            self.publish(forbidden_audio_sha256=[self.manifest['stems'][0]['file']['sha256']])
        self.assertFalse(self.store.root.exists())
        self.assert_preserved()

    def test_changed_stored_files_reference_and_all_trusted_bindings_refuse(self):
        reference = self.publish()
        folder = self.store.root / reference['bundle_id']
        for name in ('manifest.json', 'renderer-receipt.json', 'verification.json', 'performer-1.wav'):
            path = folder / name
            raw = path.read_bytes()
            path.write_bytes(raw + b'foreign')
            with self.subTest(name=name), self.assertRaises(store.CompanionStoreError):
                self.store.verify(reference, **self.context)
            with self.assertRaises(store.CompanionStoreError):
                self.publish()
            self.assertEqual(path.read_bytes(), raw + b'foreign')
            path.write_bytes(raw)
        for key, nested, value in (('source', 'id', 'other-original'), ('arrangement', 'revision', 'other-job'),
                                    ('clock', 'origin_sample', ORIGIN + 1), ('renderer', 'version', 'other-renderer')):
            context = deepcopy(self.context)
            context['expected_binding'][key][nested] = value
            with self.subTest(binding=key), self.assertRaises(store.CompanionStoreError):
                self.store.verify(reference, **context)
        for path, value in ((('manifest', 'bytes'), True), (('renderer_receipt', 'sha256'), 'f' * 64),
                            (('audio', 0, 'name'), '../original.mp3'), (('verification', 'bytes'), 1)):
            bad = deepcopy(reference)
            row = bad
            for key in path[:-1]:
                row = row[key]
            row[path[-1]] = value
            with self.subTest(reference=path), self.assertRaises(store.CompanionStoreError):
                self.store.verify(bad, **self.context)
        with self.assertRaises(store.CompanionStoreError):
            self.store.verify(reference, **{**self.context, 'expected_manifest_sha256': 'f' * 64})
        self.assert_preserved()

    def test_explicit_caps_and_reserve_fail_before_any_copy(self):
        audio_bytes = sum(row['bytes'] for row in store._members(self.manifest))
        with patch.object(store, '_copy_stream', side_effect=AssertionError('Must fail before copy')):
            for change in ({'max_total_audio_bytes': audio_bytes - 1}, {'max_publish_bytes': audio_bytes},
                           {'max_total_audio_bytes': True}, {'max_publish_bytes': False}):
                with self.subTest(change=change), self.assertRaises(store.CompanionStoreError):
                    self.publish(**change)
            with patch.object(self.library, 'space', side_effect=ValueError('8 GiB reserve')) as space:
                with self.assertRaises(store.CompanionStoreError):
                    self.publish()
                space.assert_called_once()
        self.assertFalse(self.store.root.exists())
        self.assert_preserved()

    def test_protected_source_readers_remain_held_during_copy(self):
        original_copy = store._copy_stream
        tested = False
        def inspect(source, target, size, cancel):
            nonlocal tested
            if not tested:
                tested = True
                for name in ('manifest.json', 'renderer-receipt.json', 'performer-1.wav', 'performer-2.wav', 'combined.wav'):
                    with self.subTest(name=name), self.assertRaises(OSError):
                        (self.source / name).open('r+b')
            return original_copy(source, target, size, cancel)
        with patch.object(store, '_copy_stream', side_effect=inspect):
            self.publish()
        self.assertTrue(tested)

    def test_copy_failure_and_cancellation_clean_only_owned_files(self):
        self.store.root.mkdir()
        sentinel = self.store.root / 'unrelated-notes.txt'
        sentinel.write_bytes(b'leave unrelated data')
        original_copy = store._copy_stream
        for failure in (OSError('Injected short disk write'), InterruptedError('Injected cancellation')):
            calls = 0
            def fail_second(source, target, size, cancel):
                nonlocal calls
                calls += 1
                if calls == 2:
                    target.write(b'partial')
                    raise failure
                return original_copy(source, target, size, cancel)
            with patch.object(store, '_copy_stream', side_effect=fail_second):
                with self.assertRaises(store.CompanionStoreError) as raised:
                    self.publish()
            self.assertEqual(raised.exception.cancelled, isinstance(failure, InterruptedError))
            self.assertEqual(list(self.store.root.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b'leave unrelated data')
        with self.assertRaises(store.CompanionStoreCancelled):
            self.publish(cancel=lambda: True)
        self.assert_preserved()

    def test_foreign_and_changed_stage_members_survive_bounded_cleanup(self):
        original_write = self.store._write
        calls = 0
        foreign = None
        changed = None
        def interfere(stage, record, name, source, size, cancel):
            nonlocal calls, foreign, changed
            calls += 1
            if calls == 2:
                foreign = stage / 'foreign-sentinel.txt'
                foreign.write_bytes(b'foreign')
                changed = stage / 'performer-1.wav'
                changed.write_bytes(b'replaced bytes on existing inode')
                raise OSError('Injected failure with changed owned path')
            return original_write(stage, record, name, source, size, cancel)
        with patch.object(self.store, '_write', side_effect=interfere):
            with self.assertRaises(store.CompanionStoreError) as raised:
                self.publish()
        self.assertEqual(foreign.read_bytes(), b'foreign')
        self.assertEqual(changed.read_bytes(), b'replaced bytes on existing inode')
        record = raised.exception.pending_stage
        self.assertIsNotNone(record)
        self.assertNotIn(str(self.root), canonical(record).decode())
        self.assertEqual(self.store.cleanup_stage(record), {'bytes': 0})
        self.assertEqual(foreign.read_bytes(), b'foreign')
        self.assert_preserved()

    def test_atomic_publication_does_not_replace_occupied_target_or_adopt_stage(self):
        real_rename = store.os.rename
        occupied = None
        def race(source, target):
            nonlocal occupied
            occupied = Path(target)
            occupied.mkdir()
            (occupied / 'foreign-sentinel').write_bytes(b'occupied target')
            return real_rename(source, target)
        with patch.object(store.os, 'rename', side_effect=race):
            with self.assertRaises(store.CompanionStoreError):
                self.publish()
        self.assertEqual((occupied / 'foreign-sentinel').read_bytes(), b'occupied target')
        self.assertEqual(list(self.store.root.iterdir()), [occupied])
        self.assert_preserved()

    def test_occupied_stage_is_never_adopted_or_removed(self):
        self.store.root.mkdir()
        identity = uuid.UUID('00000000-0000-4000-8000-000000000002')
        occupied = self.store.root / ('.publish-' + str(identity))
        occupied.mkdir()
        sentinel = occupied / 'foreign-sentinel'
        sentinel.write_bytes(b'keep occupied stage')
        with patch.object(store.uuid, 'uuid4', return_value=identity):
            with self.assertRaises(store.CompanionStoreError):
                self.publish()
        self.assertEqual(sentinel.read_bytes(), b'keep occupied stage')
        self.assert_preserved()

    def test_cancel_after_atomic_rename_retains_complete_unreferenced_blob(self):
        real_rename = store.os.rename
        renamed = False
        def remember(source, target):
            nonlocal renamed
            value = real_rename(source, target)
            renamed = True
            return value
        with patch.object(store.os, 'rename', side_effect=remember):
            with self.assertRaises(store.CompanionStoreCancelled):
                self.publish(cancel=lambda: renamed)
        target = self.store.root / self.context['expected_manifest_sha256']
        self.assertEqual(len(list(target.iterdir())), 6)
        reference = self.publish()
        self.assertEqual(self.store.verify(reference, **self.context)['reference'], reference)
        self.assert_preserved()

    def test_capture_identity_paths_hardlinks_and_extra_final_members_are_refused(self):
        for identity in ('../master', str(uuid.uuid4()).upper(), str(self.source)):
            with self.subTest(identity=identity), self.assertRaises(store.CompanionStoreError):
                self.store.publish(identity, **self.context, max_publish_bytes=store.MAX_BUNDLE_BYTES)
        alias = self.root / 'hardlink.wav'
        os.link(self.source / 'performer-1.wav', alias)
        with self.assertRaises(store.CompanionStoreError):
            self.publish()
        alias.unlink()
        reference = self.publish()
        foreign = self.store.root / reference['bundle_id'] / 'foreign-note'
        foreign.write_bytes(b'never delete')
        with self.assertRaises(store.CompanionStoreError):
            self.store.verify(reference, **self.context)
        self.assertEqual(foreign.read_bytes(), b'never delete')
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
