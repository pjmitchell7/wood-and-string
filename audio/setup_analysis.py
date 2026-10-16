"""Explicit offline portable setup. Run with Python3.12 from the app directory.

Never called automatically by the app. --reuse-env permits the existing idle
private research env; otherwise a new environment is created under audio/vendor.
Only local hash-locked wheels and verified public MIT model files are consumed.
"""
from pathlib import Path
import argparse,email,hashlib,json,os,re,shutil,subprocess,sys,time,venv,zipfile
from concurrent.futures import ThreadPoolExecutor
from .analysis_runtime import ROOT,LOCK_PATH,MANIFEST_PATH,MODELS,runtime_version,sha256,require_runtime
RESERVE=8*1024**3
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-env',type=Path);args=parser.parse_args()
    if sys.version_info[:2]!=(3,12) or sys.platform!='win32':raise RuntimeError('Requires existing Windows Python3.12; no system install is performed.')
    dest=ROOT/'vendor/analysis';dest.mkdir(parents=True,exist_ok=True)
    env=(args.reuse_env or dest/'env').resolve()
    if not env.is_relative_to(ROOT.resolve()):raise RuntimeError('Environment must stay under this app audio directory.')
    if args.reuse_env and not (env/'Scripts/python.exe').is_file():raise RuntimeError('Reuse environment missing.')
    rows=[]
    for line in LOCK_PATH.read_text().splitlines():
        if line and not line.startswith('#'):
            match=re.fullmatch(r'([^=]+)==([^ ]+) --hash=sha256:([a-f0-9]{64})',line)
            if not match:raise ValueError('Invalid lock row')
            rows.append(match.groups())
    folders=[ROOT/'research/pyin-wheels',ROOT/'research/mixed',ROOT/'research/umx',dest/'cache']
    wheels={};expanded=0;artifacts=[];notices=dest/'notices';notices.mkdir(exist_ok=True)
    for folder in folders:
        for wheel in folder.glob('*.whl'):
            with zipfile.ZipFile(wheel) as z:
                metadata=email.message_from_bytes(z.read(next(n for n in z.namelist() if n.endswith('/METADATA'))))
                name=metadata['Name'].lower().replace('_','-');version=metadata['Version']
                row=next((r for r in rows if r[:2]==(name,version)),None)
                if not row:continue
                if sha256(wheel)!=row[2]:raise RuntimeError('Wheel integrity mismatch: '+wheel.name)
                if name in wheels:continue
                wheels[name]=wheel;expanded+=sum(i.file_size for i in z.infolist())
                artifacts.append({'name':name,'version':version,'filename':wheel.name,'sha256':row[2],'bytes':wheel.stat().st_size})
                for n in z.namelist():
                    if not n.endswith('/') and (n.rsplit('/',1)[-1].lower().startswith(('license','licence','copying','notice','authors')) or ('.dist-info/' in n and n.endswith('/METADATA'))):
                        (notices/(wheel.stem+'-'+n.replace('/','_'))).write_bytes(z.read(n))
    missing=[r[0] for r in rows if r[0] not in wheels]
    if missing:raise RuntimeError('Offline wheel cache missing: '+', '.join(missing))
    source_models=ROOT/'research/umx';modeldir=dest/'models';modeldir.mkdir(exist_ok=True)
    record=source_models/'record.json'
    if not record.is_file() or json.loads(record.read_text())['metadata']['license']['id']!='mit-license':raise RuntimeError('Verified UMX-HQ MIT record must be retained in offline cache.')
    shutil.copyfile(record,notices/'UMX-HQ-official-record.json')
    (notices/'UMX-HQ-attribution.txt').write_text('UMX-HQ v1.0.1; Fabian-Robert Stoter and Antoine Liutkus. Official record3370489 declares MIT. Source: https://zenodo.org/records/3370489 ; retain the MIT permission notice supplied alongside Open-Unmix and the official record. Input music rights are separate.\n')
    for n,h in MODELS.items():
        source=(modeldir/n) if (modeldir/n).exists() else source_models/n
        if not source.is_file() or sha256(source)!=h:raise RuntimeError('Verified offline model missing: '+n)
    # Conservative full expansion allowance even when reusing most dependencies.
    if shutil.disk_usage(dest).free-expanded-200000000<RESERVE:raise RuntimeError('8GiB disk reserve would be crossed.')
    owned=[ROOT/'research',dest]
    used=sum(f.stat().st_size for d in owned for f in d.rglob('*') if f.is_file())
    increment=400000000 if args.reuse_env else expanded+200000000
    if used+increment>4*1024**3:raise RuntimeError('Prior4GiB experiment expanded cap would be crossed; report before setup.')
    if not args.reuse_env:
        if env.exists():raise RuntimeError('Environment exists; use explicit --reuse-env only while idle.')
        venv.create(env,with_pip=True)
    python=env/'Scripts/python.exe'
    command=[str(python),'-m','pip','install','--no-index','--no-deps','--no-compile','--require-hashes','-r',str(LOCK_PATH)]
    for f in folders:command+=['--find-links',str(f)]
    subprocess.run(command,check=True,timeout=600)
    subprocess.run([str(python),'-m','pip','check'],check=True,timeout=30)
    versions={r[0]:r[1] for r in rows}
    probe='import json,sys,importlib.metadata as m;print(json.dumps({"python":list(sys.version_info[:3]),"base":sys._base_executable,"versions":{n:m.version(n) for n in '+repr(list(versions))+'}}))'
    observed=json.loads(subprocess.check_output([str(python),'-I','-c',probe],text=True,timeout=30))
    if observed['python'][:2]!=[3,12] or observed['versions']!=versions:raise RuntimeError('Installed runtime version mismatch.')
    for n in MODELS:
        target=modeldir/n
        if not target.exists():
            try:os.link(source_models/n,target)
            except OSError:shutil.copyfile(source_models/n,target)
    # Runtime-bearing files plus installed metadata; compiler headers, test
    # fixtures and documentation are not loaded by this inference-only pipeline.
    paths=[f for f in env.rglob('*') if f.is_file() and '__pycache__' not in f.parts and not any(p in ('tests','test','include','docs','benchmarks') for p in f.parts) and (f.suffix in ('.py','.pyd','.dll','.exe','.pth') or any(p.endswith('.dist-info') for p in f.parts))]
    def inventory(f):return {'path':f.relative_to(env).as_posix(),'bytes':f.stat().st_size,'sha256':sha256(f)}
    with ThreadPoolExecutor(max_workers=8) as pool:files=list(pool.map(inventory,paths))
    manifest={'schema_version':1,'runtime_version':runtime_version(),'python':str(python),'environment':str(env),'modeldir':str(modeldir),'package_versions':{r[0]:r[1] for r in rows},'artifacts':artifacts,'model_hashes':MODELS,'installed_files':files,'decoder':'PyAV19.0.1 local-file canonical passage; direct PCM16 WAV44.1kHz path retained','decoder_distribution_status':'Private execution only; bundled transitive redistribution audit remains open in docs/DECODER-001.md','created_unix':time.time(),'free_after_bytes':shutil.disk_usage(dest).free,'notices':str(notices)}
    manifest['python_version']=observed['python'];manifest['base_python']={'path':observed['base'],'sha256':sha256(observed['base'])}
    pending=MANIFEST_PATH.with_suffix('.partial');pending.write_text(json.dumps(manifest,indent=2),encoding='utf-8');pending.replace(MANIFEST_PATH)
    # Installation lock remains held; full verification runs immediately after it
    # is released by the CLI wrapper below, before reporting success.
    print(json.dumps({'installed':True,'runtime_version':manifest['runtime_version'],'python':str(python),'modeldir':str(modeldir),'free_bytes':manifest['free_after_bytes']}))
if __name__=='__main__':
    lock=MANIFEST_PATH.parent/'setup.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('x') as f:f.write(str(os.getpid()))
    try:main()
    finally:lock.unlink()
    try:
        checked=require_runtime();print(json.dumps({'ready':checked['ready'],'runtime_version':checked['runtime_version']}))
    except Exception:
        if MANIFEST_PATH.exists():MANIFEST_PATH.replace(MANIFEST_PATH.with_suffix('.failed'))
        raise
