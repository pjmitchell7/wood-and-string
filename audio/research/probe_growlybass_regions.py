"""Frozen 56-region pitch evidence; no source transposition or production mode."""
import hashlib,json,os,re,struct,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'audio/research/growlybass/probe-002'
SR=44100

def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def imports(path):
    data=Path(path).read_bytes();pe=struct.unpack_from('<I',data,60)[0]
    if data[pe:pe+4]!=b'PE\0\0':raise ValueError('Not a PE binary')
    count=struct.unpack_from('<H',data,pe+6)[0];opt=pe+24;optsize=struct.unpack_from('<H',data,pe+20)[0]
    directory=opt+(112 if struct.unpack_from('<H',data,opt)[0]==523 else 96)
    rva=struct.unpack_from('<I',data,directory+8)[0]
    sections=[]
    for i in range(count):
        at=opt+optsize+i*40;virtualsize,virtual,rawsize,raw=struct.unpack_from('<IIII',data,at+8)
        sections.append((virtual,max(virtualsize,rawsize),raw))
    def offset(value):
        for virtual,size,raw in sections:
            if virtual<=value<virtual+size:return raw+value-virtual
        raise ValueError('Unmapped PE import RVA')
    if not rva:return []
    at=offset(rva);names=[]
    while any(data[at:at+20]):
        name=offset(struct.unpack_from('<I',data,at+12)[0]);names.append(data[name:data.index(b'\0',name)].decode());at+=20
    return names

def binary_inventory(python):
    env=Path(python).parents[1];cfg=(env/'pyvenv.cfg').read_text()
    base=Path(re.search(r'^home = (.+)$',cfg,re.M).group(1).strip())
    sfizz=ROOT/'audio/vendor/sfizz/bin/Release/sfizz.dll';system=Path(os.environ['SystemRoot'])/'System32'
    search=[sfizz.parent,base,base/'DLLs',system,env/'Scripts']
    for name in ['numpy.libs','scipy.libs']:search.append(env/'Lib/site-packages'/name)
    seeds=[Path(python),base/'python.exe',sfizz,*base.glob('*.dll'),*(base/'DLLs').glob('*.pyd')]
    for name in ['numpy','scipy','numpy.libs','scipy.libs']:
        folder=env/'Lib/site-packages'/name
        seeds.extend(p for p in folder.rglob('*') if p.suffix.lower() in ('.dll','.pyd'))
    todo=list(seeds);seen={};contracts=set();edges={}
    while todo:
        path=todo.pop().resolve();key=str(path).lower()
        if key in seen:continue
        seen[key]={'path':str(path),'sha256':sha(path),'bytes':path.stat().st_size};edges[str(path)]=imports(path)
        for name in edges[str(path)]:
            if name.lower().startswith(('api-ms-','ext-ms-')):contracts.add(name);continue
            found=next((folder/name for folder in [path.parent,*search] if (folder/name).is_file()),None)
            if found is None:raise ValueError('Unresolved native dependency: '+name)
            todo.append(found)
        if len(seen)>400:raise ValueError('Unexpected native dependency count')
    return {'binaries':list(seen.values()),'imports':edges,'windows_api_set_contracts':sorted(contracts),
            'base_python':str(base),'os_version':str(os.sys.getwindowsversion())}

