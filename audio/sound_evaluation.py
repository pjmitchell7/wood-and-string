"""Descriptive, evidence-bound checks; no audio playback or musical quality score."""
import array
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

VERSION = 'sound-evaluation-v1'
STAGES = ('source_observation','interpretation','arrangement','dry_render','effects')
BASES = ('synthetic_commands','human_annotation','estimated_source','unavailable','registered_user_edit','control_render','measured_input')
SCOPES = ('measured_signal','command_transport','source_transcription','arrangement_intent','effect_isolation','perceptual')
MAX_BYTES = 64*1024**2
CHRONOLOGY = 'Hashing this contract does not establish that its criteria predate observations; verify the named registration evidence.'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,ensure_ascii=True).encode()


def _text(value, limit=2048):
    if type(value) is not str or not 0<len(value)<=limit: raise ValueError('Expected bounded nonempty text')
    return value


def _number(value):
    try: valid=type(value) in (int,float) and math.isfinite(value)
    except OverflowError: valid=False
    if not valid: raise ValueError('Expected finite number, not a boolean')
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}',value): raise ValueError('Expected exact SHA-256')
    return value


def _clock(clock):
    if type(clock) is not dict or set(clock)!={'sample_rate','origin_sample','end_sample'}: raise ValueError('Expected source clock')
    if any(type(v) is not int for v in clock.values()) or not 1<=clock['sample_rate']<=384000 or not 0<=clock['origin_sample']<clock['end_sample']:
        raise ValueError('Invalid integer source clock')
    return clock


def _window(window, clock):
    if type(window) is not dict or set(window)!={'start','end'} or any(type(v) is not int for v in window.values()): raise ValueError('Expected integer absolute sample window')
    if not clock['origin_sample']<=window['start']<window['end']<=clock['end_sample']: raise ValueError('Window outside source clock')


def _reference(ref):
    if type(ref) is not dict or not {'id','kind','sha256','bytes'}<=set(ref) or set(ref)-{'id','kind','sha256','bytes','clock'}: raise ValueError('Invalid evidence reference')
    _text(ref['id'],200);_hash(ref['sha256'])
    if ref['kind'] not in ('control_plan','source_audio','annotation','estimated_events','command_events','dry_render','wet_render','measurement','edit_receipt','method_note'): raise ValueError('Unknown evidence kind')
    if type(ref['bytes']) is not int or ref['bytes']<0: raise ValueError('Invalid evidence byte length')
    if 'clock' in ref: _clock(ref['clock'])
    return ref


def _refs(items):
    if type(items) is not list or len(items)>256: raise ValueError('Evidence reference bound exceeded')
    result={}
    for ref in items:
        _reference(ref)
        if ref['id'] in result: raise ValueError('Duplicate evidence identity')
        result[ref['id']]=ref
    return result


