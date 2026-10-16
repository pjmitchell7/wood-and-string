"""Protected, pinned Vulkan trial package sharing the existing verified GGUF.

No acquisition, installation, native import or execution. Keep protect_runtime
open through native termination. CPU package protection is nested, never edited.
Directory closure is observed, not protected by an ACL; OS files are not locked.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile
import zlib

from . import local_arranger_runtime as cpu

ROOT = Path(__file__).resolve().parent / 'research/local-arranger-vulkan-001'
VERSION = 'local-arranger-qwen3-4b-vulkan-b11669-v1'
RUNTIME = {
    'repository': 'ggml-org/llama.cpp', 'tag': 'b11669',
    'commit': '620d2225cc436ab0b001e3200e590e3b286ef9ab', 'asset_id': 644420898,
    'file': 'llama-b11669-bin-win-vulkan-x64.zip', 'bytes': 32182350,
    'sha256': '2c07574e7bde71321cb785282b20cc821e951132149eeceeea32ddebdbf0598e',
    'url': 'https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-win-vulkan-x64.zip',
    'expanded_bytes': 91423005, 'members': 52,
    'executable': 'llama-completion.exe', 'tokenize': 'llama-tokenize.exe',
    'metadata': {
        'acquisition.json': (3863, '6be797408c8c9a73428f6c05f4c5b84fdba4d0c710916c820037d87ab70ba656'),
        'LICENSE-LLVM-OpenMP.body': (6009, 'dede80b44a3469f53a7850e4161f6fde5f221477c5fec31432ffd7939aa1f1d2'),
        'LICENSE-LLVM-OpenMP.header': (30, '53e1a340d6aa31064a98d5850443692703f10a907143a4b2d0782785a594b999'),
        'metadata-receipt.json': (8283, '503ef8bd44ec3a6d2f9b3fca8161ac70ed96d24ecef0879e517e4dc08acdc0aa'),
        'plan.json': (2730, '74f47d6f145511f5634f8c487c94daff5c2d2b766eba20d4c0cf334caadc45b6'),
        'publisher-asset.json': (1893, 'a8eb5236c6aa367cd980c211564e302a01fee60541853dc8cb565e991b737493'),
        'runtime-inventory.json': (9389, '8b655edaa6999b5b6d3b5f0afb734028341e5e8f31ed243378fc65cd765ee47a'),
        'static-dependencies.json': (83360, '0fbf9dad8e998c7df45bc658fb8e81000f39f7cb86b0c5f7ba42308c0979e92c'),
        'zip-central-inventory.json': (12199, 'e68f470527a59022b65728df181bee746fa94ca9b080160ddf79738cf562166d'),
        'zip-tail.bin': (65536, 'c9e402c317b855c77662baa02f32e70db04ce81f79c99c6137264cee7acd36a8'),
        'notices/LICENSE-LLVM-OpenMP': (19741, 'fdad1758a9e1f9d5a81e18879b3406772115edc92c24bfa36b70c654f325e8e4'),
        'notices/llama-source-MIT-LICENSE': (1078, '94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d'),
    },
}
IDENTITY_KEYS = ('repository', 'tag', 'commit', 'asset_id', 'file', 'bytes', 'sha256', 'url')
CENTRAL_KEYS = {'path', 'compressed_bytes', 'expanded_bytes', 'crc32', 'method',
                'flags', 'external_attr', 'local_offset'}


def _root():
    root = ROOT.absolute()
    for path in reversed((root, *root.parents)):
        cpu._regular(path, True)
    return root


def _layout(root, inventory):
    expected = {RUNTIME['file'], 'runtime', 'notices',
                *(name for name in RUNTIME['metadata'] if '/' not in name)}
    found = set()
    for path in root.iterdir():
        cpu._name(path.name)
        cpu._regular(path, path.name in ('runtime', 'notices'))
        found.add(path.name)
    if found != expected:
        raise ValueError('Vulkan package has missing or extra entries')
    cpu._closure(root / 'runtime', inventory)
    cpu._closure(root / 'notices', [name.split('/')[1] for name in RUNTIME['metadata']
                                  if name.startswith('notices/')])


def _snapshot(read=cpu._small, cancel=lambda: False):
    cpu._check(cancel)
    root = _root(); metadata = {}; digests = {}
    for name, (size, digest) in RUNTIME['metadata'].items():
        cpu._check(cancel)
        parts = name.split('/')
        if len(parts) > 2 or (len(parts) == 2 and parts[0] != 'notices'):
            raise ValueError('Invalid fixed Vulkan metadata path')
        for part in parts: cpu._name(part)
        data = read(root / name)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Vulkan metadata pin changed: ' + name)
        metadata[name] = data; digests[name] = digest
    plan = cpu._json(metadata['plan.json']); receipt = cpu._json(metadata['acquisition.json'])
    inspected = cpu._json(metadata['metadata-receipt.json'])
    publisher = cpu._json(metadata['publisher-asset.json'])
    if (receipt.get('state') != 'complete' or receipt.get('native_executed') is not False
            or receipt.get('model_loaded') is not False or receipt.get('status') != 200
            or receipt.get('transferred_bytes') != RUNTIME['bytes'] or receipt.get('sha256') != RUNTIME['sha256']):
        raise ValueError('Vulkan acquisition is not the completed verified attempt')
    if (inspected.get('state') != 'inspected' or inspected.get('native_executed') is not False
            or inspected.get('model_loaded') is not False
            or inspected.get('members') != RUNTIME['members']
            or inspected.get('expanded_bytes') != RUNTIME['expanded_bytes']):
        raise ValueError('Vulkan inspection identity differs')
    if plan.get('state') != 'inspected_awaiting_root_review_before_full_fetch':
        raise ValueError('Vulkan plan identity differs')
    for value in (receipt.get('runtime', {}), plan.get('runtime', {})):
        if any(value.get(key) != RUNTIME[key] for key in IDENTITY_KEYS):
            raise ValueError('Vulkan runtime identity differs from publisher pins')
    if any(plan['runtime'].get(key) != RUNTIME[key] for key in ('expanded_bytes', 'members')):
        raise ValueError('Vulkan plan geometry differs')
    if (publisher.get('id') != RUNTIME['asset_id'] or publisher.get('name') != RUNTIME['file']
            or publisher.get('size') != RUNTIME['bytes'] or publisher.get('digest') != 'sha256:' + RUNTIME['sha256']
            or publisher.get('browser_download_url') != RUNTIME['url']):
        raise ValueError('Vulkan publisher metadata differs')
    for field, name in [('plan_sha256', 'plan.json'), ('runtime_inventory_sha256', 'runtime-inventory.json'),
                        ('static_dependencies_sha256', 'static-dependencies.json')]:
        if receipt.get(field) != digests[name]:
            raise ValueError('Vulkan receipt references changed evidence')
    if receipt.get('setup_sha256') != plan.get('setup_sha256'):
        raise ValueError('Vulkan acquisition/setup identities differ')
    shared = plan.get('same_model_reused_without_copy', {})
    expected_model = cpu.ROOT.absolute() / cpu.MODEL['file']
    if (any(shared.get(key) != cpu.MODEL[key] for key in ('file', 'bytes', 'sha256', 'revision'))
            or shared.get('path') != str(expected_model)):
        raise ValueError('Vulkan must reference the existing pinned CPU GGUF')
    notice_rows = [{'path': name.split('/')[1], 'bytes': len(data), 'sha256': digests[name]}
                   for name, data in metadata.items() if name.startswith('notices/')]
    if (type(plan.get('notices')) is not list or len(plan['notices']) != len(notice_rows)
            or sorted(plan['notices'], key=lambda r: r['path']) != sorted(notice_rows, key=lambda r: r['path'])):
        raise ValueError('Vulkan notices differ from pinned evidence')
    rows = cpu._json(metadata['runtime-inventory.json']); inventory = {}; lower = set()
    if type(rows) is not list or len(rows) != RUNTIME['members']:
        raise ValueError('Vulkan inventory member count differs')
    for row in rows:
        if type(row) is not dict or set(row) != {'path', 'bytes', 'sha256', 'zip_crc32'}:
            raise ValueError('Invalid Vulkan inventory fields')
        name = cpu._name(row['path'])
        if (name.casefold() in lower or type(row['bytes']) is not int or not 0 < row['bytes'] <= RUNTIME['expanded_bytes']
                or type(row['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', row['sha256'])
                or type(row['zip_crc32']) is not str or not re.fullmatch('[0-9a-f]{8}', row['zip_crc32'])):
            raise ValueError('Invalid Vulkan inventory identity')
        lower.add(name.casefold()); inventory[name] = row
    if sum(row['bytes'] for row in rows) != RUNTIME['expanded_bytes']:
        raise ValueError('Vulkan expanded size differs')
    if not {RUNTIME['executable'], RUNTIME['tokenize'], 'ggml-vulkan.dll'} <= inventory.keys():
        raise ValueError('Vulkan native entry points missing')
    central = cpu._json(metadata['zip-central-inventory.json'])
    if (type(central) is not list or len(central) != len(rows)
            or any(type(row) is not dict or set(row) != CENTRAL_KEYS for row in central)):
        raise ValueError('Invalid Vulkan central inventory')
    _layout(root, inventory)
    if cpu._regular(root / RUNTIME['file']).st_size != RUNTIME['bytes']:
        raise ValueError('Vulkan archive size differs')
    for name, row in inventory.items():
        if cpu._regular(root / 'runtime' / name).st_size != row['bytes']:
            raise ValueError('Vulkan installed member size differs')
    systems = cpu._dependencies(root, inventory, cpu._json(metadata['static-dependencies.json']))
    return root, inventory, systems, digests, central


def _receipt(root, inventory, systems, digests, shared):
    identity = {'version': VERSION, 'runtime': RUNTIME, 'manifest_sha256': digests,
                'shared_cpu_identity_sha256': shared['identity_sha256']}
    return {'ready': True, 'reason': None, 'version': VERSION, 'runtime_version': VERSION,
            'executable': str(root / 'runtime' / RUNTIME['executable']),
            'tokenize': str(root / 'runtime' / RUNTIME['tokenize']), 'runtime_root': str(root / 'runtime'),
            'model': shared['model'], 'model_sha256': cpu.MODEL['sha256'],
            'runtime_archive_sha256': RUNTIME['sha256'], 'manifest_sha256': dict(digests),
            'shared_cpu_identity_sha256': shared['identity_sha256'],
            'identity_sha256': hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'native_inputs': [{'path': shared['model'], 'bytes': cpu.MODEL['bytes'], 'sha256': cpu.MODEL['sha256']},
                              *[{'path': str(root / 'runtime' / n), 'bytes': r['bytes'], 'sha256': r['sha256']}
                                for n, r in sorted(inventory.items())]],
            'observed_system_dependencies': systems, 'full_verified': False, 'protection_active': False,
            'limits': 'Static normal import closure and existing package protection only. No native/GPU execution, dynamic-loader, CPU/GPU compatibility or musical-quality claim; System32 files are not locked.'}


@contextmanager
def protect_runtime(cancel=lambda: False):
    """Fully verify and protect both packages; share the original model in place."""
    from .full_decode import _protected_reader
    cpu._check(cancel); root = _root()
    with ExitStack() as stack:
        handles = {}
        def owned(path):
            cpu._check(cancel)
            if path not in handles:
                before = cpu._regular(path); stream = stack.enter_context(_protected_reader(path))
                after = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
                    raise ValueError('Vulkan owned file changed while opening')
                handles[path] = stream
            return handles[path]
        def read(path):
            stream = owned(path)
            if os.fstat(stream.fileno()).st_size > cpu.MAX_METADATA:
                raise ValueError('Oversize Vulkan metadata')
            stream.seek(0); return stream.read(cpu.MAX_METADATA + 1)
        root, inventory, systems, digests, central = _snapshot(read, cancel)
        archive_stream = owned(root / RUNTIME['file'])
        for name in inventory: owned(root / 'runtime' / name)
        shared = stack.enter_context(cpu.protect_runtime(cancel=cancel))
        if (shared.get('full_verified') is not True or shared.get('protection_active') is not True
                or shared.get('model') != str(cpu.ROOT.absolute() / cpu.MODEL['file'])
                or shared.get('model_sha256') != cpu.MODEL['sha256']):
            raise ValueError('Existing CPU GGUF protection was not established')
        count, digest, _ = cpu._digest(archive_stream, cancel)
        if (count, digest) != (RUNTIME['bytes'], RUNTIME['sha256']):
            raise ValueError('Pinned Vulkan archive failed verification')
        archive_stream.seek(0)
        with zipfile.ZipFile(archive_stream) as archive:
            entries = archive.infolist(); names = []
            if len(entries) != RUNTIME['members'] or sum(i.file_size for i in entries) != RUNTIME['expanded_bytes']:
                raise ValueError('Vulkan ZIP geometry differs')
            for info, inspected in zip(entries, central):
                cpu._check(cancel); name = cpu._name(info.filename); names.append(name)
                mode = info.external_attr >> 16
                if (info.is_dir() or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or info.external_attr & 0x10 or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG))):
                    raise ValueError('Unsupported Vulkan ZIP member type')
                observed = {'path': name, 'compressed_bytes': info.compress_size, 'expanded_bytes': info.file_size,
                            'crc32': f'{info.CRC:08x}', 'method': info.compress_type, 'flags': info.flag_bits,
                            'external_attr': info.external_attr, 'local_offset': info.header_offset}
                if observed != inspected:
                    raise ValueError('Vulkan ZIP central metadata differs')
                row = inventory.get(name)
                if row is None or row['bytes'] != info.file_size or row['zip_crc32'] != f'{info.CRC:08x}':
                    raise ValueError('Vulkan ZIP inventory differs')
                with archive.open(info) as original:
                    h = hashlib.sha256(); crc = 0; count = 0
                    while True:
                        cpu._check(cancel); data = original.read(cpu.CHUNK)
                        if not data: break
                        count += len(data); h.update(data); crc = zlib.crc32(data, crc)
                        if count > row['bytes']: raise ValueError('Vulkan ZIP member exceeds bound')
                expected = row['bytes'], row['sha256'], row['zip_crc32']
                if ((count, h.hexdigest(), f'{crc:08x}') != expected
                        or cpu._digest(owned(root / 'runtime' / name), cancel) != expected):
                    raise ValueError('Vulkan installed member differs from authenticated ZIP bytes')
                notice_name = 'notices/' + name
                if notice_name in digests and row['sha256'] != digests[notice_name]:
                    raise ValueError('Vulkan bundled notice differs from retained notice')
            if len({n.casefold() for n in names}) != len(names) or set(names) != set(inventory):
                raise ValueError('Vulkan ZIP member collision')
        for item in systems:
            cpu._check(cancel); path = Path(item['path']); before = cpu._regular(path, owned=False)
            with path.open('rb') as stream: count, digest, _ = cpu._digest(stream, cancel)
            after = cpu._regular(path, owned=False)
            if ((count, digest) != (item['bytes'], item['sha256'])
                    or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
                raise ValueError('Observed Vulkan System32 dependency changed')
        cpu._check(cancel); _layout(root, inventory)
        receipt = _receipt(root, inventory, systems, digests, shared)
        receipt.update(full_verified=True, protection_active=True,
                       owned_file_protection='Both packages held with Windows GENERIC_READ / FILE_SHARE_READ through context exit; System32 not locked')
        try: yield receipt
        finally: _layout(root, inventory)


def require_runtime(cancel=lambda: False):
    with protect_runtime(cancel=cancel) as receipt:
        result = dict(receipt); result['protection_active'] = False
    return result


def runtime_status():
    """Cheap structural discovery only; native callers still need protection."""
    result = {'ready': False, 'reason': 'Private Vulkan package unavailable', 'version': VERSION,
              'full_verified': False, 'protection_active': False}
    try:
        root, inventory, systems, digests, _ = _snapshot()
        shared = cpu.runtime_status()
        if not shared['ready']: raise ValueError('Shared CPU package unavailable: ' + str(shared['reason']))
        result = _receipt(root, inventory, systems, digests, shared)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        result['reason'] = str(error)
    return result
