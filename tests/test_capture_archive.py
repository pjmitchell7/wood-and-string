"""Real silent Windows file/transaction tests; no model or native engine."""
from contextlib import ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio import capture_archive as archive
from audio.full_decode import _protected_reader


def pin(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


@unittest.skipUnless(os.name == 'nt', 'Windows same-handle sharing/deletion contract')
class CaptureArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'local-arranger-public8-001'
        self.root.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(archive, 'ROOT', self.root).start()
        self.freeze = 'a' * 64

    def stage(self, ordinal=2, operation='count'):
        stage = self.root / f'development-{ordinal:02}' / operation
        stage.mkdir(parents=True, exist_ok=True)
        return stage

    def capture(self, data=b'zero\x00\xff\r\nline\nbackslash\\\n', ordinal=2, operation='count'):
        stage = self.stage(ordinal, operation)
        name = operation + '.stderr'
        (stage / name).write_bytes(data)
        return stage, name, pin(data)

    def publish(self, stage, name, expected, **kwargs):
        return archive.archive_capture(stage, name, expected=expected, freeze_sha256=self.freeze, **kwargs)

    def read(self, stage, name, expected, binding, **kwargs):
        with ExitStack() as stack:
            return archive.read_capture(stack, stage, name, expected=expected, binding=binding,
                freeze_sha256=self.freeze, **kwargs)

    def install_encoded(self, encoded, expected, ordinal=2):
        """An authenticated but invalid archive exercises decoding, not SHA mismatch."""
        stage, name = self.stage(ordinal), 'count.stderr'
        payload = {'version': archive.VERSION, 'ownership': {'freeze_sha256': self.freeze, 'ordinal': ordinal, 'stage': 'count'},
            'raw': {'name': name, **expected}, 'archive': {'name': name + '.gz', **pin(encoded)}}
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        (stage / (name + '.gz')).write_bytes(encoded)
        (stage / (name + '.archive.json')).write_bytes(raw)
        binding = {**payload, 'receipt': {'name': name + '.archive.json', **pin(raw)}}
        return stage, name, binding

    def test_roundtrip_exact_binary_and_empty_with_persistent_binding(self):
        for ordinal, data in ((2, b'\x00\xff\r\nplain\n\\u1234\n'), (3, b'')):
            with self.subTest(length=len(data)):
                stage, name, expected = self.capture(data, ordinal)
                binding = self.publish(stage, name, expected)
                self.assertFalse((stage / name).exists())
                self.assertEqual(self.read(stage, name, expected, binding), data)
                self.assertEqual({p.name for p in stage.iterdir()}, {name + '.gz', name + '.archive.json'})
                compressed = (stage / (name + '.gz')).read_bytes()
                self.assertEqual(pin(compressed), {k: binding['archive'][k] for k in ('bytes', 'sha256')})
                self.assertEqual(gzip.decompress(compressed), data)
                self.assertEqual(compressed[:10], b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff')
                receipt = json.loads((stage / (name + '.archive.json')).read_bytes())
                self.assertEqual(receipt, {k: binding[k] for k in ('version', 'ownership', 'raw', 'archive')})
                self.assertNotIn('raw_removed', receipt)

    def test_maximum_incompressible_and_deterministic_across_stage_and_name(self):
        data = os.urandom(archive.MAX_CAPTURE_BYTES)
        encoded = []
        for ordinal, operation in ((2, 'count'), (3, 'generate')):
            stage, name, expected = self.capture(data, ordinal, operation)
            binding = self.publish(stage, name, expected)
            encoded.append((stage / binding['archive']['name']).read_bytes())
            self.assertLessEqual(len(encoded[-1]), archive.MAX_ARCHIVE_BYTES)
            self.assertEqual(self.read(stage, name, expected, binding), data)
        self.assertEqual(encoded[0], encoded[1])

    def test_wrong_raw_identity_and_oversize_preserve_source_without_output(self):
        for ordinal, data, expected in ((2, b'changed', pin(b'original')), (3, b'x' * (archive.MAX_CAPTURE_BYTES + 1), pin(b'x'))):
            stage, name, _ = self.capture(data, ordinal)
            with self.assertRaises(ValueError):
                self.publish(stage, name, expected)
            self.assertEqual((stage / name).read_bytes(), data)
            self.assertEqual([p.name for p in stage.iterdir()], [name])

    def test_exact_new_scope_names_and_freeze_only(self):
        stage, name, expected = self.capture()
        for folder, capture_name in ((stage.parent / 'count-other', name), (stage, '../count.stderr'),
                (stage, 'generate.stderr'), (self.root / 'development-01' / 'count', name),
                (self.root / 'development-09' / 'count', name), (self.root.parent / 'old-count', name)):
            with self.assertRaises(ValueError):
                self.publish(folder, capture_name, expected)
        for frozen in ('', 'A' * 64, True):
            with self.assertRaises(ValueError):
                archive.archive_capture(stage, name, expected=expected, freeze_sha256=frozen)
        self.assertTrue((stage / name).exists())

    def test_every_occupied_transaction_file_prevents_overwrite(self):
        for ordinal, suffix in enumerate(('.gz', '.gz.pending', '.archive.json', '.archive.json.pending'), 2):
            stage, name, expected = self.capture(ordinal=ordinal)
            occupied = stage / (name + suffix)
            occupied.write_bytes(b'preserve unrelated prior attempt')
            before = {p.name: p.read_bytes() for p in stage.iterdir()}
            with self.assertRaises(FileExistsError):
                self.publish(stage, name, expected)
            self.assertEqual({p.name: p.read_bytes() for p in stage.iterdir()}, before)

    def test_open_writer_and_existing_read_guard_refuse_delete_access(self):
        stage, name, expected = self.capture()
        for open_handle in (lambda: (stage / name).open('r+b'), lambda: _protected_reader(stage / name)):
            with open_handle():
                with self.assertRaises(OSError):
                    self.publish(stage, name, expected)
            self.assertEqual([p.name for p in stage.iterdir()], [name])
        binding = self.publish(stage, name, expected)
        self.assertEqual(self.read(stage, name, expected, binding), b'zero\x00\xff\r\nline\nbackslash\\\n')

    def test_transient_writes_rename_and_delete_are_denied_on_source(self):
        data = b'protected bytes\n' * 10000
        stage, name, expected = self.capture(data)
        tried = []
        def cancel():
            if not tried and (stage / (name + '.gz.pending')).exists():
                for operation in (lambda: (stage / name).write_bytes(b'corrupt'),
                        lambda: (stage / name).rename(stage / 'replacement'), lambda: (stage / name).unlink()):
                    with self.assertRaises(OSError):
                        operation()
                tried.append(True)
            return False
        binding = self.publish(stage, name, expected, cancel=cancel)
        self.assertEqual(tried, [True])
        self.assertEqual(self.read(stage, name, expected, binding), data)

    def test_cancellation_at_each_persistent_boundary_preserves_raw(self):
        for ordinal, suffix in enumerate(('.gz.pending', '.gz', '.archive.json'), 2):
            stage, name, expected = self.capture(b'a' * 200000, ordinal)
            observed = stage / (name + suffix)
            with self.assertRaises(InterruptedError):
                self.publish(stage, name, expected, cancel=lambda: observed.exists() and observed.stat().st_size > 0)
            self.assertEqual(pin((stage / name).read_bytes()), expected)
            self.assertTrue(observed.exists())

    def test_flush_and_each_rename_failure_preserve_raw_and_artifacts(self):
        stage, name, expected = self.capture()
        with patch.object(archive.os, 'fsync', side_effect=OSError('disk flush failed')):
            with self.assertRaises(OSError):
                self.publish(stage, name, expected)
        self.assertTrue((stage / name).exists())
        self.assertTrue((stage / (name + '.gz.pending')).exists())
        rename = os.rename
        for ordinal, fail_suffix in ((3, '.gz'), (4, '.archive.json')):
            stage, name, expected = self.capture(ordinal=ordinal)
            def fault(source, target):
                if str(target).endswith(fail_suffix):
                    raise OSError('publication failed')
                return rename(source, target)
            with patch.object(archive.os, 'rename', side_effect=fault):
                with self.assertRaises(OSError):
                    self.publish(stage, name, expected)
            self.assertTrue((stage / name).exists())
            self.assertTrue(any(p.name.endswith('.pending') for p in stage.iterdir()))

    def test_committed_receipt_and_archive_survive_failed_disposition(self):
        stage, name, expected = self.capture()
        with patch.object(archive, '_mark_delete', side_effect=OSError('delete not permitted')):
            with self.assertRaises(OSError):
                self.publish(stage, name, expected)
        before = {p.name: p.read_bytes() for p in stage.iterdir()}
        self.assertEqual(set(before), {name, name + '.gz', name + '.archive.json'})
        self.assertEqual(gzip.decompress(before[name + '.gz']), before[name])
        with self.assertRaises(FileExistsError):
            self.publish(stage, name, expected)
        self.assertEqual({p.name: p.read_bytes() for p in stage.iterdir()}, before)

    def test_hardlinked_source_archive_and_receipt_refused(self):
        stage, name, expected = self.capture()
        alias = stage / 'alias'
        os.link(stage / name, alias)
        with self.assertRaises(ValueError):
            self.publish(stage, name, expected)
        alias.unlink()
        binding = self.publish(stage, name, expected)
        for suffix in ('.gz', '.archive.json'):
            os.link(stage / (name + suffix), alias)
            with self.assertRaises(ValueError):
                self.read(stage, name, expected, binding)
            alias.unlink()

    def test_symbolic_source_and_parent_refused(self):
        stage, name, expected = self.capture()
        target = stage / 'original'
        (stage / name).rename(target)
        try:
            os.symlink(target, stage / name)
        except OSError as error:
            self.skipTest('Creating Windows symlinks is unavailable: ' + str(error))
        with self.assertRaises(ValueError):
            self.publish(stage, name, expected)
        (stage / name).unlink()
        target.rename(stage / name)
        linked = self.root / 'development-03'
        os.symlink(stage.parent, linked, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.publish(linked / 'count', name, expected)
        self.assertEqual(pin((stage / name).read_bytes()), expected)

    def test_held_reader_prevents_archive_and_receipt_replacement(self):
        stage, name, expected = self.capture()
        binding = self.publish(stage, name, expected)
        before = {p.name: p.read_bytes() for p in stage.iterdir()}
        with ExitStack() as stack:
            archive.read_capture(stack, stage, name, expected=expected, binding=binding, freeze_sha256=self.freeze)
            for path in stage.iterdir():
                for operation in (lambda p=path: p.write_bytes(b'corrupt'), lambda p=path: p.unlink()):
                    with self.assertRaises(OSError):
                        operation()
        self.assertEqual({p.name: p.read_bytes() for p in stage.iterdir()}, before)

    def test_reader_rejects_corrupt_and_multi_member_archives_even_with_compressed_pin(self):
        raw = b'exact raw bytes'
        encoded = gzip.compress(raw, mtime=0)
        damaged = bytearray(encoded)
        damaged[-8] ^= 1
        cases = (encoded[:-1], bytes(damaged), encoded + b'junk', encoded + gzip.compress(b'extra', mtime=0))
        for ordinal, bad in enumerate(cases, 2):
            stage, name, binding = self.install_encoded(bad, pin(raw), ordinal)
            before = {p.name: p.read_bytes() for p in stage.iterdir()}
            with self.assertRaises(ValueError):
                self.read(stage, name, pin(raw), binding)
            self.assertEqual({p.name: p.read_bytes() for p in stage.iterdir()}, before)

    def test_reader_rejects_bomb_and_wrong_expanded_hash(self):
        expected = pin(b'x' * archive.MAX_CAPTURE_BYTES)
        stage, name, binding = self.install_encoded(gzip.compress(b'x' * (4 * archive.MAX_CAPTURE_BYTES), mtime=0), expected)
        with self.assertRaises(ValueError):
            self.read(stage, name, expected, binding)
        expected = pin(b'wrong raw bytes')
        stage, name, binding = self.install_encoded(gzip.compress(b'other raw bytes', mtime=0), expected, 3)
        with self.assertRaises(ValueError):
            self.read(stage, name, expected, binding)

    def test_reader_rejects_missing_tampered_and_cross_scope_bindings(self):
        stage, name, expected = self.capture()
        binding = self.publish(stage, name, expected)
        for field in ('archive', 'receipt'):
            path = stage / binding[field]['name']
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaises(ValueError):
                self.read(stage, name, expected, binding)
            path.write_bytes(original)
            path.unlink()
            with self.assertRaises(OSError):
                self.read(stage, name, expected, binding)
            path.write_bytes(original)
        for change in ({'ordinal': 3}, {'stage': 'generate'}, {'freeze_sha256': 'b' * 64}, {'ordinal': True}):
            bad = json.loads(json.dumps(binding));bad['ownership'].update(change)
            with self.assertRaises(ValueError):
                self.read(stage, name, expected, bad)
        with self.assertRaises(ValueError):
            self.read(stage, name, {'bytes': expected['bytes'], 'sha256': 'f' * 64}, binding)

    def test_reader_cancellation_and_caller_mutation_never_recreate_raw(self):
        stage, name, expected = self.capture()
        binding = self.publish(stage, name, expected)
        with self.assertRaises(InterruptedError):
            self.read(stage, name, expected, binding, cancel=lambda: True)
        calls = []
        def mutate():
            if not calls:
                calls.append(True)
            elif len(calls) == 1:
                binding['archive']['name'] = '../outside'
                calls.append(True)
            return False
        self.assertEqual(self.read(stage, name, expected, binding, cancel=mutate), b'zero\x00\xff\r\nline\nbackslash\\\n')
        self.assertFalse((stage / name).exists())


if __name__ == '__main__':
    unittest.main()
