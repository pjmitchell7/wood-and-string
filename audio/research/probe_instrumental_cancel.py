"""One silent real API cancellation attempt; no automatic retry."""
import ctypes as C
from ctypes import wintypes as W
import hashlib,json,shutil,subprocess,time
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from server import create_app

APP=Path(__file__).resolve().parents[2]
RESEARCH=APP/'audio/research';OUT=RESEARCH/'instrumental-cancel-001'
SOURCE=RESEARCH/'guitarset/recovery-001/raw-mixture.wav'
CAP=8*1024**2

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def size(p):return sum(x.stat().st_size for x in p.rglob('*') if x.is_file())

class Entry(C.Structure):
    _fields_=[('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',C.c_size_t),('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),('priority',W.LONG),('flags',W.DWORD),('name',W.WCHAR*260)]
class Memory(C.Structure):
    _fields_=[('cb',W.DWORD),('faults',W.DWORD)]+[(n,C.c_size_t) for n in ('peak','working','qpp','qp','qnpp','qnp','page','peakpage')]
k=C.WinDLL('kernel32',use_last_error=True);ps=C.WinDLL('psapi',use_last_error=True)
k.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];k.CreateToolhelp32Snapshot.restype=W.HANDLE
k.Process32FirstW.argtypes=[W.HANDLE,C.POINTER(Entry)];k.Process32NextW.argtypes=[W.HANDLE,C.POINTER(Entry)]
k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
k.CloseHandle.argtypes=[W.HANDLE];k.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
ps.GetProcessMemoryInfo.argtypes=[W.HANDLE,C.POINTER(Memory),W.DWORD]

def processes():
    h=k.CreateToolhelp32Snapshot(2,0)
    if h==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        e=Entry();e.size=C.sizeof(e);ok=k.Process32FirstW(h,C.byref(e));rows=[]
        while ok:rows.append((e.pid,e.parent,e.name));ok=k.Process32NextW(h,C.byref(e))
        return rows
    finally:k.CloseHandle(h)

def main():
    used=size(RESEARCH)+size(APP/'audio/vendor/analysis');free=shutil.disk_usage(RESEARCH).free
    assert used+CAP<4*1024**3 and free>8*1024**3+CAP
    OUT.mkdir(exist_ok=False)
    names=['server.py','worker.py','library.py','analysis_evidence.py','instrumental_preview.py','audio/instrumental_analysis.py','audio/instrumental_runtime.py','audio/mixed_analysis.py','audio/analysis_runtime.py','audio/managed_process.py','audio/basic_pitch.py','audio/basic_pitch_upstream.py']
    inputs=[Path(__file__),SOURCE,APP/'audio/vendor/analysis/runtime.json',*[APP/n for n in names]]
    frozen={'trigger':'First running job with 0 < progress < 0.5 and captured actual server Popen.poll() is None; expected 0.09 means separation pre-inference phase emitted. No assertion of exact matmul interruption.',
            'source_seconds':[0,6],'deadline_seconds':90,'additional_bytes':CAP,'research_before':used,'free_before':free,'inputs':{str(p.relative_to(APP)):sha(p) for p in inputs},'audio_played':False}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2))
    for p in inputs:
        if p.suffix=='.py':
            dst=OUT/'code'/p.relative_to(APP);dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(p.read_bytes())
    evidence={'state':'started','frozen_sha256':sha(OUT/'frozen.json'),'samples':[]};handles={};spawned=[];real=subprocess.Popen;began=time.monotonic()
    def save():(OUT/'evidence.json').write_text(json.dumps(evidence,indent=2))
    def spawn(*a,**kw):
        p=real(*a,**kw);spawned.append(p);return p
    def sample():
        known={p.pid for p in spawned}|set(handles);rows=processes()
        for _ in range(5):
            for pid,parent,name in rows:
                if pid in known or parent in known:
                    known.add(pid)
                    if pid not in handles:
                        h=k.OpenProcess(0x100000|0x400|0x10,False,pid)
                        if h:handles[pid]={'handle':h,'parent':parent,'name':name,'peak_bytes':0}
        live=[]
        for pid,record in handles.items():
            if k.WaitForSingleObject(record['handle'],0)==258:
                m=Memory();m.cb=C.sizeof(m)
                if not ps.GetProcessMemoryInfo(record['handle'],C.byref(m),m.cb):raise C.WinError(C.get_last_error())
                record['peak_bytes']=max(record['peak_bytes'],m.working);live.append(pid)
        return live
    save();app=create_app(OUT/'library');library=app.state.library;identity=None
    try:
        with patch('server.subprocess.Popen',side_effect=spawn),TestClient(app,base_url='http://127.0.0.1:8765') as client:
            client.get('/session')
            r=client.post('/api/import',content=SOURCE.read_bytes(),headers={'X-Filename':'Private cancellation mixture.wav'});assert r.status_code==200,r.text
            asset=r.json();evidence['source']=asset
            r=client.post('/api/instrumental-candidates',json={'asset_id':asset['id'],'start_seconds':0.,'end_seconds':6.});assert r.status_code==200,r.text
            identity=r.json()['id'];last=None
            while time.monotonic()-began<80:
                job=client.get('/api/jobs/'+identity).json();live=sample()
                if (job['state'],job['progress'])!=last:
                    last=(job['state'],job['progress']);evidence['samples'].append({'seconds':time.monotonic()-began,'state':job['state'],'progress':job['progress'],'live_pids':live});save()
                assert job['state'] in ('queued','running'),'Finished before trigger; no retry'
                assert size(OUT)<CAP
                if job['state']=='running' and 0<job['progress']<.5:
                    assert len(spawned)==1 and spawned[0].poll() is None and len(live)>=2
                    evidence['at_cancel']={'job':job,'server_pid':spawned[0].pid,'popen_poll':spawned[0].poll(),'live_pids':live,'seconds':time.monotonic()-began};save();break
                time.sleep(.05)
            else:raise TimeoutError('Trigger not observed; no retry')
            cancelled_at=time.monotonic();r=client.post('/api/jobs/'+identity+'/cancel');assert r.status_code==200,r.text
            while time.monotonic()-began<88:
                job=client.get('/api/jobs/'+identity).json();live=sample()
                if job['state']=='cancelled' and not live:break
                time.sleep(.05)
            assert job['state']=='cancelled' and not live,(job,live)
            for p in spawned:p.wait(timeout=1)
            evidence.update(job=job,cancellation_seconds=time.monotonic()-cancelled_at)
            assert library.verify(asset['id']) and sha(SOURCE)==frozen['inputs'][str(SOURCE.relative_to(APP))]
            assert len(library.items())==1 and list(library.media.iterdir())==[library.media/asset['sha256']]
            assert not list(library.stage.iterdir())
            assert not list((library.root/'analysis-evidence').glob('*'))
        assert all(sha(APP/n)==digest for n,digest in frozen['inputs'].items())
        evidence.update(state='passed',staging_empty=True,no_analysis_published=True,original_unchanged=True,elapsed_seconds=time.monotonic()-began)
    except BaseException as error:
        evidence.update(state='failed',error=repr(error));raise
    finally:
        evidence['owned_processes']={str(pid):{**{k:v for k,v in r.items() if k!='handle'},'terminal':k.WaitForSingleObject(r['handle'],0)==0} for pid,r in handles.items()}
        evidence['retained_bytes']=size(OUT);save()
        for r in handles.values():k.CloseHandle(r['handle'])
    print(json.dumps({'state':evidence['state'],'seconds':evidence['elapsed_seconds'],'cancel_seconds':evidence['cancellation_seconds']}),flush=True)

if __name__=='__main__':main()
