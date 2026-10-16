"""One-attempt, fixed-artifact Emilyguitar acquisition; no audio/native execution.

--plan is offline. --acquire additionally requires a root-reviewed admission at
work/electric-bank-plan-001/admission-002.json. Failed files remain; never resume,
retry, overwrite a prior attempt, install a bank, or copy an expanded tree.
"""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import time
import urllib.parse
import urllib.request
import zipfile

APP = Path(__file__).resolve().parents[2]
META = APP / 'work/electric-bank-plan-001'
OUT = APP / 'audio/research/emilyguitar-002'
FIRST = APP / 'audio/research/emilyguitar-001'
VULKAN = APP / 'audio/research/local-arranger-vulkan-001/acquisition.json'
ADMISSION = META / 'admission-002.json'
MIB = 1024**2
CAPS = {'retained_bytes': 7 * 1024**3, 'acquisition_bytes': 5 * 1024**3,
        'free_disk_bytes': 8 * 1024**3}
PIN = {'commit': 'b4920dc662fd9cad6dcaccdeecffdd91c8725d8c',
       'asset_id': 83413459, 'file': 'Karoryfer.Emilyguitar.v1.001.zip',
       'url': 'https://github.com/sfzinstruments/karoryfer.emilyguitar/releases/download/v1.001/Karoryfer.Emilyguitar.v1.001.zip',
       'bytes': 103484350, 'expanded_bytes': 125538629, 'members': 334,
       'files': 331, 'wav_files': 323, 'publisher_sha256': None}
PINS = {
    'emily-tree.json': '95f23b979fed48ef28c93fb5a2f220ad66e8b50a803a29dc89c1eada508cf447',
    'release-directory.json': '2aefa186c3b673f66c152d4f8bd7060250ba57187beb84172875794dbccd1d1b',
    'emily-central-directory.bin': '39a2955644f6bdc71163007d00ed3af475b39c3582c395fc9f647bbdbd995cb3',
    'emily-eocd.bin': 'a4bb0361e964fd830aa594b2ca3575561442a0f2c01a9312ef50a2117bfd3aed',
    'emily-release.json': '6be0722c4b62c1143cc3dc864bd1fccc725236067fa03a3f8f2f48b3ed75a123',
    'emily-tag.json': 'cf599a4ef42157541e81b0a69feac0479e9b9902008baa001e1da8fc19227a03',
    'LICENSE': 'a2010f343487d3f7618affe54f789f5487602331c0a8d03f49e9a7c547cf0499',
}
CD_START = 103458044
CD_BYTES = 26284
METADATA_ALLOWANCE = 2 * MIB
SCRATCH_ALLOWANCE = 64 * MIB
VULKAN_RESERVE = 72 * MIB
ROOTS = ('audio/research', 'audio/vendor/analysis', 'work')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + '.partial')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def final_once(path, value):
    """Create a terminal receipt once; no replacement of any reader-held file.

    Readers must require process termination and valid complete JSON. A killed
    writer can leave incomplete JSON; its existence alone never means success.
    """
    content = json.dumps(value, indent=2, allow_nan=False).encode('utf-8')
    if len(content) > 512 * 1024:
        raise ValueError('Final receipt exceeds metadata bound')
    with path.open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


