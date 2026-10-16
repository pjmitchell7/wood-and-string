"""Explicit one-shot acquisition only; never execute or load acquired artifacts."""
import hashlib,json,os,shutil,stat,time,urllib.request,urllib.error,zipfile
from pathlib import Path,PurePosixPath
from .plan_local_arranger import MODEL,RUNTIME,APP,tree_size
from .probe_growlybass_regions import imports

OUT=APP/'audio/research/local-arranger-001'
GiB=1024**3
METADATA_CHARGE=2*1024**2

def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,data):
    p=path.with_name(path.name+'.partial');p.write_text(json.dumps(data,indent=2),encoding='utf-8');p.replace(path)

def reconcile():
    basic=APP/'audio/research/basic-pitch/acquisition.json';bass=APP/'audio/research/growlybass/acquisition.json'
    b=json.loads(basic.read_text());g=json.loads(bass.read_text())
    # Prior ledger is a cumulative baseline, not added again as component files.
    assert b['prior_binary_model_bytes']==724539637 and b['incremental_binary_model_bytes']==15602460
    assert g['archive_bytes']==167454256
    guitar=2793541
    doc=APP/'docs/GUITARSET-001.md';assert '2,793,541' in doc.read_text(encoding='utf-8')
    total=b['prior_binary_model_bytes']+b['incremental_binary_model_bytes']+g['archive_bytes']+guitar
    assert total==910389894
    return {'components':{'prior_mixed_umx_decoder_cumulative':724539637,'basic_pitch_increment':15602460,
                         'growlybass_archive':167454256,'guitarset_bodies_and_known_metadata':guitar,
                         'arranger_metadata_conservative_charge':METADATA_CHARGE},
            'charged_before':total+METADATA_CHARGE,
            'evidence':{str(p.relative_to(APP)):sha(p) for p in (basic,bass,doc,APP/'docs/ANALYSIS-RUNTIME.md')},
            'scope':'HTTP artifact/body ledger; protocol/TLS overhead and older unmetered web metadata are not asserted exact. Expanded files, hashes and same-artifact references are not extra downloads.'}

class TLSRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self,record):self.record=record
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not newurl.startswith('https://'):raise ValueError('Non-TLS redirect denied')
        self.record.append({'from':req.full_url,'status':code,'to':newurl})
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def static_inventory(folder):
    system=Path(os.environ['SystemRoot'])/'System32';queue=list(folder.glob('*.exe'))+list(folder.glob('*.dll'))
    seen={};edges={};contracts=set();unresolved=set()
    while queue:
        p=queue.pop().resolve();key=str(p).lower()
        if key in seen:continue
        if len(seen)>400:raise ValueError('Unexpected static dependency count')
        seen[key]={'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p)}
        names=imports(p);edges[str(p)]=names
        for name in names:
            if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('Unexpected PE dependency name')
            if name.lower().startswith(('api-ms-','ext-ms-')):contracts.add(name);continue
            found=next((base/name for base in (folder,system) if (base/name).is_file()),None)
            if found:queue.append(found)
            else:unresolved.add(name)
    return {'binaries':list(seen.values()),'imports':edges,'windows_api_set_contracts':sorted(contracts),'unresolved':sorted(unresolved),
            'limitations':'Static normal import tables only; delay-load/dynamic resolution and CPU compatibility untested. No binary execution or DLL loading.'}

def main():
    ledger=reconcile();used=tree_size(APP/'audio/research')+tree_size(APP/'audio/vendor/analysis');free=shutil.disk_usage(APP).free
    increment=MODEL['bytes']+RUNTIME['bytes']+RUNTIME['expanded_bytes']+74*1024**2
    assert used+increment<=7*GiB and free-increment>=8*GiB
    assert ledger['charged_before']+MODEL['bytes']+RUNTIME['bytes']<=5*GiB
    OUT.mkdir(exist_ok=False)
    frozen={'approved_caps':{'research_bytes':7*GiB,'cumulative_acquisition_bytes':5*GiB,'free_reserve_bytes':8*GiB},
            'existing_bytes':used,'free_bytes':free,'incremental_budget':increment,'ledger':ledger,'model':MODEL,'runtime':RUNTIME,
            'code_sha256':sha(Path(__file__)),'plan_sha256':sha(APP/'audio/research/plan_local_arranger.py'),
            'policy':'One attempt per artifact. No resume/retry, no execution. Preserve partial and transferred-byte receipt on any failure.'}
    save(OUT/'frozen.json',frozen)
    receipt={'state':'acquiring','artifacts':[],'transferred_bytes':0,'started_at':time.time(),'native_executed':False}
    def record():save(OUT/'acquisition.json',receipt)
    record()
    try:
        for item in (MODEL,RUNTIME):
            row={'file':item['file'],'url':item['url'],'expected_bytes':item['bytes'],'expected_sha256':item['sha256'],'transferred_bytes':0,'redirects':[],'state':'started'}
            receipt['artifacts'].append(row);record()
            partial=OUT/(item['file']+'.partial');h=hashlib.sha256();opener=urllib.request.build_opener(TLSRedirect(row['redirects']))
            request=urllib.request.Request(item['url'],headers={'User-Agent':'private-local-arranger-acquisition','Accept-Encoding':'identity'})
            with opener.open(request,timeout=60) as response,partial.open('xb') as out:
                row.update(status=response.status,final_url=response.url,content_length=response.headers.get('Content-Length'))
                if response.status!=200 or not response.url.startswith('https://'):raise ValueError('Unexpected download response')
                if row['content_length'] is not None and int(row['content_length'])!=item['bytes']:raise ValueError('Publisher length changed')
                record();last_report=0
                while data:=response.read1(1024**2):
                    row['transferred_bytes']+=len(data);receipt['transferred_bytes']+=len(data)
                    if row['transferred_bytes']>item['bytes']:record();raise ValueError('Artifact exceeded pinned length')
                    out.write(data);h.update(data);record()
                    if shutil.disk_usage(APP).free<8*GiB:raise ValueError('Disk reserve reached')
                    if row['transferred_bytes']-last_report>=64*1024**2:
                        print(json.dumps({'file':item['file'],'transferred_bytes':row['transferred_bytes'],'expected_bytes':item['bytes']}),flush=True);last_report=row['transferred_bytes']
                out.flush();os.fsync(out.fileno())
            row['sha256']=h.hexdigest()
            if row['transferred_bytes']!=item['bytes'] or row['sha256']!=item['sha256']:raise ValueError('Artifact length/hash mismatch')
            partial.replace(OUT/item['file']);row['state']='verified';record()
        stage=OUT/'runtime.partial';stage.mkdir()
        inventory=[]
        with zipfile.ZipFile(OUT/RUNTIME['file']) as archive:
            entries=archive.infolist();seen=set()
            if len(entries)!=51 or sum(i.file_size for i in entries)!=49893149:raise ValueError('Runtime archive inventory changed')
            for i in entries:
                name=i.filename;p=PurePosixPath(name);mode=i.external_attr>>16
                if (p.is_absolute() or not p.parts or '/' in name or any(part in ('','..','.') for part in name.split('/'))
                    or '\\' in name or ':' in name or name.casefold() in seen or i.is_dir()
                    or stat.S_IFMT(mode) not in (0,stat.S_IFREG) or i.flag_bits&1):raise ValueError('Unsafe or unexpected ZIP member')
                seen.add(name.casefold())
                target=stage.joinpath(*p.parts)
                if not target.resolve().is_relative_to(stage.resolve()):raise ValueError('ZIP path escaped staging')
                target.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256();count=0
                with archive.open(i) as src,target.open('xb') as dst:
                    while data:=src.read(1024**2):count+=len(data);h.update(data);dst.write(data)
                if count!=i.file_size:raise ValueError('Expanded member size mismatch')
                inventory.append({'path':name,'bytes':count,'sha256':h.hexdigest(),'zip_crc32':f'{i.CRC:08x}'})
        save(OUT/'runtime-inventory.json',inventory)
        stage.replace(OUT/'runtime')
        notices=OUT/'notices';notices.mkdir()
        metadata=APP/'work/local-arranger-plan-001'
        for name in ('qwen-LICENSE','qwen-README.md','llama-LICENSE','LICENSE-LLVM-OpenMP','metadata-files.json','llama-zip-inventory.json'):
            shutil.copyfile(metadata/name,notices/name)
        save(OUT/'static-dependencies.json',static_inventory(OUT/'runtime'))
        after=tree_size(APP/'audio/research')+tree_size(APP/'audio/vendor/analysis');free_after=shutil.disk_usage(APP).free
        assert after<=7*GiB and free_after>=8*GiB
        receipt.update(state='complete',elapsed_seconds=time.time()-receipt['started_at'],retained_directory_bytes=tree_size(OUT),
                       combined_research_bytes=after,free_bytes=free_after,charged_cumulative_bytes=ledger['charged_before']+receipt['transferred_bytes'],
                       notices={p.name:{'bytes':p.stat().st_size,'sha256':sha(p)} for p in notices.iterdir()})
        record();print(json.dumps({k:receipt[k] for k in ('state','transferred_bytes','charged_cumulative_bytes','elapsed_seconds')}),flush=True)
    except BaseException as error:
        receipt.update(state='failed',error=repr(error),elapsed_seconds=time.time()-receipt['started_at'],charged_cumulative_bytes=ledger['charged_before']+receipt['transferred_bytes'])
        record();raise

if __name__=='__main__':main()