def run(request,emit):
    frozen=json.loads((OUT/'frozen.json').read_text());assert sha(OUT/'frozen.json')==request['frozen_sha256']
    for entry in frozen['native']['binaries']:assert sha(entry['path'])==entry['sha256']
    for path,digest in frozen['code'].items():assert sha(ROOT/path)==digest
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import correlate,find_peaks
    import ctypes as C
    from audio.render import Sfizz,_configure_offline
    from audio.growlybass import clean_sustain_map
    bank=ROOT/'audio/research/growlybass/bank/Growlybass';mapping,inventory=clean_sustain_map(bank)
    assert inventory==frozen['samples'];assert hashlib.sha256(mapping.encode()).hexdigest()==frozen['map_sha256']
    def estimate(signal):
        x=np.asarray(signal,dtype=float)[round(.15*SR):round(.85*SR)];x-=x.mean()
        ac=correlate(x,x,method='fft',mode='full')[len(x)-1:];ac/=ac[0]
        low,high=int(SR/1500),int(SR/20);peaks,_=find_peaks(ac[low:high]);peaks+=low
        lag=int(max(peaks,key=lambda p:ac[p]));ac_hz=SR/lag
        # Independent frequency-domain estimate; lowest sufficiently strong peak.
        nfft=131072;mag=np.abs(np.fft.rfft(x*np.hanning(len(x)),n=nfft));freq=np.fft.rfftfreq(nfft,1/SR)
        valid=(freq>=20)&(freq<=1500);peaks,_=find_peaks(mag)
        peaks=[p for p in peaks if valid[p] and mag[p]>=.1*mag[valid].max()]
        if not peaks:raise ValueError('No spectral estimate')
        p=peaks[0];left,mid,right=np.log(np.maximum(mag[p-1:p+2],1e-30));delta=.5*(left-right)/(left-2*mid+right)
        spectral_hz=(p+delta)*SR/nfft
        return {'autocorrelation_hz':ac_hz,'spectral_hz':float(spectral_hz),'autocorrelation_strength':float(ac[lag])}
    rows=frozen['rows'];source=[]
    for row in rows:
        sr,x=wavfile.read(bank/row['sample']);assert sr==SR;source.append(estimate(x))
    api=Sfizz().lib;block=256;total=round(len(rows)*1.5*SR);reports=[];first=None
    for repeat in (1,2):
        audio=np.zeros((total,2),dtype=np.float32);rendered=[];quality=[]
        for index,row in enumerate(rows):
            synth=api.sfizz_create_synth();assert synth
            try:
                api.sfizz_set_sample_rate(synth,SR);api.sfizz_set_samples_per_block(synth,block)
                quality.append(_configure_offline(api,synth));assert api.sfizz_load_string(synth,str(bank/'region-probe.sfz').encode(),mapping.encode())
                buffers=[(C.c_float*block)(),(C.c_float*block)()];channels=(C.POINTER(C.c_float)*2)(*buffers)
                base=round(index*1.5*SR);length=round(1.5*SR)
                for start in range(0,length,block):
                    size=min(block,length-start)
                    if start==0:api.sfizz_send_note_on(synth,0,row['command'],row['velocity'])
                    if start<=SR<start+size:api.sfizz_send_note_off(synth,SR-start,row['command'],0)
                    api.sfizz_render_block(synth,channels,2,size)
                    for ch in (0,1):audio[base+start:base+start+size,ch]=np.ctypeslib.as_array(buffers[ch])[:size]
                rendered.append(estimate(audio[base:base+SR,0]))
            finally:api.sfizz_free(synth)
        assert np.isfinite(audio).all()
        path=OUT/f'regions-{repeat}.wav';wavfile.write(path,SR,audio)
        reports.append({'path':path.name,'sha256':sha(path),'peak':float(np.abs(audio).max()),'qualities':quality})
        if first is None:first=rendered
        else:assert rendered==first
        emit({'phase':'matrix-render','progress':repeat/2})
    assert reports[0]['sha256']==reports[1]['sha256']
    results=[]
    def cents(hz,expected):return float(1200*np.log2(hz/(440*2**((expected-69)/12))))
    for row,src,dst in zip(rows,source,first):
        errors={label:{method:cents(values[method],row['command']-12) for method in ('autocorrelation_hz','spectral_hz')} for label,values in [('source',src),('render',dst)]}
        disagreements={label:abs(float(1200*np.log2(values['autocorrelation_hz']/values['spectral_hz']))) for label,values in [('source',src),('render',dst)]}
        differences={method:abs(float(1200*np.log2(src[method]/dst[method]))) for method in ('autocorrelation_hz','spectral_hz')}
        flags=[]
        if any(abs(v)>50 for values in errors.values() for v in values.values()):flags.append('outside-50-cents')
        if any(v>25 for v in disagreements.values()):flags.append('estimators-disagree')
        if any(v>25 for v in differences.values()):flags.append('source-render-disagree')
        results.append({**row,'expected_sounding_midi':row['command']-12,'source':src,'render':dst,'errors_cents':errors,'estimator_disagreement_cents':disagreements,'source_render_difference_cents':differences,'flags':flags})
    result={'rows':results,'passing_rows':sum(not r['flags'] for r in results),'flagged_rows':sum(bool(r['flags']) for r in results),'renders':reports,'played_audio':False,'policy':frozen['policy']}
    (OUT/'result.json').write_text(json.dumps(result,indent=2));return {'passing_rows':result['passing_rows'],'flagged_rows':result['flagged_rows'],'report_sha256':sha(OUT/'result.json')}

def main():
    from audio.research.setup_growlybass import tree_size
    from audio.analysis_runtime import runtime_status,MANIFEST_PATH
    from audio.growlybass import clean_sustain_map,POLICY
    from audio.managed_process import run_worker
    import shutil
    used=tree_size(ROOT/'audio/research')+tree_size(ROOT/'audio/vendor/analysis');reserve=128*1024**2
    assert used+reserve<4*1024**3 and shutil.disk_usage(ROOT).free>8*1024**3+reserve,(used,reserve)
    OUT.mkdir(exist_ok=False);runtime=runtime_status();assert runtime['ready']
    mapping,samples=clean_sustain_map(ROOT/'audio/research/growlybass/bank/Growlybass');rows=[]
    for group in mapping.split('<group>')[1:]:
        head,*regions=group.split('<region>');center=re.search(r'pitch_keycenter=(\d+)',head);center=int(center.group(1)) if center else 60
        for region in regions:
            high=re.search(r'hivel=(\d+)',region);velocity=int(high.group(1)) if high else 127
            rows.append({'command':center,'velocity':velocity,'sample':re.search(r'sample=([^\r\n]+)',region).group(1)})
    assert len(rows)==56 and len({r['command'] for r in rows})==14
    frozen={'policy':POLICY,'rows':rows,'samples':samples,'map_sha256':hashlib.sha256(mapping.encode()).hexdigest(),
            'criteria':{'cents_from_command_minus_12':50,'estimator_disagreement_cents':25,'source_render_disagreement_cents':25,'window_seconds':[.15,.85],'spectral_threshold':.1,'fmin':20,'fmax':1500,'retuning':False},
            'native':binary_inventory(runtime['python']),'runtime_manifest_sha256':sha(MANIFEST_PATH),
            'code':{p:sha(ROOT/p) for p in ['audio/research/probe_growlybass_regions.py','audio/growlybass.py','audio/render.py','audio/managed_process.py']},
            'budget_before':used,'additional_cap_bytes':64*1024**2,'root_reserved_bytes':64*1024**2}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2))
    try:
        result=run_worker('audio.research.probe_growlybass_regions',{'frozen_sha256':sha(OUT/'frozen.json')},python=runtime['python'],wall_seconds=120,memory_bytes=1024**3,progress=lambda e:print(json.dumps(e),flush=True))
        assert tree_size(OUT)<64*1024**2
        (OUT/'supervisor.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
    except BaseException as e:(OUT/'failure.json').write_text(json.dumps({'error':repr(e)}));raise

if __name__=='__main__':main()
