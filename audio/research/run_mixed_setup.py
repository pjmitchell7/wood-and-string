"""Private research acquisition; normal TLS, bounded writes, no installation."""
import hashlib,json,shutil,urllib.request,zipfile
from pathlib import Path
P=Path(__file__).parent/'mixed'
P.mkdir(exist_ok=True)
if not (P/'acquisition-plan.json').exists():
    runtime='https://download-r2.pytorch.org/whl/cpu/'
    initial={
      'torch':{'url':runtime+'torch-2.6.0%2Bcpu-cp312-cp312-win_amd64.whl#sha256=4027d982eb2781c93825ab9527f17fbbb12dbabf422298e4b954be60016f87d8','bytes':206493444},
      'torchaudio':{'url':runtime+'torchaudio-2.6.0%2Bcpu-cp312-cp312-win_amd64.whl#sha256=75266c25d394bb5d70f83a38b1b4d858c074a767c18f7ff87443bdf193c1b236','bytes':2438883},
      'model':{'url':'https://download.pytorch.org/torchaudio/models/hdemucs_high_musdbhq_only.pt#sha256=85ce4420e0852344b816487b4c4bb821bd406376b93bf684b2b76aa56404c46a','bytes':334697255,'hash_provenance':'observed first acquisition, not upstream attestation'},
      'plan':{'free_before':shutil.disk_usage(P).free,'download_estimate':600000000,'expanded_estimate':1800000000,'minimum_free':8589934592}}
    (P/'acquisition-plan.json').write_text(json.dumps(initial,indent=2))
plan=json.loads((P/'acquisition-plan.json').read_text())
if shutil.disk_usage(P).free-plan['plan'].get('expanded_estimate',1800000000)<plan['plan']['minimum_free']:raise RuntimeError('Expanded disk reserve')
manifest={}
for name in ('torch','torchaudio','model'):
    item=plan[name];url=item['url'];expected=url.split('#sha256=')[-1] if '#sha256=' in url else None
    dest=P/('model.pt' if name=='model' else urllib.parse.unquote(url.split('/')[-1].split('#')[0]))
    if not dest.exists():
        if shutil.disk_usage(P).free-item['bytes']<plan['plan']['minimum_free']:raise RuntimeError('Reserve')
        temp=dest.with_suffix('.partial');count=0;digest=hashlib.sha256()
        with urllib.request.urlopen(url.split('#')[0],timeout=60) as response,temp.open('wb') as out:
            while block:=response.read(1024*1024):
                count+=len(block)
                if count>item['bytes']:raise RuntimeError('Size cap')
                digest.update(block);out.write(block)
        if count!=item['bytes'] or expected and digest.hexdigest()!=expected:raise RuntimeError('Identity mismatch')
        temp.replace(dest)
    digest=hashlib.file_digest(dest.open('rb'),'sha256').hexdigest()
    if expected and digest!=expected:raise RuntimeError('Cached mismatch')
    manifest[name]={'file':dest.name,'bytes':dest.stat().st_size,'sha256':digest,'upstream_hash':bool(expected) and name!='model','url':url}
    if dest.suffix=='.whl':
        with zipfile.ZipFile(dest) as z:
            manifest[name]['expanded_bytes']=sum(i.file_size for i in z.infolist())
            for n in z.namelist():
                if n.endswith('/METADATA') or n.endswith('/LICENSE'):
                    (P/(name+'-'+n.rsplit('/',1)[-1]+'.txt')).write_bytes(z.read(n))
    (P/'acquisition-manifest.json').write_text(json.dumps(manifest,indent=2))
    print(name,manifest[name],flush=True)