def freeze_contract(contract):
    """Freeze declared criteria, never manufacture proof of prior registration."""
    keys={'version','comparison_id','analysis_mode','registration','clock','references','expectations'}
    if type(contract) is not dict or set(contract)!=keys or contract['version']!=VERSION: raise ValueError('Invalid evaluation contract')
    _text(contract['comparison_id'],200);clock=_clock(contract['clock']);refs=_refs(contract['references'])
    if contract['analysis_mode'] not in ('prospective_frozen','retrospective_diagnostic'): raise ValueError('Declare prospective or retrospective analysis')
    registration=contract['registration']
    if type(registration) is not dict or set(registration)!={'reference_id','declared_before_observation','scope'}: raise ValueError('Registration source and timing required')
    if registration['reference_id'] not in refs or refs[registration['reference_id']]['kind']!='control_plan': raise ValueError('Registration must name a hash-bound control plan')
    if type(registration['declared_before_observation']) is not bool: raise ValueError('Explicit registration timing required')
    _text(registration['scope'])
    if contract['analysis_mode']=='prospective_frozen' and not registration['declared_before_observation']: raise ValueError('Post-observation criteria cannot be prospective')
    if type(contract['expectations']) is not list or not 1<=len(contract['expectations'])<=128: raise ValueError('Expectation count outside bound')
    seen=set()
    for e in contract['expectations']:
        if type(e) is not dict or set(e)!={'id','stage','claim_scope','reference_basis','description','metric','unit','window','criterion','reference_ids','next_discriminating_check'}: raise ValueError('Invalid expectation fields')
        _text(e['id'],200)
        if e['id'] in seen: raise ValueError('Duplicate expectation identity')
        seen.add(e['id'])
        if e['stage'] not in STAGES or e['claim_scope'] not in SCOPES or e['reference_basis'] not in BASES: raise ValueError('Invalid stage or evidence scope')
        for key in ('description','metric','unit','next_discriminating_check'): _text(e[key])
        _window(e['window'],clock)
        if type(e['reference_ids']) is not list or not e['reference_ids'] or len(set(e['reference_ids']))!=len(e['reference_ids']) or not set(e['reference_ids'])<=refs.keys(): raise ValueError('Expected behavior needs unique registered references')
        criterion=e['criterion']
        if type(criterion) is not dict or set(criterion)!={'operator','expected','absolute_tolerance'}: raise ValueError('Registered numeric criterion required')
        if _number(criterion['absolute_tolerance'])<0: raise ValueError('Negative tolerance')
        op=criterion['operator'];expected=criterion['expected']
        if op=='report_only':
            if expected is not None or criterion['absolute_tolerance']!=0: raise ValueError('Descriptive-only criterion has no numeric target')
        elif op=='between':
            if type(expected) is not list or len(expected)!=2 or _number(expected[0])>_number(expected[1]): raise ValueError('Expected ordered interval')
        elif op in ('equal','at_most','at_least'): _number(expected)
        else: raise ValueError('Unknown registered comparison')
    raw=canonical(contract)
    if len(raw)>256*1024: raise ValueError('Contract exceeds byte bound')
    return {'contract':json.loads(raw),'sha256':hashlib.sha256(raw).hexdigest(),'chronology_limit':CHRONOLOGY}


