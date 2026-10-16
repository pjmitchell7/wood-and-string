"""Fetch the pinned portable Caddy release; never install, start or trust it.

Public release metadata was checked on 2026-10-15. HTTPS validation stays on.
18.5 MB download, at most 100 MiB expansion, 250 MiB setup allowance.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '2.11.7'
BASE = f'https://github.com/caddyserver/caddy/releases/download/v{VERSION}/'
ARCHIVE = f'caddy_{VERSION}_windows_amd64.zip'
SIZE = 18479498
SHA256 = '0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06'
SHA512 = 'c308154504e53755958ffc62d8a2657ab17eefd6dd25c45dbb2c3d93d29519971f77ca652f7d0f2bcb7f980b9ba3822478865e29969d0272d7613550a43152b1'
CHECKSUM_SIZE = 6769
CHECKSUM_SHA256 = '5d27b76b95f496638d79208c55250317b8755fd9e90005f7466aa32956944a90'
SBOM_SIZE = 146669
SBOM_SHA256 = 'e38b1c0e0e41e22a6d838a6cbfc1bcb73152cda035491efcb847d09b4011e6a3'


def download(name, path, size, expected):
    h = hashlib.sha256()
    count = 0
    with urllib.request.urlopen(BASE + name, timeout=60) as response, path.open('xb') as writer:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > size:
                raise ValueError('Release file exceeds its pinned length')
            h.update(chunk)
            writer.write(chunk)
    if count != size or h.hexdigest() != expected:
        raise ValueError('Release file does not match its pinned length and checksum')


def setup():
    vendor = ROOT / 'vendor'
    vendor.mkdir(exist_ok=True)
    target = vendor / f'caddy-{VERSION}'
    if target.exists():
        record = json.loads((target / 'verified-release.json').read_text())
        actual = hashlib.sha256((target / 'caddy.exe').read_bytes()).hexdigest()
        if record['archive_sha256'] != SHA256 or record['executable_sha256'] != actual:
            raise ValueError('Existing portable copy differs; preserved for inspection')
        print('Existing portable Caddy verified; no system changes.')
        return
    if shutil.disk_usage(ROOT).free < 8 * 1024**3 + 250 * 1024**2:
        raise RuntimeError('Need the 8 GiB reserve plus 250 MiB setup allowance')
    with tempfile.TemporaryDirectory(prefix='setup-', dir=ROOT) as temp:
        temp = Path(temp)
        archive = temp / ARCHIVE
        checksums = temp / 'checksums.txt'
        sbom = temp / 'windows-amd64.sbom'
        download(f'caddy_{VERSION}_checksums.txt', checksums, CHECKSUM_SIZE, CHECKSUM_SHA256)
        published = dict((parts[1].lstrip('*'), parts[0]) for line in checksums.read_text().splitlines() if len(parts := line.split()) == 2)
        if published.get(ARCHIVE) != SHA512:
            raise ValueError('Published checksums do not contain the pinned archive')
        download(ARCHIVE, archive, SIZE, SHA256)
        with archive.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha512').hexdigest() != SHA512:
                raise ValueError('Archive differs from the published SHA-512 checksum')
        download(f'caddy_{VERSION}_windows_amd64.sbom', sbom, SBOM_SIZE, SBOM_SHA256)
        staging = temp / 'staging'
        staging.mkdir()
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            expanded = sum(item.file_size for item in entries)
            if expanded > 100 * 1024**2:
                raise ValueError('Expanded release exceeds setup allowance')
            for item in entries:
                name = item.filename.replace('\\', '/')
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or ':' in name or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('Unsupported archive entry')
                if not (staging / name).resolve().is_relative_to(staging.resolve()):
                    raise ValueError('Archive entry leaves the setup directory')
            z.extractall(staging)
        exe = staging / 'caddy.exe'
        if not exe.is_file() or not (staging / 'LICENSE').is_file():
            raise ValueError('Expected executable and license are missing')
        shutil.copy2(checksums, staging / 'release-checksums.txt')
        shutil.copy2(sbom, staging / 'windows-amd64.sbom')
        record = {'version': VERSION, 'source': BASE + ARCHIVE,
                  'archive_bytes': SIZE, 'archive_sha256': SHA256,
                  'archive_sha512': SHA512,
                  'expanded_bytes': expanded,
                  'executable_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
                  'checksums_sha256': CHECKSUM_SHA256, 'sbom_sha256': SBOM_SHA256,
                  'scope': 'portable private evaluation; no installer, listener, trust or firewall changes'}
        (staging / 'verified-release.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        os.rename(staging, target)
        print(json.dumps(record, indent=2))


if __name__ == '__main__':
    setup()
