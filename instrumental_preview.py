"""Unknown-instrument candidates and explicit source-grounded solo selection."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from analysis_evidence import AnalysisEvidence, digest, work_directory
from audio.performance import Note, Performance
from audio.preview_instruments import selection_fields, arrange_selected, renderer_version
from audio.instrumental_analysis import VERSION as PIPELINE_VERSION
from audio.instrumental_runtime import PINS

VERSION='instrumental-review-v1'
MAX_JSON=8*1024**2
MAX_RESULT=12*1024**2
INTERPRETATIONS=('published','onset_only')
MODEL_SHA=PINS['research/basic-pitch/nmp.onnx']

def source_anchors(origin,end):
    frames=((end-origin+1)//2)*142//36164
    return [min(end,origin+2*((i//142)*36164+(i%142)*256)) for i in range(frames+1)]

def _settings(actual,expected):
    if type(actual) is not dict or any(type(actual.get(k)) is not type(v) or actual[k]!=v for k,v in expected.items()):
        raise ValueError('Instrumental decoder policy changed')

def canonical(value,maximum=MAX_RESULT):
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>maximum:raise ValueError('This instrumental passage exceeds the saved evidence limit.')
    return raw

def _finite(value,depth=0):
    if depth>16:raise ValueError('Invalid instrumental metadata depth')
    if value is None or type(value) in (bool,int):return
    if type(value) is float and math.isfinite(value):return
    if type(value) is str and len(value)<=8192:return
    if type(value) is list and len(value)<=100000:
        for item in value:_finite(item,depth+1)
        return
    if type(value) is dict and len(value)<=256 and all(type(k) is str for k in value):
        for item in value.values():_finite(item,depth+1)
        return
    raise ValueError('Invalid or nonfinite instrumental metadata')

def validate_bundle(bundle,*,source_id,source_sha256,origin,end):
    """Return detached evidence; no guessing instrument, confidence or role."""
    if type(bundle) is not dict:raise ValueError('Invalid instrumental candidate bundle')
    canonical(bundle,MAX_JSON);_finite(bundle)
    if (type(bundle.get('warnings')) is not list or len(bundle['warnings'])>128
        or any(type(s) is not str for s in bundle['warnings']) or type(bundle.get('provenance')) is not dict):
        raise ValueError('Invalid instrumental provenance or warnings')
    if (type(bundle.get('version')) is not int or bundle['version']!=1
        or bundle.get('source_id')!=source_id or bundle.get('source_sha256')!=source_sha256
        or type(bundle.get('sample_rate')) is not int or bundle['sample_rate']!=44100
        or type(bundle.get('origin')) is not int or type(bundle.get('end')) is not int
        or (bundle['origin'],bundle['end'])!=(origin,end) or not 0<=origin<end<=origin+10*44100):
        raise ValueError('Instrumental source identity or timing failed validation')
    part=bundle.get('source_part')
    if type(part) is not dict or part.get('id')!='umx:other' or part.get('instrument')!='unknown':
        raise ValueError('An instrumental mixture must retain its unknown instrument identity')
    interpretations=bundle.get('interpretations')
    if type(interpretations) is not dict or set(interpretations)!=set(INTERPRETATIONS):
        raise ValueError('Both instrumental interpretations must be retained')
    anchors=source_anchors(origin,end);frames=len(anchors)-1
    if not 3<=frames<=1000:raise ValueError('Invalid instrumental frame count')
    coverage=dict(first_anchor=origin,last_boundary=anchors[-1],passage_end=end,uncovered_tail_samples=end-anchors[-1])
    if bundle['provenance'].get('pipeline_version')!=PIPELINE_VERSION or bundle['provenance'].get('model_sha256')!=MODEL_SHA:
        raise ValueError('Instrumental pipeline or model identity changed')
    for name,report in interpretations.items():
        if type(report) is not dict or type(report.get('events')) is not list or len(report['events'])>4000:
            raise ValueError('Invalid instrumental event list')
        clock=report.get('source_clock')
        if (type(clock) is not dict or any(type(clock.get(k)) is not int for k in ('sample_rate','origin','end'))
            or (clock.get('sample_rate'),clock.get('origin'),clock.get('end'))!=(44100,origin,end)):
            raise ValueError('Instrumental interpretation clock failed validation')
        if clock.get('frame_samples')!=anchors[:-1] or clock.get('source_coverage')!=coverage:
            raise ValueError('Instrumental source anchors changed')
        if name=='published':
            if report.get('policy')!='published_default':raise ValueError('Invalid default event interpretation')
            _settings(report.get('settings'),dict(onset_thresh=.5,frame_thresh=.3,min_note_len=11,
                                                 infer_onsets=True,melodia_trick=True,energy_tol=11))
        else:
            if report.get('version')!='basic-pitch-onset-only-research-v1' or report.get('model_sha256')!=MODEL_SHA:
                raise ValueError('Invalid onset-only interpretation')
            _settings(report.get('policy'),dict(onset_threshold=.5,frame_threshold=.3,minimum_duration_frames=0,
                                              gap_tolerance_frames=0,infer_onsets=False,melodia_trick=False))
        seen=set();previous=origin
        for event in report['events']:
            if type(event) is not dict:raise ValueError('Invalid instrumental event')
            identity=event.get('id')
            if type(identity) is not str or not identity or len(identity)>200 or identity in seen:
                raise ValueError('Invalid instrumental event identity')
            seen.add(identity)
            if (any(type(event.get(k)) is not int for k in ('start','end','pitch'))
                or not origin<=event['start']<event['end']<=end or event['start']<previous
                or not 21<=event['pitch']<=108):
                raise ValueError('Instrumental event exceeds its source clock or pitch range')
            previous=event['start']
            first,last=event.get('start_frame'),event.get('end_frame_exclusive')
            if (type(first) is not int or type(last) is not int or not 0<=first<last<=frames
                or (event['start'],event['end'])!=(anchors[first],anchors[last])):
                raise ValueError('Instrumental event does not match its model frame anchors')
            if name=='published' and last-first<=11:
                raise ValueError('Default interpretation violates its minimum-duration policy')
            activation=event.get('mean_note_activation')
            if type(activation) not in (int,float) or not math.isfinite(activation) or not 0<=activation<=1:
                raise ValueError('Invalid uncalibrated note activation')
            contour=event.get('contour_candidates',[])
            if type(contour) is not list:raise ValueError('Invalid instrumental contour list')
            for point in contour:
                if (type(point) is not dict or type(point.get('sample')) is not int
                    or not event['start']<=point['sample']<=event['end']
                    or type(point.get('midi')) not in (int,float) or not math.isfinite(point['midi'])):
                    raise ValueError('Invalid instrumental contour evidence')
    raw=bundle.get('raw_activations')
    if (type(raw) is not dict or raw.get('file')!='activations.npz'
        or type(raw.get('sha256')) is not str or not re.fullmatch('[0-9a-f]{64}',raw['sha256'])
        or type(raw.get('bytes')) is not int or not 0<raw['bytes']<=16*1024**2):
        raise ValueError('Invalid raw instrumental evidence reference')
    if (raw.get('finite') is not True or raw.get('range')!=[0,1]
        or any(type(raw.get(k)) is not int for k in ('sample_rate','origin','end'))
        or (raw['sample_rate'],raw['origin'],raw['end'])!=(44100,origin,end)
        or raw.get('frame_samples')!=anchors[:-1]
        or raw.get('arrays')!={k:dict(shape=[frames,width],dtype='float32') for k,width in (('note',88),('onset',88),('contour',264))}):
        raise ValueError('Raw instrumental array geometry or source clock changed')
    return deepcopy(bundle)

def source_recording(library,spec):
    source=library.get(spec['asset_id'])
    if source['kind']!='original' or source['sha256']!=spec['sha256'] or not library.verify(source['id']):
        raise ValueError('The original recording failed verification; it is unchanged.')
    return source

def checked_candidates(library,job):
    if job['state']!='complete' or job['request'].get('kind')!='instrumental':
        raise ValueError('Choose a completed instrumental review.')
    spec=job['request'];source=source_recording(library,spec)
    result=job['result'];reference=result['evidence']
    bundle=validate_bundle(result['candidates'],source_id=source['source_id'],source_sha256=source['sha256'],
        origin=round(spec['start_seconds']*44100),end=round(spec['end_seconds']*44100))
    raw=bundle['raw_activations']
    if (reference.get('sha256'),reference.get('bytes'))!=(raw['sha256'],raw['bytes']):
        raise ValueError('Raw instrumental evidence does not match its bundle')
    AnalysisEvidence(library).verify(reference)
    return bundle,source

def prepare_arrangement(library,value):
    if (type(value) is not dict or set(value)-{'parent_job_id','interpretation','note_ids','instrument','accept_as_lead','electric_tone'}
        or not {'parent_job_id','interpretation','note_ids','instrument','accept_as_lead'}<=set(value)
        or value['accept_as_lead'] is not True):
        raise ValueError('Select notes and explicitly accept them as a proposed lead part.')
    identity=value['parent_job_id']
    if type(identity) is not str or str(uuid.UUID(identity))!=identity:
        raise ValueError('Choose a saved instrumental review.')
    parent=library.job(identity);bundle,source=checked_candidates(library,parent)
    interpretation=value['interpretation']
    if interpretation not in INTERPRETATIONS:raise ValueError('Choose one instrumental interpretation.')
    ids=value['note_ids']
    if (type(ids) is not list or not 1<=len(ids)<=4000 or any(type(i) is not str for i in ids)
        or len(set(ids))!=len(ids)):
        raise ValueError('Select between one and4000 unique candidate notes.')
    events=bundle['interpretations'][interpretation]['events'];known={n['id'] for n in events}
    if not set(ids)<=known:raise ValueError('A selected note is not in this saved interpretation.')
    selection=selection_fields({key:value[key] for key in ('instrument','electric_tone') if key in value},1)
    spec=dict(kind='instrumental-arrange',parent_job_id=identity,
              parent_result_sha256=hashlib.sha256(canonical(parent['result'])).hexdigest(),
              asset_id=source['id'],sha256=source['sha256'],performers=1,
              start_seconds=parent['request']['start_seconds'],end_seconds=parent['request']['end_seconds'],
              interpretation=interpretation,note_ids=sorted(ids),accept_as_lead=True,
              role_assignment='user-selected-lead',review_version=VERSION,
              renderer_version=renderer_version(selection),**selection)
    return dict(spec=spec,parent=parent,bundle=bundle,source=source,selection=selection)

def arrange_review(prepared):
    spec=prepared['spec'];bundle=prepared['bundle']
    events=bundle['interpretations'][spec['interpretation']]['events'];ids=set(spec['note_ids'])
    notes=[]
    for event in events:
        if event['id'] not in ids:continue
        notes.append(Note(event['id'],event['start'],event['end'],event['pitch'],80,'melody',
                          event['mean_note_activation'],contour=tuple((p['sample'],p['midi']) for p in event.get('contour_candidates',[]))))
    performance=Performance(bundle['source_id'],44100,notes)
    arrangement=arrange_selected(performance,1,None,prepared['selection'],render_origin_sample=bundle['origin'])
    arrangement['render_origin_sample']=bundle['origin']
    arrangement['candidate_review']=dict(version=VERSION,parent_job_id=spec['parent_job_id'],
        parent_result_sha256=spec['parent_result_sha256'],source_part=deepcopy(bundle['source_part']),
        interpretation=spec['interpretation'],selected_note_ids=spec['note_ids'],
        unselected_note_ids=[e['id'] for e in events if e['id'] not in ids],
        role_assignment='User-selected lead; not instrument identification or analyzer truth',
        confidence_semantics='Compatibility field contains uncalibrated mean note activation, not correctness probability',
        velocity_policy='Fixed80 for sampled preview; dynamics were not inferred',
        other_interpretation_retained_in_parent=True)
    arrangement['adaptations'].extend(dict(note_id=e['id'],kind='candidate-not-selected',
        reason='Unselected uncertain candidate; retained in the parent review, not rendered') for e in events if e['id'] not in ids)
    if not arrangement['notes']:raise ValueError('None of the selected notes fit this instrument. Review the listed range constraints.')
    if prepared['selection'].get('instrument')=='electric' and arrangement.get('renderable') is not True:
        raise ValueError('The selected notes could not all be assigned to this Electric lead. Review the omissions before rendering.')
    return arrangement

def _receipt(path,receipt,prefix,maximum):
    size=receipt.get(prefix+'_bytes');checksum=receipt.get(prefix+'_sha256')
    if (type(size) is not int or not 0<size<=maximum or type(checksum) is not str
        or not re.fullmatch('[0-9a-f]{64}',checksum) or path.is_symlink()
        or path.stat().st_size!=size or digest(path)!=checksum):
        raise ValueError('Instrumental analysis receipt failed validation')

def analyze_job(library,spec,identity,*,cancel,progress,run_worker):
    from audio.instrumental_runtime import require_runtime
    runtime=require_runtime(cancel=cancel)
    if (spec.get('runtime_version')!=runtime['runtime_version'] or spec.get('review_version')!=VERSION
        or spec.get('pipeline_version')!=PIPELINE_VERSION):
        raise ValueError('The instrumental analysis runtime changed. Inspect the passage again.')
    source=source_recording(library,spec);directory=work_directory(library,identity)
    directory.mkdir(exist_ok=False);library.space(64*1024**2)
    request=dict(work_dir=str(directory.resolve()),source_path=str((library.media/source['sha256']).resolve()),
                 source_sha256=source['sha256'],source_id=source['source_id'],
                 start_seconds=spec['start_seconds'],end_seconds=spec['end_seconds'])
    metrics={}
    for stage,weight in (('separate',0.),('recognize',.5)):
        if cancel():raise InterruptedError('Analysis cancelled')
        response=run_worker('audio.instrumental_analysis',{**request,'stage':stage},python=runtime['python'],
            cancel=cancel,progress=lambda event:progress(weight+max(0,min(1,float(event.get('progress',0))))*.45),
            wall_seconds=120,memory_bytes=2*1024**3)
        metrics[stage]=response['metrics'];receipt=response['result']
        if stage=='separate':
            _receipt(directory/'separation.json',receipt,'separation',MAX_JSON)
            _receipt(directory/'other.npy',receipt,'other',16*1024**2)
    _receipt(directory/'candidates.json',receipt,'candidates',MAX_JSON)
    _receipt(directory/'activations.npz',receipt,'activations',16*1024**2)
    if cancel():raise InterruptedError('Analysis cancelled')
    raw=(directory/'candidates.json').read_bytes()
    def nonfinite(value):raise ValueError('Invalid nonfinite candidate metadata')
    bundle=validate_bundle(json.loads(raw,parse_constant=nonfinite),source_id=source['source_id'],source_sha256=source['sha256'],
        origin=round(spec['start_seconds']*44100),end=round(spec['end_seconds']*44100))
    reference=dict(sha256=receipt['activations_sha256'],bytes=receipt['activations_bytes'],format='numpy-npz-v1')
    if (bundle['raw_activations']['sha256'],bundle['raw_activations']['bytes'])!=(reference['sha256'],reference['bytes']):
        raise ValueError('Candidate bundle does not match raw activations')
    AnalysisEvidence(library).publish(directory/'activations.npz',reference,cancel)
    progress(1.)
    return dict(candidates=bundle,evidence=reference,metrics=metrics,
                notice='Estimated instrumental mixture with unknown instruments. Review both interpretations; extra notes, missing attacks and truncated sustain remain possible. Nothing plays until you explicitly select and preview notes.')