def evaluate(frozen, observations):
    """Numerical findings, with causal assertions kept separate from measurement."""
    checked=freeze_contract(frozen['contract'])
    if checked!=frozen: raise ValueError('Frozen contract identity differs')
    c=checked['contract'];registered=_refs(c['references']);lookup={e['id']:e for e in c['expectations']}
    if type(observations) is not list or len(observations)>128: raise ValueError('Observation bound exceeded')
    supplied={};all_refs=dict(registered)
    for o in observations:
        if type(o) is not dict or set(o)!={'expectation_id','stage','metric','unit','window','clock','value','evidence_refs','method','uncertainty','proximate_failure','mechanism_hypotheses','alternatives'}: raise ValueError('Invalid observation fields')
        key=o['expectation_id']
        if key not in lookup or key in supplied or o['stage'] not in STAGES: raise ValueError('Unknown/duplicate observation or stage')
        if o['value'] is not None: _number(o['value'])
        _text(o['method']);_text(o['metric']);_text(o['unit']);_window(o['window'],_clock(o['clock']));refs=_refs(o['evidence_refs'])
        if not refs: raise ValueError('Observation needs hash-bound evidence')
        for rid,ref in refs.items():
            if rid in all_refs and ref!=all_refs[rid]: raise ValueError('Observation substituted evidence identity')
            all_refs[rid]=ref
        for field in ('uncertainty','alternatives'):
            if type(o[field]) is not list or len(o[field])>32: raise ValueError('Bounded uncertainty/alternatives required')
            for text in o[field]: _text(text)
        if type(o['mechanism_hypotheses']) is not list or len(o['mechanism_hypotheses'])>16: raise ValueError('Bounded hypotheses required')
        claims=o['mechanism_hypotheses']+([] if o['proximate_failure'] is None else [o['proximate_failure']])
        for claim in claims:
            if type(claim) is not dict or set(claim)!={'statement','reference_ids'}: raise ValueError('Cause claim needs statement and evidence')
            _text(claim['statement'])
            if type(claim['reference_ids']) is not list or not claim['reference_ids'] or not set(claim['reference_ids'])<=set(registered)|set(refs): raise ValueError('Unbound causal assertion')
        supplied[key]=o
    if len(canonical(observations))>256*1024: raise ValueError('Observation evidence exceeds byte bound')
    findings=[]
    for key,e in lookup.items():
        o=supplied.get(key);why=[];value=None if o is None else o['value'];delta=None
        if o is None or value is None: why.append('No numerical observation available.')
        if e['reference_basis']=='unavailable': why.append('Expected reference truth is unavailable.')
        if e['claim_scope']=='perceptual': why.append('Signal statistics cannot establish a listening judgment.')
        refs={} if o is None else _refs(o['evidence_refs'])
        if e['claim_scope']=='source_transcription':
            if e['reference_basis']!='human_annotation' or not any(registered[r]['kind']=='annotation' and 'clock' in registered[r] for r in e['reference_ids']): why.append('No clock-bound source annotation ground truth; estimated notes are not transcription truth.')
        for rid in e['reference_ids']:
            ref=registered[rid]
            if 'clock' in ref and (ref['clock']['sample_rate']!=c['clock']['sample_rate'] or not ref['clock']['origin_sample']<=e['window']['start']<e['window']['end']<=ref['clock']['end_sample']): why.append('Registered reference clock does not cover this expectation.')
        if o:
            if o['metric']!=e['metric'] or o['unit']!=e['unit'] or o['window']!=e['window'] or o['clock']['sample_rate']!=c['clock']['sample_rate']: why.append('Observed metric, unit, window or sample rate differs from the registered criterion.')
            if o['stage']!=e['stage']: why.append('Observation belongs to a different causal stage.')
            if e['stage']=='dry_render' and any(r['kind']=='wet_render' for r in refs.values()): why.append('Wet output cannot establish the dry-render expectation.')
            for ref in refs.values():
                if 'clock' in ref and (ref['clock']['sample_rate']!=c['clock']['sample_rate'] or not ref['clock']['origin_sample']<=e['window']['start']<e['window']['end']<=ref['clock']['end_sample']): why.append('Evidence clock does not cover this registered window.')
        criterion=e['criterion'];target=criterion['expected'];tol=criterion['absolute_tolerance'];op=criterion['operator']
        if op=='report_only': why.append('Descriptive measurement only; no registered pass/fail criterion.')
        status='inconclusive'
        if value is not None and op!='report_only':
            delta={'from_lower':value-target[0],'from_upper':value-target[1]} if op=='between' else value-target
            lower=target[0]-tol if op=='between' else target-tol
            upper=target[1]+tol if op=='between' else target+tol
            derived=[*delta.values()] if isinstance(delta,dict) else [delta]
            if not all(math.isfinite(v) for v in [*derived,lower,upper]):
                delta=None;why.append('Numerical difference or tolerance boundary is not finitely representable.')
            else:
                within=(lower<=value<=upper if op=='between' else abs(delta)<=tol if op=='equal' else value<=upper if op=='at_most' else value>=lower)
                if not why: status='supports' if within else 'violates'
        findings.append({'expectation_id':key,'stage':e['stage'],'analysis_mode':c['analysis_mode'],
            'expected':deepcopy(e),'observed':value,'delta':delta,'units':e['unit'],'time_window':deepcopy(e['window']),
            'status':status,'status_reasons':why,'expected_evidence_refs':[deepcopy(registered[r]) for r in e['reference_ids']],
            'observed_evidence_refs':deepcopy(list(refs.values())),
            'observation_binding':None if o is None else {k:deepcopy(o[k]) for k in ('stage','metric','unit','window','clock','method')},
            'reported_proximate_observation':None if o is None else deepcopy(o['proximate_failure']),
            'mechanism_hypotheses':[] if o is None else deepcopy(o['mechanism_hypotheses']),
            'causal_limit':'Causal text is an author assertion bound to named evidence; this numerical evaluator does not establish mechanism.',
            'alternatives':[] if o is None else list(o['alternatives']),
            'uncertainty':['No listening performed by this evaluator.']+([] if o is None else list(o['uncertainty'])),
            'next_discriminating_check':e['next_discriminating_check']})
    return {'version':VERSION,'comparison_id':c['comparison_id'],'contract_sha256':checked['sha256'],
            'analysis_mode':c['analysis_mode'],'chronology_limit':CHRONOLOGY,'findings':findings,
            'listening_performed':False}


