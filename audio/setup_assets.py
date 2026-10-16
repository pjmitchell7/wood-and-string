"""Portable pinned audio assets only. Run python -m audio.setup_assets.

No installer, service, firewall or certificate-trust modifications. HTTPS uses
Python's normal certificate validation. Only missing vendor directories publish.
"""
import hashlib
import os
from pathlib import Path,PurePosixPath
import shutil
import stat
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent
ASSETS=[
 ('sfizz','https://github.com/sfztools/sfizz/releases/download/1.2.3/sfizz-1.2.3-win64.zip',8286178,'7431cbe9b7f2e71a9ff4b12fbbe82c766dbf75b1eedecca627e7506cb06a9fcc'),
 ('Meta-open-sound','https://github.com/facebookincubator/Meta-open-sound/releases/download/meta-steel-guitar-v1.0.0/meta-open-sound-meta-steel-guitar-ogg.zip',25440465,'a06baf193497ba36f20ccb67018e9f106155ad5e4f81d8a672530498038f68ed'),
]

def safe_extract(archive,destination):
    destination=Path(destination).resolve()
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist()
        if sum(x.file_size for x in entries)>100*1024**2:raise ValueError('Expanded archive exceeds limit')
        for item in entries:
            name=item.filename.replace('\\','/')
            parts=PurePosixPath(name)
            if parts.is_absolute() or '..' in parts.parts or ':' in name or stat.S_ISLNK(item.external_attr>>16):
                raise ValueError('Unsafe archive member')
            target=(destination/name).resolve()
            if not target.is_relative_to(destination):raise ValueError('Archive escapes destination')
        z.extractall(destination)

def setup():
    vendor=ROOT/'vendor';vendor.mkdir(exist_ok=True)
    if shutil.disk_usage(ROOT).free < 8*1024**3+150*1024**2:raise RuntimeError('Need 8 GiB free reserve plus 150 MiB setup space')
    for name,url,size,digest in ASSETS:
        target=vendor/name
        if target.exists():
            print(f'{name}: existing directory retained (not overwritten)');continue
        with tempfile.TemporaryDirectory(prefix='audio-setup-',dir=ROOT) as tmp:
            tmp=Path(tmp);archive=tmp/'download.zip';h=hashlib.sha256();count=0
            with urllib.request.urlopen(url,timeout=60) as response,archive.open('xb') as out:
                while chunk:=response.read(1024*1024):
                    count+=len(chunk)
                    if count>size:raise RuntimeError('Download exceeds pinned length')
                    h.update(chunk);out.write(chunk)
            if count!=size or h.hexdigest()!=digest:raise RuntimeError('Downloaded asset fails pinned length/hash check')
            staging=tmp/'staging';safe_extract(archive,staging)
            os.rename(staging,target)
            print(f'{name}: verified and installed locally')
    print('Audio assets ready. Existing directories were preserved; runtime tests verify usability.')

if __name__=='__main__':setup()
