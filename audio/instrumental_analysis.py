"""Two managed, offline stages for inspectable unknown-instrument candidates.

run(request, emit) takes fixed stage, source identity/path, start/end seconds,
and an existing app-owned work_dir. No caller-selected module/model/overlay.
The parent MUST reconcile the whole owned work_dir after forced process death;
Python finally cannot guarantee cleanup when the managed process is killed.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

VERSION='instrumental-candidates-v1'
RATE=44100
MAX_JSON=8*1024**2
MAX_ARRAYS=16*1024**2
MAX_EVENTS=4000
PART={'id':'umx:other','instrument':'unknown','label':'Estimated instrumental mixture'}

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def validate_request(request):
    start,end=request.get('start_seconds'),request.get('end_seconds')
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or not 0<=start<end or end-start>10 or round((end-start)*RATE)<4096:raise ValueError('Choose a finite passage of 0.1 to 10 seconds')
    if not isinstance(request.get('source_id'),str) or not 1<=len(request['source_id'])<=256:raise ValueError('Invalid source identity')
    if not isinstance(request.get('source_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',request['source_sha256']):raise ValueError('Invalid source digest')
    work=Path(request['work_dir']);source=Path(request['source_path'])
    if not work.is_absolute() or not work.is_dir() or not source.is_absolute() or not source.is_file():raise ValueError('Expected existing application-owned paths')
    if source.resolve().is_relative_to(work.resolve()):raise ValueError('Original cannot be inside analysis staging')
    if request.get('stage') not in ('separate','recognize'):raise ValueError('Unknown instrumental stage')
    return work,round(start*RATE),round(end*RATE)

def write_json(path,value):
    payload=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
    if len(payload)>MAX_JSON:raise ValueError('Candidate report exceeds 8 MiB; choose a shorter passage')
    partial=path.with_name(path.name+'.partial')
    with partial.open('xb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
    partial.replace(path)

def validate_arrays(predictions):
    import numpy as np
    arrays={key:np.asarray(predictions[key],dtype=np.float32) for key in ('note','onset','contour')}
    frames=len(arrays['note'])
    if not 3<=frames<=1000:raise ValueError('Unexpected Basic Pitch frame count')
    for key,width in [('note',88),('onset',88),('contour',264)]:
        x=arrays[key]
        if x.shape!=(frames,width) or not np.isfinite(x).all() or (x<0).any() or (x>1).any():raise ValueError('Invalid raw activation matrix: '+key)
    if sum(x.nbytes for x in arrays.values())>MAX_ARRAYS:raise ValueError('Raw activations exceed 16 MiB')
    return arrays

def validate_events(events,origin,end):
    if not isinstance(events,list) or len(events)>MAX_EVENTS:raise ValueError('Candidate event budget exceeded')
    ids=set()
    for event in events:
        if not isinstance(event.get('id'),str) or event['id'] in ids:raise ValueError('Invalid candidate identity')
        ids.add(event['id'])
        if any(type(event.get(k)) is not int for k in ('start','end','pitch')) or not origin<=event['start']<event['end']<=end or not 0<=event['pitch']<=127:raise ValueError('Candidate outside source coordinates')
        for key in ('mean_note_activation','onset_activation','raw_onset_activation'):
            if key in event and (type(event[key]) not in (int,float) or not math.isfinite(event[key]) or not 0<=event[key]<=1):raise ValueError('Invalid uncalibrated activation')

def _separate(request,runtime,work,origin,end,emit):
    from .mixed_analysis import read_input_passage,TARGETS,WEIGHTS
    import numpy as np
    import torch,openunmix
    raw,timebase=read_input_passage(request['source_path'],request['source_sha256'],request['start_seconds'],request['end_seconds'],work/'decoded.wav')
    if timebase['origin_sample']!=origin or timebase['end_sample']!=end:raise ValueError('Canonical source bounds changed')
    channels=timebase['source_channels'];x=np.frombuffer(raw,dtype='<i2').reshape(-1,channels).astype(np.float32)/32768
    if channels==1:x=np.repeat(x,2,axis=1)
    if x.shape!=(end-origin,2):raise ValueError('Unexpected canonical passage shape')
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    model=openunmix.umxhq(targets=list(TARGETS),pretrained=False,niter=1,wiener_win_len=300,device='cpu')
    for target in TARGETS:
        filename,digest=WEIGHTS[target];path=Path(runtime['modeldir'])/filename
        if sha(path)!=digest:raise ValueError('UMX weight digest changed')
        state=torch.load(path,map_location='cpu',weights_only=True)
        extras=set(state)-set(model.target_models[target].state_dict())
        if extras!={'sample_rate','stft.window','transform.0.window'} or float(state['sample_rate'])!=RATE:raise ValueError('Unexpected UMX checkpoint structure')
        for key in extras:del state[key]
        model.target_models[target].load_state_dict(state,strict=True)
    model.eval();emit({'phase':'separating instrumental mixture','progress':.2});began=time.monotonic()
    with torch.inference_mode():estimates=model(torch.from_numpy(x.T.copy())[None])[0].numpy()
    if estimates.shape!=(4,2,end-origin) or not np.isfinite(estimates).all():raise ValueError('Invalid separation output')
    other=estimates[list(TARGETS).index('other')].T.astype(np.float32,copy=True)
    if other.nbytes>MAX_ARRAYS:raise ValueError('Separated array budget exceeded')
    path=work/'other.npy'
    with (work/'other.npy.partial').open('xb') as stream:np.save(stream,other,allow_pickle=False)
    (work/'other.npy.partial').replace(path)
    report={'version':1,'source_id':request['source_id'],'source_sha256':request['source_sha256'],'sample_rate':RATE,'origin':origin,'end':end,
            'source_part':PART,'timebase':timebase,'other':{'filename':'other.npy','sha256':sha(path),'bytes':path.stat().st_size,'shape':list(other.shape),'dtype':'float32'},
            'provenance':{'separator':'UMX-HQ','niter':1,'wiener_win_len':300,'device':'cpu','weights':WEIGHTS,'runtime_version':runtime['runtime_version'],
                          'normalization':False,'clipping':False,'recognition_downmix':'arithmetic stereo mean, no normalization'},'separation_seconds':time.monotonic()-began}
    write_json(work/'separation.json',report);emit({'phase':'separated','progress':1})
    return {'separation_sha256':sha(work/'separation.json'),'separation_bytes':(work/'separation.json').stat().st_size,'other_sha256':report['other']['sha256'],'other_bytes':report['other']['bytes']}

def _recognize(request,runtime,work,origin,end,emit):
    import numpy as np
    metadata=work/'separation.json'
    if metadata.stat().st_size>MAX_JSON:raise ValueError('Separation receipt too large')
    report=json.loads(metadata.read_text())
    for key,expected in [('source_id',request['source_id']),('source_sha256',request['source_sha256']),('sample_rate',RATE),('origin',origin),('end',end),('source_part',PART)]:
        if report[key]!=expected:raise ValueError('Separation identity or geometry mismatch')
    path=work/'other.npy'
    if path.stat().st_size>MAX_ARRAYS or path.stat().st_size!=report['other']['bytes'] or sha(path)!=report['other']['sha256']:raise ValueError('Separated array integrity failed')
    stereo=np.load(path,allow_pickle=False,mmap_mode='r')
    if stereo.shape!=(end-origin,2) or stereo.dtype!=np.dtype('float32') or not np.isfinite(stereo).all():raise ValueError('Invalid separated array geometry')
    overlay=runtime['basic_pitch_overlay'];sys.path.insert(0,overlay)
    try:
        from .basic_pitch import analyze_pcm,frame_samples,STRIDE,FRAMES,OVERLAP
        from .basic_pitch_upstream import decode_saved
        raw=analyze_pcm(stereo.mean(axis=1),RATE,origin,runtime['basic_pitch_model'],progress=lambda p:emit({'phase':'recognizing instrumental candidates','progress':p*.8}))
        arrays=validate_arrays(raw['raw_predictions'])
        expected=int(((end-origin+1)//2)/STRIDE*(FRAMES-OVERLAP))
        if len(arrays['note'])!=expected:raise ValueError('Activation frame count does not match source duration')
        if any(raw.get(k)!=v for k,v in [('source_rate',RATE),('source_origin_sample',origin),('source_length',end-origin)]):raise ValueError('Recognition source geometry changed')
        anchors=np.minimum(frame_samples(expected+1,RATE,origin),end).tolist()
        if raw['frame_samples']!=anchors[:-1]:raise ValueError('Recognition source anchors changed')
        published=decode_saved(raw,'published_default')
    finally:
        if sys.path and sys.path[0]==overlay:sys.path.pop(0)
    onset={k:v for k,v in raw.items() if k!='raw_predictions'}
    for interpretation in (published,onset):
        rejected=[];kept=[]
        for event in interpretation['events']:
            if event.get('start')==end and event.get('end')==end:
                rejected.append({**event,'reason':'Clipped terminal anchors have no positive source duration'})
            else:kept.append(event)
        if len(interpretation['events'])>MAX_EVENTS:raise ValueError('Candidate event budget exceeded')
        interpretation['events']=kept;interpretation['rejected_boundary_events']=rejected
        validate_events(kept,origin,end)
        interpretation['source_clock']={'sample_rate':RATE,'origin':origin,'end':end,'frame_samples':onset['frame_samples'],
                                        'official_frame_samples':onset['official_frame_samples'],'source_coverage':onset['source_coverage']}
        if rejected:interpretation.setdefault('warnings',[]).append('Terminal zero-duration events retained in rejected_boundary_events, excluded from playable candidates.')
    npz=work/'activations.npz'
    with (work/'activations.npz.partial').open('xb') as stream:np.savez_compressed(stream,**arrays)
    if (work/'activations.npz.partial').stat().st_size>MAX_ARRAYS:raise ValueError('Compressed arrays exceed 16 MiB')
    (work/'activations.npz.partial').replace(npz)
    payload={'version':1,'source_id':request['source_id'],'source_sha256':request['source_sha256'],'sample_rate':RATE,'origin':origin,'end':end,'source_part':PART,
             'interpretations':{'published':published,'onset_only':onset},'timebase':report['timebase'],
             'warnings':['Estimated other stem may contain multiple instruments and separation leakage; no guitar identification is performed.',
                         'Activations are uncalibrated model outputs, not accuracy or confidence probabilities.',
                         'Published event decoding uses custom source anchors; it is not complete official timing or MIDI equivalence.',
                         'Candidate endings and contour bins are model estimates, not physical releases or identified techniques.'],
             'provenance':{'pipeline_version':VERSION,'separation':report['provenance'],'recognition':onset.get('metadata'),
                           'model_sha256':onset.get('model_sha256'),'source_anchors':onset.get('source_coverage')},
             'raw_activations':{'file':'activations.npz','sha256':sha(npz),'bytes':npz.stat().st_size,'arrays':{k:{'shape':list(a.shape),'dtype':'float32'} for k,a in arrays.items()},
                                'finite':True,'range':[0,1],'sample_rate':RATE,'origin':origin,'end':end,'frame_samples':onset['frame_samples'],'official_frame_samples':onset['official_frame_samples']}}
    write_json(work/'candidates.json',payload);emit({'phase':'candidates saved','progress':1})
    return {'candidates_sha256':sha(work/'candidates.json'),'candidates_bytes':(work/'candidates.json').stat().st_size,'activations_sha256':sha(npz),'activations_bytes':npz.stat().st_size}

def run(request,emit):
    work,origin,end=validate_request(request)
    names=(['other.npy','separation.json','decoded.wav','decoded.wav.decode.partial'] if request['stage']=='separate' else ['activations.npz','candidates.json'])
    owned=[work/name for name in names]+[work/(name+'.partial') for name in names]
    if any(path.exists() for path in owned):raise FileExistsError('Instrumental stage output is occupied')
    os.environ['OMP_NUM_THREADS']='2';os.environ['NUMBA_NUM_THREADS']='2'
    try:
        from .instrumental_runtime import require_runtime
        runtime=require_runtime()
        return (_separate if request['stage']=='separate' else _recognize)(request,runtime,work,origin,end,emit)
    except BaseException:
        for path in owned:path.unlink(missing_ok=True)
        raise