class ProgressJournal:
    """Exclusive creation, one held writer, bounded/fsynced append records.

    On Windows deny other write/delete opens while allowing readers which share
    writes. A reader may deny delete sharing: appends never replace the file.
    """
    def __init__(self, path, *, max_bytes=128 * 1024, max_records=512):
        self.bytes = self.records = 0
        self.max_bytes, self.max_records = max_bytes, max_records
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            # GENERIC_WRITE, FILE_SHARE_READ, CREATE_NEW, FILE_ATTRIBUTE_NORMAL.
            handle = create(str(path), 0x40000000, 1, None, 1, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
            except BaseException:
                close = kernel.CloseHandle
                close.argtypes = [wintypes.HANDLE]
                close(handle)
                raise
            self.stream = os.fdopen(fd, 'wb', buffering=0)
        else:
            self.stream = path.open('xb', buffering=0)

    def append(self, event):
        row = dict(event, sequence=self.records)
        content = (json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if (len(content) > 1024 or self.records >= self.max_records
                or self.bytes + len(content) > self.max_bytes):
            raise ValueError('Progress journal bound reached')
        if self.stream.write(content) != len(content):
            raise OSError('Incomplete journal write')
        os.fsync(self.stream.fileno())
        self.bytes += len(content)
        self.records += 1

    def close(self):
        self.stream.close()


def regular(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Linked/reparse or nonordinary path: ' + str(path))
    return info


def tree_size(path):
    if not path.exists():
        return 0
    regular(path, directory=True)
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            regular(Path(directory) / name, directory=True)
        for name in files:
            total += regular(Path(directory) / name).st_size
    return total


def resources():
    counts = {name: tree_size(APP / name) for name in ROOTS}
    return {'roots': counts, 'retained_bytes': sum(counts.values()),
            'free_disk_bytes': shutil.disk_usage(APP).free}


def safe_name(name):
    if not isinstance(name, str) or not name.isascii() or '\\' in name or '\0' in name:
        raise ValueError('Unsafe ZIP path')
    path = PurePosixPath(name)
    parts = name[:-1].split('/') if name.endswith('/') else name.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if (path.is_absolute() or len(parts) < 2 or parts[0] != 'Emilyguitar'
            or any(not part or part in ('.', '..') or part.endswith((' ', '.'))
                   or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                   or part.split('.')[0].upper() in reserved for part in parts)):
        raise ValueError('Unsafe or unexpected ZIP path: ' + name)
    return '/'.join(parts[1:])


def metadata():
    for name, expected in PINS.items():
        regular(META / name)
        if sha(META / name) != expected:
            raise ValueError('Reviewed metadata changed: ' + name)
    tree = json.loads((META / 'emily-tree.json').read_text(encoding='utf-8'))
    tag = json.loads((META / 'emily-tag.json').read_text(encoding='utf-8'))
    release = json.loads((META / 'emily-release.json').read_text(encoding='utf-8'))
    asset = next(item for item in release['assets'] if item['id'] == PIN['asset_id'])
    if (tree['sha'] != PIN['commit'] or tree['truncated']
            or tag['object']['sha'] != PIN['commit'] or asset.get('digest') is not None
            or any(asset[key] != PIN[pin] for key, pin in
                   [('name', 'file'), ('size', 'bytes'), ('browser_download_url', 'url')])):
        raise ValueError('Pinned upstream identity differs')
    blobs = {row['path']: row for row in tree['tree'] if row['type'] == 'blob'}
    central = json.loads((META / 'release-directory.json').read_text(encoding='utf-8'))
    rows = central['members']
    if (len(rows) != PIN['members'] or len(blobs) != PIN['files']
            or sum(row['bytes'] for row in rows) != PIN['expanded_bytes']
            or sum(row['size'] for row in blobs.values()) != 125538510
            or any(row['mode'] != '100644' for row in blobs.values())):
        raise ValueError('Pinned inventory differs')
    seen = set()
    files = set()
    for row in rows:
        name = safe_name(row['path'])
        if name.casefold() in seen:
            raise ValueError('Case-colliding member')
        seen.add(name.casefold())
        if not row['path'].endswith('/'):
            files.add(name)
    if files != set(blobs) or sum(name.endswith('.wav') for name in files) != PIN['wav_files']:
        raise ValueError('Archive/source file closure differs')
    return rows, blobs


def ledger():
    regular(VULKAN)
    receipt = json.loads(VULKAN.read_text(encoding='utf-8'))
    if (receipt.get('state') != 'complete' or receipt.get('transferred_bytes') != 32182350
            or receipt.get('charged_cumulative_bytes') != 3463584690
            or receipt.get('sha256') != '2c07574e7bde71321cb785282b20cc821e951132149eeceeea32ddebdbf0598e'):
        raise ValueError('Require the completed reviewed Vulkan acquisition ledger')
    terminal = FIRST / 'terminal-failure.json'
    if sha(terminal) != 'c3a6b323cd68a77282a6d6b71fcee7669586b213e845e993c00456032ddc0c20':
        raise ValueError('First attempt terminal evidence changed')
    failure = json.loads(terminal.read_text(encoding='utf-8'))
    for name, expected in failure['original_receipts_preserved'].items():
        if sha(FIRST / name) != expected:
            raise ValueError('First attempt original receipt changed')
    partial = FIRST / (PIN['file'] + '.partial')
    if (regular(partial).st_size != 67221327 or failure['transferred_bytes'] != 67221327
            or failure['state'] != 'failed' or failure['expanded_bytes'] != 0
            or failure['charged_cumulative_bytes'] != 3532903169
            or sha(FIRST / 'code/acquire_emilyguitar.py') != 'dbca0ab648a5c75984a87a067fc87a9d1c58c4c0457a662dfd447935e62e2bfd'):
        raise ValueError('First attempt accounting/source differs')
    return {'vulkan_receipt_sha256': sha(VULKAN), 'first_attempt_terminal_sha256': sha(terminal),
            'charged_cumulative_bytes': failure['charged_cumulative_bytes'],
            'emily_metadata_already_charged_bytes': METADATA_ALLOWANCE}


def check_budget(current, remaining, charged):
    if current['retained_bytes'] + remaining > CAPS['retained_bytes']:
        raise ValueError('Combined retained cap would be exceeded')
    if current['free_disk_bytes'] - remaining < CAPS['free_disk_bytes']:
        raise ValueError('Free-disk reserve would be exceeded')
    if charged > CAPS['acquisition_bytes']:
        raise ValueError('Cumulative acquisition cap would be exceeded')


def gpu_cache_snapshot():
    baseline_path = APP / 'work/local-arranger-gpu-trial-001/metadata/frozen.json'
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))['shared_cache_before']
    # The four fixed NVIDIA cache paths are evidence only, never modified.
    local = Path(os.environ['LOCALAPPDATA'])
    allowed = {str(local / 'NVIDIA/DXCache'), str(local / 'NVIDIA/GLCache'),
               str(local / 'NVIDIA Corporation/NV_Cache'),
               str(local.parent / 'LocalLow/NVIDIA/DXCache')}
    if set(baseline) != allowed:
        raise ValueError('GPU cache baseline paths differ')
    current = {path: tree_size(Path(path)) for path in sorted(allowed)}
    growth = sum(max(0, current[path] - baseline[path]['bytes']) for path in current)
    if growth > 64 * MIB:
        raise ValueError('Observed shared GPU cache growth exceeds reserved scratch')
    return {'baseline_sha256': sha(baseline_path), 'baseline': baseline,
            'current_bytes_by_path': current, 'positive_growth_bytes': growth,
            'scope': 'Positive logical-byte change in four known caches; no attribution '
                     'to one process or discovery of every possible driver cache.'}


def plan(extra_acquisition=0, other_reserve=VULKAN_RESERVE, gpu_cache_growth=None):
    metadata()
    old = ledger()
    current = resources()
    cache = gpu_cache_snapshot()
    if gpu_cache_growth is None:
        gpu_cache_growth = cache['positive_growth_bytes']
    if (type(extra_acquisition) is not int or extra_acquisition < 0
            or type(other_reserve) is not int or other_reserve < VULKAN_RESERVE
            or type(gpu_cache_growth) is not int or not 0 <= gpu_cache_growth <= 64 * MIB):
        raise ValueError('Invalid external acquisition/reservation ledger')
    if gpu_cache_growth < cache['positive_growth_bytes']:
        raise ValueError('GPU cache grew beyond reviewed admission; refresh the plan')
    if tree_size(META) > METADATA_ALLOWANCE:
        raise ValueError('Emily metadata allowance already exceeded')
    # First failure is retained in live counts. A full fresh metadata reserve is
    # conservative for attempt 002. Prior network metadata is never recharged.
    remaining = PIN['bytes'] + PIN['expanded_bytes'] + METADATA_ALLOWANCE + SCRATCH_ALLOWANCE + other_reserve
    charged = old['charged_cumulative_bytes'] + extra_acquisition + PIN['bytes']
    check_budget(current, remaining, charged)
    return {'version': 1, 'pin': PIN, 'caps': CAPS, 'current': current, 'ledger': old,
            'remaining_retained_reservation_bytes': remaining,
            'projected_retained_bytes': current['retained_bytes'] + remaining,
            'charged_cumulative_if_complete': charged,
            'components': {'archive': PIN['bytes'], 'one_expansion': PIN['expanded_bytes'],
                           'fresh_metadata_reserve': METADATA_ALLOWANCE, 'emily_scratch': SCRATCH_ALLOWANCE,
                           'other_reserved_retained_bytes': other_reserve},
            'gpu_cache_growth_bytes': gpu_cache_growth,
            'gpu_cache_snapshot': cache,
            'gpu_cache_accounting': 'Positive outside-APP cache growth is within the existing 64 MiB '
                                    'Vulkan scratch component of other reserve, not added twice. '
                                    'Full 72 MiB is still reserved conservatively.',
            'admission_template': {'version': 1, 'approved': False, 'approved_at_epoch': None,
                                   'helper_sha256': sha(Path(__file__)),
                                   'vulkan_receipt_sha256': old['vulkan_receipt_sha256'],
                                   'first_attempt_terminal_sha256': old['first_attempt_terminal_sha256'],
                                   'extra_non_emily_acquisition_bytes': extra_acquisition,
                                   'other_reserved_retained_bytes': other_reserve,
                                   'gpu_cache_growth_bytes': gpu_cache_growth,
                                   'gpu_native_phase_terminal': False},
            'limits': 'Logical file lengths include all APP/work; hard links counted per path. '
                      'HTTP body ledger excludes protocol/TLS overhead. No concurrent writer exclusion.'}


def tls_url(url):
    value = urllib.parse.urlsplit(url)
    if (value.scheme != 'https' or value.hostname not in {'github.com', 'release-assets.githubusercontent.com'}
            or value.username or value.password or value.port not in (None, 443)):
        raise ValueError('Expected official TLS asset URL')


def response_headers(response):
    tls_url(response.url)
    if (response.status != 200 or response.headers.get('Content-Length') != str(PIN['bytes'])
            or response.headers.get('Content-Encoding', 'identity') != 'identity'
            or response.headers.get('Transfer-Encoding') is not None
            or response.headers.get('Content-Range') is not None):
        raise ValueError('Exact complete response size/encoding required before body read')


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, receipt):
        self.receipt = receipt

    def redirect_request(self, request, response, code, message, headers, url):
        tls_url(url)
        self.receipt['redirects'].append({'from': request.full_url, 'status': code, 'to': url})
        if len(self.receipt['redirects']) > 4:
            raise ValueError('Too many redirects')
        return super().redirect_request(request, response, code, message, headers, url)


