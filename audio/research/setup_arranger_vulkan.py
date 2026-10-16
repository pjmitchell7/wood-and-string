"""Bounded two-phase GPU runtime acquisition; never execute native/model code.

--inspect reads one 64 KiB explicit ZIP range and small license members only.
Root must review that concrete result before invoking --acquire. Both phases
are one-shot: failed bytes/receipts remain, with no resume or automatic retry.
The existing GGUF and CPU runtime are referenced in place, never copied/edited.
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
import zlib

from .acquire_local_arranger import sha, save, static_inventory
from .plan_local_arranger import APP, GiB, MODEL, RUNTIME_REV, tree_size


OUT = APP / 'audio/research/local-arranger-vulkan-001'
CACHE = APP / 'work/local-arranger-plan-001'
PIN = {'repository': 'ggml-org/llama.cpp', 'tag': 'b11669', 'commit': RUNTIME_REV,
       'asset_id': 644420898, 'file': 'llama-b11669-bin-win-vulkan-x64.zip',
       'bytes': 32182350,
       'sha256': '2c07574e7bde71321cb785282b20cc821e951132149eeceeea32ddebdbf0598e',
       'url': 'https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-win-vulkan-x64.zip'}
METADATA_CHARGE = 2 * 1024**2
SCRATCH_ALLOWANCE = 64 * 1024**2
OUTPUT_ALLOWANCE = 8 * 1024**2
RANGE_BYTES = 65536
CAPS = {'research_bytes': 7 * GiB, 'cumulative_acquisition_bytes': 5 * GiB,
        'free_reserve_bytes': 8 * GiB}
ALLOWED_HOSTS = {'github.com', 'release-assets.githubusercontent.com'}


def tls_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Expected an official public TLS release URL')


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, row): self.row = row
    def redirect_request(self, request, response, code, message, headers, target):
        tls_url(target)
        self.row['redirects'].append({'from': request.full_url, 'status': code, 'to': target})
        return super().redirect_request(request, response, code, message, headers, target)


def resources():
    return {'research_bytes': tree_size(APP / 'audio/research') + tree_size(APP / 'audio/vendor/analysis'),
            'free_disk_bytes': shutil.disk_usage(APP).free}


def previous_charge():
    path = APP / 'audio/research/local-arranger-001/acquisition.json'
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if (receipt.get('state') != 'complete' or receipt.get('charged_cumulative_bytes') != 3429305188
            or receipt.get('transferred_bytes') != 2516818142):
        raise ValueError('Reconcile the existing acquisition ledger before this package')
    return {'charged_cumulative_bytes': receipt['charged_cumulative_bytes'],
            'receipt': str(path.relative_to(APP)), 'sha256': sha(path)}


def check_budget(current, retained_increment, acquisition_increment, charged_before):
    if current['research_bytes'] + retained_increment > CAPS['research_bytes']:
        raise ValueError('Approved retained research cap would be exceeded')
    if current['free_disk_bytes'] - retained_increment < CAPS['free_reserve_bytes']:
        raise ValueError('Approved free-disk reserve would be exceeded')
    if charged_before + acquisition_increment > CAPS['cumulative_acquisition_bytes']:
        raise ValueError('Approved cumulative acquisition cap would be exceeded')


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or len(path.parts) != 1 or '/' in name or '\\' in name
            or ':' in name or '\0' in name or name in ('.', '..') or name.endswith((' ', '.'))
            or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                            *(f'COM{i}' for i in range(1, 10)),
                                            *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Unsafe or non-flat ZIP member path')
    return name


def central_inventory(tail):
    """Parse non-ZIP64 single-disk metadata completely within the bounded tail."""
    at = tail.rfind(b'PK\x05\x06')
    if at < 0 or at + 22 > len(tail): raise ValueError('Missing complete ZIP end record')
    signature, disk, start_disk, disk_count, count, size, offset, comment = struct.unpack_from('<4s4H2IH', tail, at)
    if (disk or start_disk or disk_count != count or count in (0, 65535)
            or at + 22 + comment != len(tail) or max(size, offset) == 0xffffffff):
        raise ValueError('Unsupported ZIP central metadata')
    base = PIN['bytes'] - len(tail)
    if offset < base or offset + size != base + at:
        raise ValueError('Central directory is not wholly present in bounded range')
    position = offset - base; end = position + size; rows = []; seen = set()
    while position < end:
        values = struct.unpack_from('<4s6H3I5H2I', tail, position)
        (signature, made, needed, flags, method, modified_time, modified_date, crc,
         compressed, expanded, name_length, extra_length, comment_length,
         start_disk, internal, external, local_offset) = values
        if signature != b'PK\x01\x02': raise ValueError('Invalid central record signature')
        record_end = position + 46 + name_length + extra_length + comment_length
        if record_end > end: raise ValueError('Incomplete central record')
        raw_name = tail[position + 46: position + 46 + name_length]
        name = safe_name(raw_name.decode('utf-8' if flags & 0x800 else 'cp437'))
        mode = external >> 16
        if (name.casefold() in seen or flags & 1 or method not in (0, 8) or start_disk
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or max(compressed, expanded, local_offset) == 0xffffffff
                or local_offset + 30 + name_length + compressed > offset):
            raise ValueError('Unsafe or unsupported central member')
        seen.add(name.casefold())
        rows.append({'path': name, 'compressed_bytes': compressed, 'expanded_bytes': expanded,
                     'crc32': f'{crc:08x}', 'method': method, 'flags': flags,
                     'external_attr': external, 'local_offset': local_offset})
        position = record_end
    if position != end or len(rows) != count: raise ValueError('Central member count mismatch')
    if count > 128 or sum(row['expanded_bytes'] for row in rows) > 512 * 1024**2:
        raise ValueError('Unexpected runtime expansion bound')
    return rows


def read_range(start, end, name, receipt):
    if not (0 <= start <= end < PIN['bytes']) or end - start + 1 > RANGE_BYTES:
        raise ValueError('Metadata range exceeds its bound')
    row = {'name': name, 'range': [start, end], 'url': PIN['url'],
           'redirects': [], 'transferred_bytes': 0, 'state': 'started'}
    receipt['requests'].append(row); save(OUT / 'metadata-receipt.json', receipt)
    opener = urllib.request.build_opener(Redirect(row))
    request = urllib.request.Request(PIN['url'], headers={'Range': f'bytes={start}-{end}',
                                    'Accept-Encoding': 'identity', 'User-Agent': 'private-arranger-vulkan-metadata'})
    destination = OUT / (name + '.partial')
    data = bytearray()
    try:
        with opener.open(request, timeout=30) as response, destination.open('xb') as output:
            tls_url(response.url)
            row.update(status=response.status, final_url=response.url,
                       content_range=response.headers.get('Content-Range'),
                       content_length=response.headers.get('Content-Length'))
            if (response.status != 206 or row['content_range'] != f'bytes {start}-{end}/{PIN["bytes"]}'
                    or row['content_length'] is not None and int(row['content_length']) != end - start + 1):
                raise ValueError('Range response differs from the explicit pinned interval')
            while block := response.read1(min(16384, end - start + 2 - len(data))):
                row['transferred_bytes'] += len(block); receipt['transferred_bytes'] += len(block)
                output.write(block); data.extend(block)
                save(OUT / 'metadata-receipt.json', receipt)
                if len(data) > end - start + 1 or receipt['transferred_bytes'] > METADATA_CHARGE:
                    raise ValueError('Metadata body limit exceeded')
            output.flush(); os.fsync(output.fileno())
        if len(data) != end - start + 1: raise ValueError('Incomplete metadata interval')
        destination.replace(OUT / name)
        row.update(state='complete', sha256=hashlib.sha256(data).hexdigest())
        return bytes(data)
    except BaseException as error:
        row.update(state='failed', error=repr(error)); raise
    finally:
        save(OUT / 'metadata-receipt.json', receipt)


def read_notice(member, receipt):
    if member['expanded_bytes'] > 128 * 1024 or member['compressed_bytes'] > 32768:
        raise ValueError('Notice exceeds small-member bounds')
    offset = member['local_offset']
    header = read_range(offset, offset + 29, member['path'] + '.header', receipt)
    values = struct.unpack('<4s5H3I2H', header)
    signature, version, flags, method, mtime, mdate, crc, compressed, expanded, namesize, extrasize = values
    if signature != b'PK\x03\x04' or method != member['method'] or flags != member['flags']:
        raise ValueError('Notice local-header identity differs')
    # Data-descriptor entries may leave size/CRC zero in the local header.
    if not flags & 8 and (compressed != member['compressed_bytes'] or expanded != member['expanded_bytes'] or f'{crc:08x}' != member['crc32']):
        raise ValueError('Notice local-header sizes differ')
    length = namesize + extrasize + member['compressed_bytes']
    body = read_range(offset + 30, offset + 29 + length, member['path'] + '.body', receipt)
    name = body[:namesize].decode('utf-8' if flags & 0x800 else 'cp437')
    if name != member['path']: raise ValueError('Notice local-header path differs')
    compressed_body = body[namesize + extrasize:]
    if method == 8:
        decoder = zlib.decompressobj(-15)
        content = decoder.decompress(compressed_body, member['expanded_bytes'] + 1)
        if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError('Incomplete or surplus notice deflate stream')
    else:
        content = compressed_body
    if len(content) != member['expanded_bytes'] or f'{binascii.crc32(content) & 0xffffffff:08x}' != member['crc32']:
        raise ValueError('Notice size/CRC mismatch')
    content.decode('utf-8')
    (OUT / 'notices' / member['path']).write_bytes(content)
    return {'path': member['path'], 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


def inspect():
    before = resources(); ledger = previous_charge()
    check_budget(before, METADATA_CHARGE, METADATA_CHARGE, ledger['charged_cumulative_bytes'])
    OUT.mkdir(exist_ok=False); (OUT / 'notices').mkdir()
    receipt = {'version': 1, 'state': 'inspecting', 'started_at': time.time(),
               'transferred_bytes': 0, 'metadata_charge_bytes': METADATA_CHARGE,
               'requests': [], 'native_executed': False, 'model_loaded': False}
    save(OUT / 'metadata-receipt.json', receipt)
    try:
        cached = json.loads((CACHE / 'llama-releases.json').read_text(encoding='utf-8'))
        release = next(item for item in cached if item['tag_name'] == PIN['tag'])
        asset = next(item for item in release['assets'] if item['id'] == PIN['asset_id'])
        if any(asset[key] != PIN[pin] for key, pin in [('name', 'file'), ('size', 'bytes'), ('browser_download_url', 'url')]) or asset['digest'] != 'sha256:' + PIN['sha256']:
            raise ValueError('Cached publisher identity differs from pin')
        save(OUT / 'publisher-asset.json', asset)
        tail = read_range(PIN['bytes'] - RANGE_BYTES, PIN['bytes'] - 1, 'zip-tail.bin', receipt)
        inventory = central_inventory(tail)
        save(OUT / 'zip-central-inventory.json', inventory)
        members = {item['path']: item for item in inventory}
        if 'ggml-vulkan.dll' not in members or 'llama-completion.exe' not in members:
            raise ValueError('Expected portable completion/Vulkan runtime absent')
        notices = []
        for name in ('LICENSE', 'LICENSE-LLVM-OpenMP'):
            if name in members: notices.append(read_notice(members[name], receipt))
        if 'LICENSE-LLVM-OpenMP' not in {row['path'] for row in notices}:
            raise ValueError('Expected bundled OpenMP notice unavailable')
        source_license = CACHE / 'llama-LICENSE'
        shutil.copyfile(source_license, OUT / 'notices/llama-source-MIT-LICENSE')
        notices.append({'path': 'llama-source-MIT-LICENSE', 'bytes': source_license.stat().st_size, 'sha256': sha(source_license)})
        expanded = sum(item['expanded_bytes'] for item in inventory)
        components = {'retained_zip': PIN['bytes'], 'expanded_runtime': expanded,
                      'metadata_notices_allowance': METADATA_CHARGE,
                      'scratch_allowance': SCRATCH_ALLOWANCE, 'output_allowance': OUTPUT_ALLOWANCE}
        incremental = sum(components.values())
        check_budget(before, incremental, METADATA_CHARGE + PIN['bytes'], ledger['charged_cumulative_bytes'])
        plan = {'version': 1, 'state': 'inspected_awaiting_root_review_before_full_fetch',
                'runtime': dict(PIN, expanded_bytes=expanded, members=len(inventory)),
                'caps': CAPS, 'resources_before_inspection': before, 'ledger': ledger,
                'incremental_components': components, 'incremental_budget_bytes': incremental,
                'charged_cumulative_if_complete': ledger['charged_cumulative_bytes'] + METADATA_CHARGE + PIN['bytes'],
                'notices': notices, 'setup_sha256': sha(Path(__file__)),
                'cached_publisher_metadata_sha256': sha(CACHE / 'llama-releases.json'),
                'same_model_reused_without_copy': dict(MODEL, path=str(APP / 'audio/research/local-arranger-001' / MODEL['file'])),
                'policy': 'One attempt; no automatic retry; preserve failures; never execute/load acquired binaries/model; CPU runtime unchanged; no installs/driver/system changes.'}
        save(OUT / 'plan.json', plan)
        receipt.update(state='inspected', members=len(inventory), expanded_bytes=expanded,
                       incremental_budget_bytes=incremental, elapsed_seconds=time.time() - receipt['started_at'])
        print(json.dumps({'state': receipt['state'], 'metadata_read_bytes': receipt['transferred_bytes'],
                          'members': len(inventory), 'expanded_bytes': expanded,
                          'incremental_budget_bytes': incremental, 'components': components}), flush=True)
    except BaseException as error:
        receipt.update(state='failed', error=repr(error)); raise
    finally:
        save(OUT / 'metadata-receipt.json', receipt)


def acquire():
    plan = json.loads((OUT / 'plan.json').read_text(encoding='utf-8'))
    metadata = json.loads((OUT / 'metadata-receipt.json').read_text(encoding='utf-8'))
    if (plan['state'] != 'inspected_awaiting_root_review_before_full_fetch' or metadata['state'] != 'inspected'
            or plan['setup_sha256'] != sha(Path(__file__)) or (OUT / 'acquisition.json').exists()
            or plan['runtime']['sha256'] != PIN['sha256'] or plan['runtime']['bytes'] != PIN['bytes']):
        raise ValueError('Inspection identity/state differs or acquisition was already attempted')
    current = resources(); ledger = previous_charge()
    # Metadata already retained is counted in current usage; reserve the full
    # conservative allowance again rather than assuming every byte is consumed.
    remaining = sum(plan['incremental_components'][key] for key in ('retained_zip', 'expanded_runtime', 'metadata_notices_allowance', 'scratch_allowance', 'output_allowance'))
    check_budget(current, remaining, METADATA_CHARGE + PIN['bytes'], ledger['charged_cumulative_bytes'])
    receipt = {'version': 1, 'state': 'acquiring', 'started_at': time.time(),
               'transferred_bytes': 0, 'metadata_transferred_bytes': metadata['transferred_bytes'],
               'metadata_charge_bytes': METADATA_CHARGE, 'redirects': [], 'runtime': PIN,
               'native_executed': False, 'model_loaded': False,
               'plan_sha256': sha(OUT / 'plan.json'), 'setup_sha256': sha(Path(__file__)),
               'resources_before': current, 'ledger': ledger}
    save(OUT / 'acquisition.json', receipt)
    try:
        partial = OUT / (PIN['file'] + '.partial'); digest = hashlib.sha256()
        opener = urllib.request.build_opener(Redirect(receipt))
        request = urllib.request.Request(PIN['url'], headers={'Accept-Encoding': 'identity', 'User-Agent': 'private-arranger-vulkan-acquisition'})
        with opener.open(request, timeout=30) as response, partial.open('xb') as output:
            tls_url(response.url)
            receipt.update(status=response.status, final_url=response.url, content_length=response.headers.get('Content-Length'))
            if response.status != 200 or receipt['content_length'] is not None and int(receipt['content_length']) != PIN['bytes']:
                raise ValueError('Pinned complete archive response differs')
            while block := response.read1(min(1024**2, PIN['bytes'] + 1 - receipt['transferred_bytes'])):
                receipt['transferred_bytes'] += len(block); output.write(block); digest.update(block)
                save(OUT / 'acquisition.json', receipt)
                if receipt['transferred_bytes'] > PIN['bytes']: raise ValueError('Runtime archive exceeded pinned length')
                if shutil.disk_usage(APP).free < CAPS['free_reserve_bytes']: raise ValueError('Free-disk reserve reached')
            output.flush(); os.fsync(output.fileno())
        receipt['sha256'] = digest.hexdigest()
        if receipt['transferred_bytes'] != PIN['bytes'] or receipt['sha256'] != PIN['sha256']:
            raise ValueError('Complete archive bytes/hash mismatch')
        archive_path = OUT / PIN['file']; partial.replace(archive_path)
        expected = json.loads((OUT / 'zip-central-inventory.json').read_text(encoding='utf-8'))
        stage = OUT / 'runtime.partial'; stage.mkdir(exist_ok=False); inventory = []
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist(); seen = set()
            if len(entries) != plan['runtime']['members'] or sum(item.file_size for item in entries) != plan['runtime']['expanded_bytes']:
                raise ValueError('Complete archive member count/expansion differs from inspection')
            for member, central in zip(entries, expected):
                name = safe_name(member.filename)
                observed = {'path': name, 'compressed_bytes': member.compress_size, 'expanded_bytes': member.file_size,
                            'crc32': f'{member.CRC:08x}', 'method': member.compress_type, 'flags': member.flag_bits,
                            'external_attr': member.external_attr, 'local_offset': member.header_offset}
                if observed != central or name.casefold() in seen or member.is_dir():
                    raise ValueError('Complete central entry differs from reviewed metadata')
                seen.add(name.casefold()); target = stage / name
                digest = hashlib.sha256(); count = 0; crc = 0
                with archive.open(member) as source, target.open('xb') as output:
                    while block := source.read(min(1024**2, member.file_size + 1 - count)):
                        count += len(block)
                        if count > member.file_size: raise ValueError('Expanded member exceeds its pin')
                        digest.update(block); crc = binascii.crc32(block, crc); output.write(block)
                if count != member.file_size or crc & 0xffffffff != member.CRC:
                    raise ValueError('Expanded member bytes/CRC mismatch')
                inventory.append({'path': name, 'bytes': count, 'sha256': digest.hexdigest(), 'zip_crc32': f'{member.CRC:08x}'})
        save(OUT / 'runtime-inventory.json', inventory)
        for notice in plan['notices']:
            if notice['path'] in seen and sha(stage / notice['path']) != notice['sha256']:
                raise ValueError('Extracted notice differs from reviewed range')
        stage.replace(OUT / 'runtime')
        closure = static_inventory(OUT / 'runtime')
        save(OUT / 'static-dependencies.json', closure)
        if closure['unresolved']: raise ValueError('Unresolved normal PE imports: ' + ', '.join(closure['unresolved']))
        after = resources()
        if after['research_bytes'] > CAPS['research_bytes'] or after['free_disk_bytes'] < CAPS['free_reserve_bytes']:
            raise ValueError('Final resource limits exceeded')
        receipt.update(state='complete', elapsed_seconds=time.time() - receipt['started_at'],
                       retained_directory_bytes=tree_size(OUT), resources_after=after,
                       charged_cumulative_bytes=ledger['charged_cumulative_bytes'] + METADATA_CHARGE + receipt['transferred_bytes'],
                       runtime_inventory_sha256=sha(OUT / 'runtime-inventory.json'),
                       static_dependencies_sha256=sha(OUT / 'static-dependencies.json'))
        print(json.dumps({key: receipt[key] for key in ('state', 'transferred_bytes', 'retained_directory_bytes', 'charged_cumulative_bytes')}), flush=True)
    except BaseException as error:
        receipt.update(state='failed', error=repr(error), elapsed_seconds=time.time() - receipt['started_at'],
                       charged_cumulative_bytes=ledger['charged_cumulative_bytes'] + METADATA_CHARGE + receipt['transferred_bytes'])
        raise
    finally:
        save(OUT / 'acquisition.json', receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument('--inspect', action='store_true'); phase.add_argument('--acquire', action='store_true')
    arguments = parser.parse_args()
    inspect() if arguments.inspect else acquire()


if __name__ == '__main__': main()
