"""Explicit, bounded acquisition for an isolated Basic Pitch experiment.

Does not alter the installed application runtime. Run with mixed/env's Python.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
P = ROOT / 'audio/research/basic-pitch'
REVISION = 'fa5997af0a8210982619003269994a1be25eddf3'
BASE = f'https://raw.githubusercontent.com/spotify/basic-pitch/{REVISION}/'
ARTIFACTS = [
    ('nmp.onnx', BASE+'basic_pitch/saved_models/icassp_2022/nmp.onnx', 230444,
     '2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec'),
    ('onnxruntime-1.31.0-cp312-cp312-win_amd64.whl',
     'https://files.pythonhosted.org/packages/be/ed/b12cea136ccd7b03d924f46b8393faf7ceac21115c0c50e729faa248cf23/onnxruntime-1.31.0-cp312-cp312-win_amd64.whl',
     14888841, '09d56445c1753e66e0912de69d3f0184016ad9a191dcd6925bf5dd570d2bfbe5'),
    ('flatbuffers-25.12.19-py2.py3-none-any.whl',
     'https://files.pythonhosted.org/packages/e8/2d/d2a548598be01649e2d46231d151a6c56d10b964d94043a335ae56ea2d92/flatbuffers-25.12.19-py2.py3-none-any.whl',
     26661, '7634f50c427838bb021c2d66a3d1168e9d199b0607e6329399f04846d42e20b4'),
    ('protobuf-7.36.2-cp310-abi3-win_amd64.whl',
     'https://files.pythonhosted.org/packages/8a/55/b77bda4e5e5f5971fb51b07663694690e9afdb9402136c16a522bd621cad/protobuf-7.36.2-cp310-abi3-win_amd64.whl',
     456514, 'a300819d441e078a5608c0d3c709796bb548136058fda017ae51d425b44fd353'),
]

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def footprint():
    return sum(f.stat().st_size for tree in (ROOT/'audio/research', ROOT/'audio/vendor/analysis')
               for f in tree.rglob('*') if f.is_file())

def check_budget(extra):
    if footprint()+extra > 4*1024**3 or shutil.disk_usage(ROOT).free-extra < 8*1024**3:
        raise RuntimeError('Research footprint/reserve would be exceeded')
    if 724539637 + sum(a[2] for a in ARTIFACTS) + 65536 > 2*1024**3:
        raise RuntimeError('Cumulative binary/model acquisition allowance exceeded')

def acquire():
    check_budget(200*1024**2)
    P.mkdir(exist_ok=True)
    (P/'notices').mkdir(exist_ok=True)
    inventory = []
    for name, url, size, expected in ARTIFACTS:
        target=P/name
        if not target.exists():
            temporary=target.with_suffix(target.suffix+'.partial')
            count=0
            try:
                with urllib.request.urlopen(url, timeout=45) as response, temporary.open('xb') as out:
                    while block:=response.read(1024**2):
                        count += len(block)
                        if count>size:
                            raise ValueError('Download exceeds manifest size')
                        out.write(block)
                if count!=size or digest(temporary)!=expected:
                    raise ValueError('Download identity mismatch')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        if target.stat().st_size!=size or digest(target)!=expected:
            raise ValueError('Cached artifact identity mismatch')
        item=dict(name=name,url=url,bytes=size,sha256=expected)
        if name.endswith('.whl'):
            with zipfile.ZipFile(target) as archive:
                item['expanded_bytes']=sum(i.file_size for i in archive.infolist())
                names=[n for n in archive.namelist() if any(k in Path(n).name.lower()
                       for k in ('license','licence','copying','notice')) or n.endswith('/METADATA')]
                item['notice_files']=names
                for number, member in enumerate(names):
                    (P/'notices'/f'{name}-{number}-{Path(member).name}').write_bytes(archive.read(member))
        inventory.append(item)
    for name in ('LICENSE','NOTICE'):
        target=P/'notices'/('spotify-'+name)
        if not target.exists():
            with urllib.request.urlopen(BASE+name, timeout=30) as response:
                body=response.read(32769)
            if len(body)>32768:
                raise ValueError('Unexpected notice size')
            target.write_bytes(body)
    result={'spotify_revision':REVISION,'artifacts':inventory,
            'incremental_binary_model_bytes':sum(a[2] for a in ARTIFACTS),
            'prior_binary_model_bytes':724539637,
            'logical_footprint_after_acquisition':footprint(),
            'free_bytes':shutil.disk_usage(ROOT).free,
            'base_runtime':'Existing mixed/env: numpy2.2.6 scipy1.15.3 packaging26.3',
            'model_hash_provenance':'Raw official pinned Spotify bytes matched previously published HF mirror SHA256',
            'wheel_hash_provenance':'Exact pinned PyPI release metadata'}
    (P/'acquisition.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))

def install():
    if (P/'runtime').exists():
        raise RuntimeError('Private target already exists; inspect it instead of overwriting')
    for name, _, size, expected in ARTIFACTS:
        if (P/name).stat().st_size!=size or digest(P/name)!=expected:
            raise ValueError('Artifact verification failed before offline install')
    check_budget(120*1024**2)
    lock=P/'probe-requirements.lock'
    lock.write_text('\n'.join(f'{name.split("-")[0]}=={name.split("-")[1]} --hash=sha256:{expected}'
                   for name,_,_,expected in ARTIFACTS if name.endswith('.whl'))+'\n')
    subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-index',
                    '--no-deps','--no-compile','--require-hashes','--find-links',str(P),
                    '--target',str(P/'runtime'),'-r',str(lock)],check=True,timeout=120)
    inventory=[dict(path=str(f.relative_to(P/'runtime')),bytes=f.stat().st_size,sha256=digest(f))
               for f in (P/'runtime').rglob('*') if f.is_file()]
    (P/'runtime-inventory.json').write_text(json.dumps(inventory,indent=2))
    print(json.dumps({'runtime_files':len(inventory),'footprint':footprint(),
                      'free_bytes':shutil.disk_usage(ROOT).free}))

if __name__=='__main__':
    if sys.argv[1:] == ['acquire']:
        acquire()
    elif sys.argv[1:] == ['install']:
        install()
    else:
        raise SystemExit('Choose acquire or install explicitly')