def inspect_zip(path, expected):
    if regular(path).st_size != PIN['bytes']:
        raise ValueError('Complete archive size differs')
    with path.open('rb') as stream:
        stream.seek(CD_START)
        if stream.read(CD_BYTES) != (META / 'emily-central-directory.bin').read_bytes():
            raise ValueError('Full central directory differs from reviewed range')
        if stream.read() != (META / 'emily-eocd.bin').read_bytes():
            raise ValueError('Full ZIP end differs from reviewed range')
    with zipfile.ZipFile(path) as archive, path.open('rb') as raw:
        members = archive.infolist()
        if len(members) != len(expected):
            raise ValueError('Member count differs')
        position = 0
        seen = set()
        for member, row in zip(members, expected):
            name = safe_name(member.filename)
            observed = {'path': member.filename, 'bytes': member.file_size,
                        'compressed_bytes': member.compress_size, 'crc32': f'{member.CRC:08x}',
                        'method': member.compress_type, 'flags': member.flag_bits,
                        'external_attr': member.external_attr, 'local_header_offset': member.header_offset}
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (observed != row or name.casefold() in seen or member.header_offset != position
                    or member.flag_bits != 0 or member.compress_type not in (0, 8)
                    or member.external_attr & 0x400 or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or mode == stat.S_IFDIR and not member.is_dir()
                    or mode == stat.S_IFREG and member.is_dir()):
                raise ValueError('Unsafe or changed central member: ' + member.filename)
            seen.add(name.casefold())
            raw.seek(position)
            header = raw.read(30)
            sig, version, flags, method, _, _, crc, compressed, expanded, namesize, extrasize = struct.unpack('<4s5H3I2H', header)
            if (sig != b'PK\x03\x04' or flags != member.flag_bits or method != member.compress_type
                    or crc != member.CRC or compressed != member.compress_size or expanded != member.file_size
                    or raw.read(namesize).decode('ascii') != member.filename or extrasize != 0):
                raise ValueError('Local header differs: ' + member.filename)
            position += 30 + namesize + extrasize + compressed
        if position != CD_START:
            raise ValueError('Unexpected gaps/overlap/prefix before central directory')


