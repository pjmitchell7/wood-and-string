"""Silent, preregistered native controls for the experimental bass adapter."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'audio/research/growlybass/adapter-002'
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def note(identity,start,end,pitch=28,string=0,player=1):
    return {'id':identity,'start':10000+round(start*44100),'end':10000+round(end*44100),
            'pitch':pitch,'velocity':80,'player':player,'string':string,'fret':pitch-(25,33,38,43)[string],
            'articulation':'clean-sustain-first-variation'}

def run(request,emit):
    frozen=json.loads((OUT/'frozen.json').read_text());assert request['frozen_sha256']==sha(OUT/'frozen.json')
    for path,digest in frozen['code'].items():assert sha(ROOT/path)==digest
    for item in frozen['native']['binaries']:assert sha(item['path'])==item['sha256']
    import array,copy,math
    import numpy as np
    from unittest.mock import patch
    from contextlib import ExitStack
    from audio import bass_render as bass
    from audio import growlybass as bank
    assert frozen['profile']==bank.PROFILE_VERSION
    captured=[];commands=[];receipts=[];raws={}
    original_write=bass._write_pcm16;original_engine=bass.Sfizz
    def capture(spool,*args,**kwargs):
        data=array.array('f');data.frombytes(spool.read_bytes());captured.append(np.asarray(data,dtype=np.float32).reshape(-1,2).copy())
        return original_write(spool,*args,**kwargs)
    class Engine:
        def __init__(self):
            api=original_engine().lib
            class Proxy:
                def __getattr__(self,name):return getattr(api,name)
                def sfizz_send_note_on(self,synth,at,pitch,velocity):commands.append(('on',pitch,velocity));return api.sfizz_send_note_on(synth,at,pitch,velocity)
                def sfizz_send_note_off(self,synth,at,pitch,velocity):commands.append(('off',pitch,velocity));return api.sfizz_send_note_off(synth,at,pitch,velocity)
            self.lib=Proxy()
    for index,case in enumerate(frozen['cases']):
        arrangement={'sample_rate':44100,'render_origin_sample':10000,'performers':case['performers'],'notes':case['notes']}
        before=copy.deepcopy(arrangement);commands.clear();captured.clear()
        actual_map=bass.moderate_map
        def baseline(path):return actual_map(path).replace('<global> group=1 off_by=1 off_mode=fast\n','')
        with patch.object(bass,'Sfizz',Engine),patch.object(bass,'_write_pcm16',capture):
            with ExitStack() as stack:
                if case.get('no_choke'):
                    stack.enter_context(patch.object(bass,'_reset_voices',lambda *args:None))
                    stack.enter_context(patch.object(bass,'_damping_gain',lambda *args:1.))
                result=bass.render(arrangement,OUT/(case['name']+'.wav'))
        assert arrangement==before
        expected=sorted(n['pitch']+12 for n in case['notes'])
        assert sorted(p for kind,p,v in commands if kind=='on')==expected
        assert len(captured)==1;raws[case['name']]=captured[0]
        receipts.append({'name':case['name'],'sha256':sha(OUT/(case['name']+'.wav')),'metrics':result,'commands':list(commands),'source_unchanged':True})
        (OUT/'render-receipts.json').write_text(json.dumps(receipts,indent=2))
        emit({'phase':case['name'],'progress':(index+1)/(len(frozen['cases'])+1)})
    assert receipts[0]['sha256']==receipts[1]['sha256']
    def padded(name,length):
        result=np.zeros((length,2),np.float32);x=raws[name];result[:len(x)]=x;return result
    length=len(raws['duo']);independent_error=float(np.abs(raws['duo']-(padded('old',length)+padded('new-player',length))).max())
    assert independent_error<=1e-7
    controls=[]
    for kind in ('same','different'):
        actual=raws[kind];baseline=raws[kind+'-baseline'];new=padded(kind+'-new',len(actual));old=padded('old',len(actual))
        before=slice(0,round(.4*44100)-round(.005*44100));after=slice(round(.42*44100),round(.55*44100))
        pre_error=float(np.abs(actual[before]-old[before]).max());assert pre_error<=1e-7
        actual_tail=actual-new;baseline_tail=baseline-new
        rms=lambda x:float(np.sqrt(np.mean(x.astype(float)**2)))
        old_rms=rms(old[after]);assert old_rms>1e-6
        actual_rms=rms(actual_tail[after]);baseline_rms=rms(baseline_tail[after])
        assert actual_rms<=old_rms*.01,(kind,actual_rms,old_rms)
        controls.append({'kind':kind,'before_new_attack_max_residual':pre_error,'isolated_old_tail_rms':old_rms,
                         'actual_remaining_old_tail_rms':actual_rms,'no_choke_remaining_old_tail_rms':baseline_rms,
                         'remaining_fraction':actual_rms/old_rms})
    separate=raws['different-string'];new=padded('different-string-new',len(separate));old=padded('old',len(separate))
    separate_error=float(np.abs(separate-new-old).max());assert separate_error<=1e-7
    # Explicit gate remains 0.3s; show decay beyond note-off and a later true rest.
    x=raws['old'];tail=[]
    for start,end in ((.2,.3),(.3,.4),(.4,.5),(.5,.6),(.8,1.0)):
        chunk=x[round(start*44100):round(end*44100)]
        tail.append({'start':start,'end':end,'rms':float(np.sqrt(np.mean(chunk.astype(float)**2))),'peak':float(np.abs(chunk).max())})
    assert tail[1]['rms']>0 and tail[-1]['peak']<1e-7
    cancelled=[False]
    try:
        bass.render({'sample_rate':44100,'render_origin_sample':10000,'performers':1,'notes':[note('cancel',0,.3)]},OUT/'cancelled.wav',
                    cancel=lambda:cancelled[0],progress=lambda _:cancelled.__setitem__(0,True))
        raise AssertionError('Cancellation did not fire')
    except InterruptedError:pass
    assert not (OUT/'cancelled.wav').exists() and not list(OUT.glob('*.partial'))
    result={'renders':receipts,'choke_controls':controls,'independent_raw_voice_sum_max_residual':independent_error,
            'separate_string_tail_max_residual':separate_error,'release_windows':tail,'cancel_cleanup':True,'played_audio':False}
    (OUT/'result.json').write_text(json.dumps(result,indent=2));return {'report_sha256':sha(OUT/'result.json'),'checks':'passed'}

def main():
    from audio.analysis_runtime import runtime_status
    from audio.managed_process import run_worker
    from audio.research.probe_growlybass_regions import binary_inventory
    from audio.research.setup_growlybass import tree_size
    from audio.growlybass import PROFILE_VERSION,require_bank,moderate_map
    import shutil
    used=tree_size(ROOT/'audio/research')+tree_size(ROOT/'audio/vendor/analysis');extra=24*1024**2
    assert used+extra<4*1024**3 and shutil.disk_usage(ROOT).free>8*1024**3+extra,(used,extra)
    OUT.mkdir(exist_ok=False);runtime=runtime_status();assert runtime['ready'];config=require_bank();mapping=moderate_map(config['bank'])
    old=note('old',0,.3);same=note('new',.4,.7);different=note('new',.4,.7,35);string=note('new',.4,.7,35,1);player=note('new',.4,.7,35,1,2)
    cases=[{'name':'repeat-a','notes':[old,same]},{'name':'repeat-b','notes':[old,same]},
           {'name':'old','notes':[old]},{'name':'same','notes':[old,same]},{'name':'same-baseline','notes':[old,same],'no_choke':True},
           {'name':'same-new','notes':[same]},{'name':'different','notes':[old,different]},
           {'name':'different-baseline','notes':[old,different],'no_choke':True},{'name':'different-new','notes':[different]},
           {'name':'different-string','notes':[old,string]},{'name':'different-string-new','notes':[string]},
           {'name':'duo','notes':[old,player],'performers':2},{'name':'new-player','notes':[player],'performers':2}]
    # Renderer requires each declared performer to have notes: isolated player2
    # uses player1 under a separate synth; its waveform should remain identical.
    cases[-1]['notes']=[{**player,'player':1}];cases[-1]['performers']=1
    for case in cases:case.setdefault('performers',1)
    frozen={'cases':cases,'profile':PROFILE_VERSION,'map_sha256':hashlib.sha256(mapping.encode()).hexdigest(),
            'code':{p:sha(ROOT/p) for p in ['audio/research/probe_bass_adapter.py','audio/bass_render.py','audio/growlybass.py','audio/render.py','audio/managed_process.py']},
            'native':binary_inventory(runtime['python']),'bank_receipt_sha256':sha(ROOT/'audio/research/growlybass/acquisition.json'),
            'criteria':{'pcm_repeats':'identical SHA256','raw_voice_sum_max_error':1e-7,'before_five_ms_damping_max_error':1e-7,
                        'old_tail_after_reattack_max_fraction':.01,'separate_string_tail_max_error':1e-7,'late_rest_peak':1e-7,
                        'cancellation':'no output/partial','policy':'No gate extension; outgoing waveform alone fades over final<=5ms before nextpluck, then its instance resets exactly at onset; no-damping baseline disables both fade/reset; output normalization happens after raw comparison'},
            'budget_before':used,'extra_cap_bytes':extra}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2))
    try:
        response=run_worker('audio.research.probe_bass_adapter',{'frozen_sha256':sha(OUT/'frozen.json')},python=runtime['python'],wall_seconds=120,memory_bytes=1024**3,progress=lambda event:print(json.dumps(event),flush=True))
        assert tree_size(OUT)<extra
        (OUT/'supervisor.json').write_text(json.dumps(response,indent=2));print(json.dumps(response),flush=True)
    except BaseException as error:(OUT/'failure.json').write_text(json.dumps({'error':repr(error)}));raise

if __name__=='__main__':main()
