"""Owned tiny GPU-package fixtures; no native/driver/model execution."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import zlib

from audio import local_arranger_gpu_runtime as gpu
from audio import local_arranger_runtime as cpu
from audio.full_decode import _protected_reader


def sha(data): return hashlib.sha256(data).hexdigest()


class GpuRuntimeGuard(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'gpu'; self.root.mkdir()
        self.native = self.root / 'runtime'; self.native.mkdir()
        self.notices = self.root / 'notices'; self.notices.mkdir()
        self.cpu_root = self.directory / 'cpu'; self.cpu_root.mkdir()
        self.model_pins = dict(file='tiny.gguf', bytes=32, sha256=sha(b'm' * 32), revision='fixture')
        self.model = self.cpu_root / 'tiny.gguf'; self.model.write_bytes(b'm' * 32)
        system = self.directory / 'system'; system.mkdir()
        self.system = system / 'base.dll'; self.system.write_bytes(b'OS fixture, never executed')
        self.files = {'llama-completion.exe': b'completion fixture', 'llama-tokenize.exe': b'token fixture',
                      'ggml-vulkan.dll': b'Vulkan fixture', 'LICENSE-LLVM-OpenMP': b'OpenMP notice'}
        archive = self.root / 'tiny.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name, data in self.files.items(): z.writestr(name, data); (self.native / name).write_bytes(data)
        with zipfile.ZipFile(archive) as z:
            central = [dict(path=i.filename, compressed_bytes=i.compress_size, expanded_bytes=i.file_size,
                            crc32=f'{i.CRC:08x}', method=i.compress_type, flags=i.flag_bits,
                            external_attr=i.external_attr, local_offset=i.header_offset) for i in z.infolist()]
        self.pins = dict(repository='fixture-repo', tag='fixture-tag', commit='fixture-commit', asset_id=1,
                         file='tiny.zip', bytes=archive.stat().st_size, sha256=sha(archive.read_bytes()),
                         url='https://example.invalid/fixture.zip', expanded_bytes=sum(map(len, self.files.values())),
                         members=len(self.files), executable='llama-completion.exe', tokenize='llama-tokenize.exe', metadata={})
        inventory = [dict(path=n, bytes=len(d), sha256=sha(d), zip_crc32=f'{zlib.crc32(d):08x}') for n, d in self.files.items()]
        self.write('runtime-inventory.json', inventory); self.write('zip-central-inventory.json', central)
        binaries = [dict(path=str(self.native / n), bytes=len(d), sha256=sha(d)) for n, d in self.files.items()
                    if n.endswith(('.exe', '.dll'))]
        binaries.append(dict(path=str(self.system), bytes=self.system.stat().st_size, sha256=sha(self.system.read_bytes())))
        self.write('static-dependencies.json', dict(binaries=binaries,
            imports={b['path']: ([] if b['path'] == str(self.system) else ['base.dll', 'api-ms-fixture.dll']) for b in binaries},
            windows_api_set_contracts=['api-ms-fixture.dll'], unresolved=[], limitations='Fixture normal import evidence only.'))
        for name, data in {'LICENSE-LLVM-OpenMP': self.files['LICENSE-LLVM-OpenMP'], 'llama-source-MIT-LICENSE': b'MIT fixture'}.items():
            (self.notices / name).write_bytes(data)
        notices = [dict(path=p.name, bytes=p.stat().st_size, sha256=sha(p.read_bytes())) for p in self.notices.iterdir()]
        self.plan = dict(state='inspected_awaiting_root_review_before_full_fetch', runtime={k: v for k, v in self.pins.items() if k != 'metadata'},
                         same_model_reused_without_copy=dict(self.model_pins, path=str(self.model)), notices=notices, setup_sha256='a' * 64)
        self.write('plan.json', self.plan)
        self.write('publisher-asset.json', dict(id=1, name='tiny.zip', size=self.pins['bytes'],
            digest='sha256:' + self.pins['sha256'], browser_download_url=self.pins['url']))
        self.write('metadata-receipt.json', dict(state='inspected', native_executed=False, model_loaded=False,
            members=len(self.files), expanded_bytes=self.pins['expanded_bytes']))
        self.acquisition = dict(state='complete', native_executed=False, model_loaded=False, status=200,
            transferred_bytes=self.pins['bytes'], sha256=self.pins['sha256'], setup_sha256='a' * 64,
            runtime={k: self.pins[k] for k in gpu.IDENTITY_KEYS})
        self.link_receipt(); self.repin()
        self.cpu_held = False; self.cpu_entries = 0
        self.base = dict(ready=True, reason=None, model=str(self.model), model_sha256=self.model_pins['sha256'],
                         identity_sha256='b' * 64, full_verified=True, protection_active=True)
        @contextmanager
        def cpu_guard(cancel=lambda: False):
            cpu._check(cancel); self.cpu_entries += 1
            cpu._regular(self.model)
            with _protected_reader(self.model) as stream:
                if sha(stream.read()) != self.model_pins['sha256']: raise ValueError('Shared CPU model failed verification')
                self.cpu_held = True
                try: yield deepcopy(self.base)
                finally: self.cpu_held = False
        stack = ExitStack(); self.addCleanup(stack.close)
        for module, name, value in [(gpu, 'ROOT', self.root), (gpu, 'RUNTIME', self.pins),
                                    (cpu, 'ROOT', self.cpu_root), (cpu, 'MODEL', self.model_pins),
                                    (cpu, 'SYSTEM32', system), (cpu, 'protect_runtime', cpu_guard),
                                    (cpu, 'runtime_status', lambda: dict(self.base, full_verified=False, protection_active=False))]:
            stack.enter_context(patch.object(module, name, value))

    def write(self, name, value): (self.root / name).write_text(json.dumps(value, indent=2), encoding='utf-8')
    def load(self, name): return json.loads((self.root / name).read_bytes())
    def repin(self):
        files = [*self.root.glob('*.json'), *self.notices.iterdir()]
        self.pins['metadata'] = {p.relative_to(self.root).as_posix(): (p.stat().st_size, sha(p.read_bytes())) for p in files}
    def link_receipt(self):
        for field, name in [('plan_sha256', 'plan.json'), ('runtime_inventory_sha256', 'runtime-inventory.json'),
                            ('static_dependencies_sha256', 'static-dependencies.json')]:
            self.acquisition[field] = sha((self.root / name).read_bytes())
        self.write('acquisition.json', self.acquisition)
    def released(self):
        self.assertFalse(self.cpu_held)
        for p in (self.model, self.root / 'tiny.zip', self.native / 'ggml-vulkan.dll', self.root / 'plan.json'):
            with p.open('r+b'): pass

    def test_status_is_cheap_then_full_guard_shares_model_with_stable_json_identity(self):
        with patch.object(cpu, '_digest', side_effect=AssertionError('Cheap discovery hashed a native file')):
            status = gpu.runtime_status(); self.assertTrue(status['ready']); self.assertFalse(status['full_verified'])
        first = gpu.require_runtime(); second = gpu.require_runtime()
        self.assertTrue(first['full_verified']); self.assertFalse(first['protection_active'])
        self.assertEqual(first, second); json.dumps(first, allow_nan=False)
        self.assertEqual(first['model'], str(self.model)); self.assertEqual(first['shared_cpu_identity_sha256'], 'b' * 64)
        self.assertEqual(first['identity_sha256'], status['identity_sha256'])
        self.assertFalse((self.root / 'tiny.gguf').exists()); self.assertEqual(self.cpu_entries, 2); self.released()

    def test_same_size_native_and_archive_tampering_rejected(self):
        for path in (self.native / 'ggml-vulkan.dll', self.root / 'tiny.zip'):
            with self.subTest(file=path.name):
                original = path.read_bytes(); path.write_bytes(b'x' * len(original))
                with self.assertRaises(ValueError): gpu.require_runtime()
                path.write_bytes(original); self.released()

    def test_forged_inventory_cannot_replace_authenticated_archive(self):
        path = self.native / 'ggml-vulkan.dll'; path.write_bytes(b'x' * path.stat().st_size)
        rows = self.load('runtime-inventory.json'); deps = self.load('static-dependencies.json')
        for row in rows:
            if row['path'] == path.name: row['sha256'] = sha(path.read_bytes())
        for row in deps['binaries']:
            if row['path'] == str(path): row['sha256'] = sha(path.read_bytes())
        self.write('runtime-inventory.json', rows); self.write('static-dependencies.json', deps); self.link_receipt(); self.repin()
        with self.assertRaisesRegex(ValueError, 'authenticated ZIP'): gpu.require_runtime()
        self.released()

    def test_metadata_pins_and_stale_receipt_fail_before_shared_model(self):
        original = (self.root / 'acquisition.json').read_bytes()
        (self.root / 'acquisition.json').write_bytes(original.replace(b'complete', b'xxxxxxxx'))
        self.assertFalse(gpu.runtime_status()['ready'])
        (self.root / 'acquisition.json').write_bytes(original)
        self.acquisition['state'] = 'failed'; self.link_receipt(); self.repin()
        with self.assertRaisesRegex(ValueError, 'completed verified'): gpu.require_runtime()
        self.assertEqual(self.cpu_entries, 0)

    def test_wrong_publisher_and_shared_model_reference_rejected_even_if_repinned(self):
        publisher = self.load('publisher-asset.json'); publisher['digest'] = 'sha256:' + '0' * 64
        self.write('publisher-asset.json', publisher); self.repin()
        with self.assertRaisesRegex(ValueError, 'publisher'): gpu.require_runtime()
        publisher['digest'] = 'sha256:' + self.pins['sha256']; self.write('publisher-asset.json', publisher)
        self.plan['same_model_reused_without_copy']['path'] = str(self.root / 'tiny.gguf')
        self.write('plan.json', self.plan); self.link_receipt(); self.repin()
        with self.assertRaisesRegex(ValueError, 'existing pinned CPU'): gpu.require_runtime()

    def test_exact_root_runtime_and_notice_layout(self):
        for path in (self.root / 'unexpected.exe', self.native / 'unexpected.dll', self.notices / 'extra'):
            with self.subTest(path=path.name):
                path.write_bytes(b'owned fixture')
                with self.assertRaises(ValueError): gpu.require_runtime()
                path.unlink()
        directory = self.native / 'nested'; directory.mkdir()
        with self.assertRaises(ValueError): gpu.require_runtime()

    def test_unsafe_inventory_and_case_collision(self):
        original = self.load('runtime-inventory.json')
        for name in ('../outside.dll', 'NUL.dll', 'folder/file.dll', 'bad:stream', 'LLAMA-TOKENIZE.EXE'):
            with self.subTest(name=name):
                rows = deepcopy(original); rows[0]['path'] = name; self.write('runtime-inventory.json', rows)
                self.link_receipt(); self.repin()
                with self.assertRaises(ValueError): gpu.require_runtime()

    def test_hardlinked_owned_file_rejected_but_system_hardlink_allowed(self):
        os.link(self.system, self.directory / 'system-link')
        self.assertTrue(gpu.require_runtime()['full_verified'])
        target = self.native / 'ggml-vulkan.dll'; os.link(target, self.directory / 'owned-link')
        with self.assertRaisesRegex(ValueError, 'links'): gpu.require_runtime()

    def test_reparse_attribute_is_rejected(self):
        target = self.native / 'ggml-vulkan.dll'; original = Path.lstat
        def linked(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path != target: return value
            return SimpleNamespace(st_mode=value.st_mode, st_nlink=value.st_nlink, st_size=value.st_size,
                                   st_file_attributes=0x400)
        with patch.object(Path, 'lstat', linked):
            with self.assertRaisesRegex(ValueError, 'links'): gpu.require_runtime()

    @unittest.skipUnless(os.name == 'nt', 'Windows share protection')
    def test_both_packages_protected_through_body_and_released_on_body_error(self):
        with self.assertRaisesRegex(RuntimeError, 'body failure'):
            with gpu.protect_runtime() as receipt:
                self.assertTrue(receipt['protection_active']); self.assertTrue(self.cpu_held)
                for path in (self.model, self.native / 'ggml-vulkan.dll', self.root / 'tiny.zip', self.root / 'plan.json'):
                    with self.assertRaises(PermissionError):
                        with path.open('r+b'): pass
                with self.system.open('r+b'): pass
                raise RuntimeError('body failure')
        self.released()

    def test_cancel_and_shared_guard_failure_release_all_gpu_files(self):
        with self.assertRaises(InterruptedError): gpu.require_runtime(cancel=lambda: True)
        cancelled = [False]; original = cpu._digest
        def digest(stream, cancel):
            result = original(stream, cancel)
            cancelled[0] = True  # First GPU digest follows successful shared protection.
            return result
        with patch.object(cpu, '_digest', side_effect=digest):
            with self.assertRaises(InterruptedError): gpu.require_runtime(cancel=lambda: cancelled[0])
        self.released()
        with patch.object(cpu, 'protect_runtime', side_effect=ValueError('shared failure')):
            with self.assertRaisesRegex(ValueError, 'shared failure'): gpu.require_runtime()
        self.released()

    def test_shared_model_tampering_and_unprotected_receipt_rejected(self):
        self.model.write_bytes(b'x' * 32)
        with self.assertRaisesRegex(ValueError, 'CPU model'): gpu.require_runtime()
        self.model.write_bytes(b'm' * 32); self.base['protection_active'] = False
        with self.assertRaisesRegex(ValueError, 'protection'): gpu.require_runtime()
        self.released()

    def test_central_crc_and_static_dependencies_are_not_silently_accepted(self):
        central = self.load('zip-central-inventory.json'); central[0]['crc32'] = '0' * 8
        self.write('zip-central-inventory.json', central); self.repin()
        with self.assertRaisesRegex(ValueError, 'central'): gpu.require_runtime()
        self.released()

    def test_system_tampering_and_unresolved_normal_import_fail_closed(self):
        self.system.write_bytes(b'x' * self.system.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'System32'): gpu.require_runtime()
        deps = self.load('static-dependencies.json'); deps['unresolved'] = ['missing.dll']
        self.write('static-dependencies.json', deps); self.link_receipt(); self.repin()
        with self.assertRaisesRegex(ValueError, 'Unresolved'): gpu.require_runtime()

    def test_new_entry_inside_protected_interval_rejected_at_exit(self):
        with self.assertRaisesRegex(ValueError, 'extra entries'):
            with gpu.protect_runtime(): (self.native / 'late.dll').write_bytes(b'owned fixture')
        self.released()


if __name__ == '__main__': unittest.main()