def extract(path, stage, rows, blobs, checkpoint):
    inspect_zip(path, rows)
    stage.mkdir(exist_ok=False)
    inventory = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = safe_name(member.filename)
            target = stage.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(stage.resolve()):
                raise ValueError('Member escaped owned staging')
            if member.is_dir():
                target.mkdir(exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            parent = target.parent
            while parent != stage.parent:
                regular(parent, directory=True)
                parent = parent.parent
            count = crc = 0
            digest = hashlib.sha256()
            git = hashlib.sha1(f'blob {member.file_size}\0'.encode('ascii'))
            with archive.open(member) as source, target.open('xb') as output:
                while block := source.read(min(MIB, member.file_size + 1 - count)):
                    count += len(block)
                    if count > member.file_size:
                        raise ValueError('Expanded member exceeded declared bytes')
                    output.write(block)
                    digest.update(block)
                    git.update(block)
                    crc = binascii.crc32(block, crc)
                output.flush()
                os.fsync(output.fileno())
            if count != member.file_size or crc & 0xffffffff != member.CRC:
                raise ValueError('Expanded member size/CRC differs')
            reference = blobs[name]
            identity = 'exact_git_blob'
            if count != reference['size'] or git.hexdigest() != reference['sha']:
                if name != 'LICENSE':
                    raise ValueError('Exact Git blob differs: ' + name)
                source_license = (META / 'LICENSE').read_bytes()
                data = target.read_bytes()
                if data.replace(b'\r\n', b'\n').rstrip(b'\n') != source_license.replace(b'\r\n', b'\n').rstrip(b'\n'):
                    raise ValueError('LICENSE differs beyond declared line endings/final newline')
                canonical_git = hashlib.sha1(f'blob {len(source_license)}\0'.encode() + source_license).hexdigest()
                if canonical_git != reference['sha']:
                    raise ValueError('Pinned source LICENSE Git identity differs')
                identity = 'LICENSE_only_CRLF_LF_and_final_newline_equivalence'
            inventory.append({'path': name, 'bytes': count, 'sha256': digest.hexdigest(),
                              'crc32': f'{crc & 0xffffffff:08x}', 'actual_git_blob_sha1': git.hexdigest(),
                              'expected_git_blob_sha1': reference['sha'], 'identity_check': identity})
            checkpoint(count)
    if len(inventory) != PIN['files'] or tree_size(stage) != PIN['expanded_bytes']:
        raise ValueError('Expanded file closure/size differs')
    return inventory


def acquire():
    regular(ADMISSION)
    admission = json.loads(ADMISSION.read_text(encoding='utf-8'))
    approved_at = admission.get('approved_at_epoch')
    age = time.time() - approved_at if type(approved_at) in (int, float) else -1
    if (admission.get('version') != 1 or admission.get('approved') is not True
            or admission.get('helper_sha256') != sha(Path(__file__)) or not 0 <= age <= 3600
            or admission.get('gpu_native_phase_terminal') is not True):
        raise ValueError('Require fresh root-reviewed admission bound to this helper')
    frozen = plan(admission['extra_non_emily_acquisition_bytes'], admission['other_reserved_retained_bytes'],
                  admission['gpu_cache_growth_bytes'])
    if any(admission[key] != frozen['ledger'][key]
           for key in ('vulkan_receipt_sha256', 'first_attempt_terminal_sha256')):
        raise ValueError('Acquisition ledger changed after review')
    if sha(FIRST / (PIN['file'] + '.partial')) != json.loads((FIRST / 'terminal-failure.json').read_text())['partial_archive_sha256']:
        raise ValueError('Preserved first partial changed')
    rows, blobs = metadata()
    regular(OUT.parent, directory=True)
    OUT.mkdir(exist_ok=False)  # Also the one-attempt marker: never overwrite/retry.
    frozen.update(admission=admission, admission_sha256=sha(ADMISSION), metadata_sha256=PINS,
                  helper_sha256=sha(Path(__file__)), created_at_epoch=time.time())
    final_once(OUT / 'frozen.json', frozen)
    receipt = {'version': 2, 'attempt': 'emilyguitar-002', 'state': 'acquiring', 'started_at_epoch': time.time(),
               'transferred_bytes': 0, 'expanded_bytes': 0, 'redirects': [],
               'native_executed': False, 'audio_decoded_or_played': False,
               'publisher_sha256': None, 'archive_hash_basis': 'observed identity only, not publisher attestation'}
    journal = ProgressJournal(OUT / 'progress.jsonl')
    last_phase, last_network, last_expanded = None, -MIB, -MIB
    def record(force=False):
        nonlocal last_phase, last_network, last_expanded
        receipt['charged_cumulative_bytes'] = (frozen['ledger']['charged_cumulative_bytes']
            + admission['extra_non_emily_acquisition_bytes'] + receipt['transferred_bytes'])
        if (force or receipt['state'] != last_phase or receipt['transferred_bytes'] - last_network >= MIB
                or receipt['expanded_bytes'] - last_expanded >= MIB):
            journal.append({'state': receipt['state'], 'transferred_bytes': receipt['transferred_bytes'],
                            'expanded_bytes': receipt['expanded_bytes'],
                            'elapsed_seconds': round(time.time() - receipt['started_at_epoch'], 6)})
            last_phase, last_network, last_expanded = receipt['state'], receipt['transferred_bytes'], receipt['expanded_bytes']
    try:
        record(force=True)
        partial = OUT / (PIN['file'] + '.partial')
        digest = hashlib.sha256()
        opener = urllib.request.build_opener(Redirect(receipt))
        request = urllib.request.Request(PIN['url'], headers={'Accept-Encoding': 'identity',
                                          'User-Agent': 'private-emilyguitar-one-attempt-acquisition'})
        with opener.open(request, timeout=30) as response:
            tls_url(response.url)
            receipt.update(status=response.status, final_url=response.url,
                           content_length=response.headers.get('Content-Length'))
            record()
            response_headers(response)
            with partial.open('xb') as output:
                while receipt['transferred_bytes'] < PIN['bytes']:
                    block = response.read1(min(MIB, PIN['bytes'] - receipt['transferred_bytes']))
                    if not block:
                        raise ValueError('Truncated archive body')
                    receipt['transferred_bytes'] += len(block)
                    output.write(block)
                    digest.update(block)
                    record()
                    if shutil.disk_usage(APP).free < CAPS['free_disk_bytes']:
                        raise ValueError('Disk reserve reached')
                output.flush()
                os.fsync(output.fileno())
            # HTTP message framing and exact length cap end the body: never read
            # a speculative extra byte beyond the authorized transfer envelope.
        receipt['observed_archive_sha256'] = digest.hexdigest()
        archive = OUT / PIN['file']
        partial.rename(archive)
        receipt['state'] = 'validating_extracting'
        record()
        check_budget(resources(), PIN['expanded_bytes'] + METADATA_ALLOWANCE + SCRATCH_ALLOWANCE
                     + admission['other_reserved_retained_bytes'], receipt['charged_cumulative_bytes'])
        def checkpoint(count):
            receipt['expanded_bytes'] += count
            record()
            if shutil.disk_usage(APP).free < CAPS['free_disk_bytes']:
                raise ValueError('Disk reserve reached')
        stage = OUT / 'bank.partial'
        inventory = extract(archive, stage, rows, blobs, checkpoint)
        if sha(archive) != receipt['observed_archive_sha256']:
            raise ValueError('Retained archive changed during extraction')
        metadata()
        final_once(OUT / 'inventory.json', inventory)
        # One expansion only; sibling rename on the same volume, no copy.
        if stage.stat().st_dev != OUT.stat().st_dev:
            raise ValueError('Staging is not on the destination volume')
        stage.rename(OUT / 'bank')
        used_metadata = sum(p.stat().st_size for p in OUT.iterdir()
                            if p.is_file() and p.name != PIN['file'])
        if used_metadata > METADATA_ALLOWANCE:
            raise ValueError('Combined Emily metadata allowance exceeded')
        remaining = (SCRATCH_ALLOWANCE + admission['other_reserved_retained_bytes']
                     + METADATA_ALLOWANCE - used_metadata)
        after = resources()
        check_budget(after, remaining, receipt['charged_cumulative_bytes'])
        receipt.update(state='complete', resources_after=after,
                       remaining_reserved_bytes=remaining, inventory_sha256=sha(OUT / 'inventory.json'),
                       wav_git_exact_count=sum(row['path'].endswith('.wav') for row in inventory),
                       bank_path=str(OUT / 'bank'), retained_directory_bytes=tree_size(OUT))
    except BaseException as error:
        receipt.update(state='failed', error=repr(error))
        raise
    finally:
        receipt['elapsed_seconds'] = time.time() - receipt['started_at_epoch']
        try:
            record(force=True)
        except BaseException as error:
            receipt.update(state='failed', journal_error=repr(error))
        finally:
            journal.close()
        receipt.update(progress_journal_bytes=journal.bytes, progress_journal_records=journal.records)
        final_once(OUT / 'acquisition.json', receipt)
    if receipt['state'] != 'complete':
        raise RuntimeError('Attempt failed; immutable final receipt retained')
    print(json.dumps(receipt, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan', action='store_true')
    group.add_argument('--acquire', action='store_true')
    args = parser.parse_args()
    if args.plan:
        print(json.dumps(plan(), indent=2))
    else:
        acquire()


if __name__ == '__main__':
    main()