def measure_f32(path, *, expected_sha256, sample_rate, origin_sample, frames, windows, reference_id, stage):
    """One bounded pass: little-endian stereo float32, absolute half-open windows.

    The SHA is computed from the same bytes as the measurements. Nonfinite data,
    size/hash mismatch or changed file identity fails without returning metrics.
    Zero means exact digital zero, never an audibility claim.
    """
    _hash(expected_sha256);_text(reference_id,200)
    if type(frames) is not int or type(origin_sample) is not int or not 0<frames<=MAX_BYTES//8: raise ValueError('Raw frame bound exceeded')
    clock=_clock({'sample_rate':sample_rate,'origin_sample':origin_sample,'end_sample':origin_sample+frames})
    if stage not in ('source_observation','dry_render','effects'): raise ValueError('Declare the measured signal stage')
    if type(windows) is not list or not 1<=len(windows)<=64: raise ValueError('Window count outside bound')
    rows={}
    for w in windows:
        if type(w) is not dict or set(w)!={'id','start','end'}: raise ValueError('Invalid measurement window')
        _text(w['id'],200);_window({'start':w['start'],'end':w['end']},clock)
        if w['id'] in rows: raise ValueError('Duplicate window identity')
        rows[w['id']]={**w,'peak':0.,'sum':0.,'correction':0.,'nonzero_frames':0,'first_nonzero_sample':None,'last_nonzero_sample':None}
    path=Path(path)
    for part in (path.absolute(),*path.absolute().parents):
        st=part.lstat()
        if part.is_symlink() or getattr(st,'st_file_attributes',0)&0x400: raise ValueError('Linked measurement input unsupported')
    digest=hashlib.sha256();read_frames=0
    with path.open('rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size!=frames*8: raise ValueError('Raw stereo float geometry differs')
        while raw:=stream.read(4096*8):
            if len(raw)%8 or read_frames+len(raw)//8>frames: raise ValueError('Raw frame geometry changed')
            digest.update(raw);values=array.array('f');values.frombytes(raw)
            if sys.byteorder!='little': values.byteswap()
            if not all(math.isfinite(v) for v in values): raise ValueError('Nonfinite float sample')
            for row in rows.values():
                lo=max(0,row['start']-origin_sample-read_frames);hi=min(len(values)//2,row['end']-origin_sample-read_frames)
                if lo>=hi: continue
                part=values[lo*2:hi*2];row['peak']=max(row['peak'],max(map(abs,part)))
                term=math.fsum(v*v for v in part);total=row['sum']+term
                row['correction']+=(row['sum']-total)+term if abs(row['sum'])>=abs(term) else (term-total)+row['sum']
                row['sum']=total
                for i in range(lo,hi):
                    if values[i*2]!=0 or values[i*2+1]!=0:
                        absolute=origin_sample+read_frames+i;row['nonzero_frames']+=1
                        if row['first_nonzero_sample'] is None: row['first_nonzero_sample']=absolute
                        row['last_nonzero_sample']=absolute
            read_frames+=len(values)//2
        after=os.fstat(stream.fileno());current=path.stat()
    identities=[(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns) for s in (before,after,current)]
    if read_frames!=frames or digest.hexdigest()!=expected_sha256 or identities[0]!=identities[1] or identities[0]!=identities[2]: raise ValueError('Measured input identity/hash changed')
    output=[]
    for row in rows.values():
        energy=row.pop('sum')+row.pop('correction');count=row['end']-row['start']
        output.append({**row,'frames':count,'sample_values':2*count,'sum_squares':energy,
            'mean_square':energy/(2*count),'rms':math.sqrt(energy/(2*count))})
    kind={'source_observation':'source_audio','dry_render':'dry_render','effects':'wet_render'}[stage]
    return {'reference':{'id':reference_id,'kind':kind,'sha256':expected_sha256,'bytes':frames*8,'clock':clock},
        'stage':stage,'format':'stereo little-endian float32 interleaved','windows':output,
        'metric_scope':'Linear sample amplitude; sum_squares pools both channels, not duration-normalized loudness. Exact nonzero frames are not audibility.',
        'listening_performed':False}


def compare_events(expected, observed, *, clock, tolerances, reference_basis, omissions=(), edit_lineage=()):
    """ID-based event transport diagnostics; no inferred transcription accuracy."""
    _clock(clock)
    if reference_basis not in BASES: raise ValueError('Declare event reference basis')
    if type(tolerances) is not dict or set(tolerances)!={'start_samples','end_samples','pitch_semitones','velocity_units','registration_sha256'}: raise ValueError('Registered event tolerances required')
    _hash(tolerances['registration_sha256'])
    for k in ('start_samples','end_samples','pitch_semitones','velocity_units'):
        if _number(tolerances[k])<0: raise ValueError('Negative event tolerance')
    def index(events):
        if type(events) is not list or len(events)>4000: raise ValueError('Event count bound exceeded')
        out={};total_bytes=0
        for n in events:
            if type(n) is not dict or not {'id','start','end','pitch','velocity'}<=n.keys(): raise ValueError('Incomplete event')
            if n.keys()-{'id','start','end','pitch','velocity','contour','role','role_hint','player','string','fret','articulation','phrase','confidence'}: raise ValueError('Unsupported event fields; supply the declared event projection')
            _text(n['id'],200);_window({'start':n['start'],'end':n['end']},clock)
            if n['id'] in out: raise ValueError('Duplicate event identity')
            _number(n['pitch']);_number(n['velocity'])
            for field in ('role','role_hint','articulation','phrase'):
                if field in n: _text(n[field],200)
            for field in ('player','string','fret'):
                if field in n and (type(n[field]) is not int or n[field]<0): raise ValueError('Invalid event assignment')
            if 'confidence' in n: _number(n['confidence'])
            contour=n.get('contour',[])
            if type(contour) not in (list,tuple) or len(contour)>4096: raise ValueError('Contour bound/shape differs')
            previous=-1
            for point in contour:
                if type(point) not in (list,tuple) or len(point)!=2 or type(point[0]) is not int or not n['start']<=point[0]<=n['end'] or point[0]<previous: raise ValueError('Invalid source-timed contour')
                _number(point[1]);previous=point[0]
            total_bytes+=len(canonical(n))
            if total_bytes>256*1024: raise ValueError('Event evidence exceeds byte bound')
            out[n['id']]=n
        return out
    left,right=index(expected),index(observed);declared={};lineage_sources=set();lineage_results=set()
    if type(omissions) not in (list,tuple) or type(edit_lineage) not in (list,tuple) or len(omissions)>4000 or len(edit_lineage)>4000: raise ValueError('Edit evidence count bound exceeded')
    if len(canonical([omissions,edit_lineage]))>256*1024: raise ValueError('Edit evidence exceeds byte bound')
    for omission in omissions:
        if type(omission) is not dict or set(omission)!={'note_id','reason','registration_sha256'}: raise ValueError('Omission needs registered intent')
        nid=omission['note_id'];_text(omission['reason']);_hash(omission['registration_sha256'])
        if nid not in left or nid in right or nid in declared: raise ValueError('Contradictory or duplicate omission')
        declared[nid]=omission
    for edit in edit_lineage:
        if type(edit) is not dict or set(edit)!={'operation','source_ids','result_ids','reason','registration_sha256'} or edit['operation'] not in ('join','split','other'): raise ValueError('Registered edit lineage required')
        _hash(edit['registration_sha256']);_text(edit['reason'])
        sources,results=edit['source_ids'],edit['result_ids']
        if type(sources) is not list or type(results) is not list or not sources or not results or len(set(sources))!=len(sources) or len(set(results))!=len(results): raise ValueError('Invalid edit identities')
        if not set(sources)<=left.keys() or not set(results)<=right.keys() or set(sources)&(lineage_sources|declared.keys()) or set(results)&lineage_results: raise ValueError('Contradictory edit lineage')
        lineage_sources.update(sources);lineage_results.update(results)
    rows=[]
    for nid,n in left.items():
        current=right.get(nid);deltas={};changes=[]
        if current:
            for field,unit in (('start','start_samples'),('end','end_samples'),('pitch','pitch_semitones'),('velocity','velocity_units')):
                deltas[unit]=current[field]-n[field]
                _number(deltas[unit])
                if abs(deltas[unit])>tolerances[unit]: changes.append(field)
            if canonical(n.get('contour',[]))!=canonical(current.get('contour',[])): changes.append('contour')
            for field in ('role','role_hint','player','string','fret','articulation','phrase','confidence'):
                if n.get(field)!=current.get(field): changes.append(field)
        state=('registered_edit_unassessed' if nid in lineage_sources or nid in lineage_results else 'declared_omission' if nid in declared else 'missing' if current is None else 'changed' if changes else 'within_registered_tolerance' if any(deltas.values()) else 'preserved')
        rows.append({'id':nid,'status':state,'deltas':deltas,'changed_fields':changes,'expected':deepcopy(n),'observed':deepcopy(current),
                     'omission_intent':deepcopy(declared.get(nid))})
    for nid,n in right.items():
        if nid not in left: rows.append({'id':nid,'status':'registered_edit_unassessed' if nid in lineage_results else 'extra','expected':None,'observed':deepcopy(n),'deltas':{},'changed_fields':[]})
    return {'reference_basis':reference_basis,'tolerances':deepcopy(tolerances),'events':rows,'registered_edits':deepcopy(list(edit_lineage)),
            'accuracy_claim':False,'scope':'Exact-ID comparison only. Missing/extra IDs are not automatically recognizer errors; registered edits are reported, not validated as joins/splits.'}


def trace_event_changes(snapshots, *, clock, tolerances, reference_basis):
    """Locate the first changed representation, not a proven physical cause."""
    _clock(clock)
    if type(snapshots) is not list or not 2<=len(snapshots)<=len(STAGES): raise ValueError('Provide two to five stage snapshots')
    positions=[];transitions=[];first={}
    for snap in snapshots:
        if type(snap) is not dict or set(snap)!={'stage','events','reference'} or snap['stage'] not in STAGES: raise ValueError('Invalid stage snapshot')
        _reference(snap['reference']);positions.append(STAGES.index(snap['stage']))
        bound=snap['reference'].get('clock')
        if bound and (bound['sample_rate']!=clock['sample_rate'] or bound['origin_sample']>clock['origin_sample'] or bound['end_sample']<clock['end_sample']): raise ValueError('Stage reference clock does not cover the declared comparison clock')
    if positions!=sorted(set(positions)): raise ValueError('Stage snapshots must be unique and ordered')
    for before,after in zip(snapshots,snapshots[1:]):
        diff=compare_events(before['events'],after['events'],clock=clock,tolerances=tolerances,reference_basis=reference_basis)
        transitions.append({'from_stage':before['stage'],'to_stage':after['stage'],'input_reference':deepcopy(before['reference']),'output_reference':deepcopy(after['reference']),'comparison':diff})
        for event in diff['events']:
            if event['status'] not in ('preserved','within_registered_tolerance'): first.setdefault(event['id'],after['stage'])
    return {'transitions':transitions,'first_observed_change_by_id':first,
            'causal_limit':'First changed stored representation only; skipped stages and physical/perceptual causes remain unproven.'}
