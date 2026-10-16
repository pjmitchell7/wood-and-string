"""Pure source-grounded proposal boundary for virtual symbolic benchmarks.

This is not an installed-bank registry, model, renderer or music-quality judge.
Text is retained as data; nothing here evaluates commands or infers audio facts.
"""
from copy import deepcopy
import hashlib
import json
import math

VERSION='source-grounded-arranger-v1'
ROLES={'melody','bass','harmony','counterline','drums'}
INSTRUMENTS={'acoustic','electric','jazz-bass','drums'}


def _shape(value, required, optional=()):
    if type(value) is not dict or not set(required)<=value.keys() or value.keys()-set(required)-set(optional):
        raise ValueError('Missing or unknown contract fields')


def _int(value,lo,hi):
    if type(value) is not int or not lo<=value<=hi:raise ValueError('Invalid contract integer')
    return value


def _text(value,limit=200,empty=False):
    if type(value) is not str or len(value)>limit or (not empty and not value.strip()):raise ValueError('Invalid contract text')
    return value


def _list(value,maximum):
    if type(value) is not list or len(value)>maximum:raise ValueError('Invalid bounded list')
    return value


def _unique(values):
    if len(set(values))!=len(values):raise ValueError('Duplicate contract identity or value')


def _enum(value,allowed):
    if type(value) is not str or value not in allowed:raise ValueError('Unsupported contract choice')
    return value


def validate_context(context):
    """Validate and detach bounded evidence; never promote role hypotheses."""
    _shape(context,('version','case_id','source','request','events','harmonic_hypotheses','missing_facts','capabilities','observations','desired_output'))
    if context['version']!=VERSION:raise ValueError('Unsupported arranger contract version')
    _text(context['case_id']);_text(context['observations'],2048,True);_text(context['desired_output'],2048,True)
    src=context['source'];_shape(src,('source_id','sample_rate','origin_sample','end_sample','beats'))
    _text(src['source_id']);rate=_int(src['sample_rate'],8000,192000)
    origin=_int(src['origin_sample'],0,2**53-1);end=_int(src['end_sample'],origin+1,min(2**53-1,origin+rate*120))
    beats=_list(src['beats'],256)
    for sample in beats:_int(sample,origin,end)
    if beats!=sorted(set(beats)):raise ValueError('Beat evidence must be ordered without duplicates')
    request=context['request'];_shape(request,('mode','performers','drums'))
    mode=_enum(request['mode'],{'acoustic','electric','bass','ensemble'})
    count=_int(request['performers'],1,3)
    if (mode=='ensemble' and count!=3) or (mode!='ensemble' and count not in (1,2)):raise ValueError('Wrong pitched performer count')
    if type(request['drums']) is not bool or (mode=='acoustic' and request['drums']):raise ValueError('Acoustic mode cannot contain drums')
    events=_list(context['events'],96);ids=[]
    for e in events:
        _shape(e,('id','start','end','pitch','velocity','role_hint','essential','phrase'),('confidence','contour'))
        ids.append(_text(e['id']));a=_int(e['start'],origin,end-1);_int(e['end'],a+1,end)
        _int(e['pitch'],0,127);_int(e['velocity'],1,127);_enum(e['role_hint'],ROLES|{'unknown'});_text(e['phrase'])
        if type(e['essential']) is not bool:raise ValueError('Essential evidence flag must be boolean')
        confidence=e.get('confidence')
        if confidence is not None and (type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1):raise ValueError('Invalid source confidence')
        previous=a
        for point in _list(e.get('contour',[]),1024):
            _shape(point,('sample','midi'));previous=_int(point['sample'],previous,e['end'])
            if type(point['midi']) not in (int,float) or not math.isfinite(point['midi']) or not 0<=point['midi']<=127:raise ValueError('Invalid source contour')
    _unique(ids)
    hypotheses=_list(context['harmonic_hypotheses'],32);hyp_ids=[]
    for h in hypotheses:
        _shape(h,('id','start','end','pitch_classes','evidence_ids','status'))
        hyp_ids.append(_text(h['id']));a=_int(h['start'],origin,end-1);_int(h['end'],a+1,end)
        _enum(h['status'],{'alternative'});pcs=_list(h['pitch_classes'],12)
        for p in pcs:_int(p,0,11)
        _unique(pcs);refs=_list(h['evidence_ids'],96)
        for identity in refs:
            _text(identity)
            if identity not in ids:raise ValueError('Unknown harmonic evidence ID')
        _unique(refs)
    _unique(hyp_ids);missing=[]
    for fact in _list(context['missing_facts'],32):
        _shape(fact,('id','description','blocking'));missing.append(_text(fact['id']));_text(fact['description'],1024)
        if type(fact['blocking']) is not bool:raise ValueError('Blocking evidence flag must be boolean')
    _unique(missing)
    caps=context['capabilities'];_shape(caps,('virtual_only','voices'))
    if caps['virtual_only'] is not True:raise ValueError('Initial contract accepts virtual benchmark capabilities only')
    voices=_list(caps['voices'],4);voice_ids=[]
    for v in voices:
        _shape(v,('id','instrument','role','strings','max_polyphony','max_fret_span','articulations'),('register','pitches','max_fret','velocity_range'))
        voice_ids.append(_text(v['id']));instrument=_enum(v['instrument'],INSTRUMENTS);_enum(v['role'],ROLES)
        if (instrument=='drums')!=(v['role']=='drums'):raise ValueError('Drum capability role mismatch')
        poly=_int(v['max_polyphony'],1,8);_int(v['max_fret_span'],0,12);maxfret=_int(v.get('max_fret',24),0,36)
        arts=_list(v['articulations'],16)
        if not arts:raise ValueError('No supported articulation')
        for art in arts:_text(art,80)
        _unique(arts);strings=_list(v['strings'],8);string_ids=[];coverage=set()
        for string in strings:
            _shape(string,('id','open_midi','pitches'));string_ids.append(_int(string['id'],0,7));opened=_int(string['open_midi'],0,127)
            pitches=_list(string['pitches'],128)
            if not pitches:raise ValueError('Empty string coverage')
            for pitch in pitches:_int(pitch,opened,min(127,opened+maxfret))
            _unique(pitches);coverage.update(pitches)
        _unique(string_ids)
        if instrument=='drums':
            if strings or not v.get('pitches'):raise ValueError('Drums require flat pitch coverage and no strings')
        elif not strings or poly>len(strings):raise ValueError('Pitched voice needs sufficient strings')
        if 'pitches' in v:
            pitches=_list(v['pitches'],128)
            for pitch in pitches:_int(pitch,0,127)
            _unique(pitches)
            if instrument!='drums' and set(pitches)!=coverage:raise ValueError('Voice/string coverage mismatch')
        for field,lo,hi in [('register',0,127),('velocity_range',1,127)]:
            if field in v:
                pair=_list(v[field],2)
                if len(pair)!=2:raise ValueError('Expected range pair')
                _int(pair[0],lo,hi);_int(pair[1],pair[0],hi)
    _unique(voice_ids)
    pitched=[v for v in voices if v['instrument']!='drums'];drums=[v for v in voices if v['instrument']=='drums']
    if len(pitched)!=count or len(drums)!=int(request['drums']):raise ValueError('Capability voice count does not match mode')
    expected={'acoustic':'acoustic','electric':'electric','bass':'jazz-bass'}
    if mode=='ensemble':
        if sorted(v['instrument'] for v in pitched)!=['acoustic','electric','jazz-bass']:raise ValueError('Ensemble requires one of each pitched instrument')
    elif any(v['instrument']!=expected[mode] for v in pitched):raise ValueError('Instrument does not match mode')
    _unique([v['role'] for v in pitched])
    if mode=='bass' and count==2 and {v['role'] for v in pitched}!={'melody','bass'}:raise ValueError('Bass duo needs upper melody and foundation roles')
    return deepcopy(context)


def validate_proposal(context,proposal):
    """Resolve only approved source IDs. Raise on any invalid plan; no repair.

    Deterministic greedy string placement is conservative, not a completeness
    proof. Optional supplied string/fret pairs undergo the same constraints.
    """
    c=validate_context(context)
    _shape(proposal,('version','status','assignments','omissions','abstention'))
    if proposal['version']!=VERSION:raise ValueError('Unsupported proposal version')
    status=_enum(proposal['status'],{'plan','abstain'});_text(proposal['abstention'],2048,status=='plan')
    assignments=_list(proposal['assignments'],96);omissions=_list(proposal['omissions'],96)
    result={'version':VERSION,'status':status,'executable':False,'virtual_only':True,
            'context_sha256':hashlib.sha256(json.dumps(c,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
            'source':deepcopy(c['source']),'request':deepcopy(c['request']),'evidence':c,
            'notes':[],'adaptations':[],'issues':[],
            'semantics':'Source roles and harmonic alternatives remain hypotheses; text is untrusted data, not executable instructions.'}
    if status=='abstain':
        if assignments or omissions:raise ValueError('Abstention cannot contain a partial plan')
        result['issues']=[{'kind':'abstention','reason':proposal['abstention']}]
        return result
    if proposal['abstention'] or any(f['blocking'] for f in c['missing_facts']):raise ValueError('Blocking missing evidence requires abstention')
    events={e['id']:e for e in c['events']};voices={v['id']:v for v in c['capabilities']['voices']};used=set();scheduled=[]
    for a in assignments:
        _shape(a,('note_id','voice_id','octave','articulation'),('string','fret'))
        identity=_text(a['note_id']);voice_id=_text(a['voice_id'])
        if identity not in events or identity in used or voice_id not in voices:raise ValueError('Unknown or duplicate assigned source/voice ID')
        used.add(identity);e=events[identity];v=voices[voice_id];octave=_int(a['octave'],-2,2);pitch=e['pitch']+12*octave
        _text(a['articulation'],80)
        if a['articulation'] not in v['articulations']:raise ValueError('Unsupported articulation')
        if (e['role_hint']=='drums')!=(v['role']=='drums') or (v['role']=='drums' and octave):raise ValueError('Cannot invent percussion or pitch a drum event')
        if e['essential'] and e['role_hint']=='melody' and v['role']!='melody':raise ValueError('Essential melody cannot be reassigned to accompaniment')
        if not v.get('register',[0,127])[0]<=pitch<=v.get('register',[0,127])[1]:raise ValueError('Assigned pitch outside voice register')
        if not v.get('velocity_range',[1,127])[0]<=e['velocity']<=v.get('velocity_range',[1,127])[1]:raise ValueError('Unsupported source velocity; no silent scaling')
        if ('string' in a)!=('fret' in a):raise ValueError('String/fret must be supplied together')
        scheduled.append((e,v,a,pitch))
    for omission in omissions:
        _shape(omission,('note_id','reason'));identity=_text(omission['note_id']);_text(omission['reason'],1024)
        if identity not in events or identity in used:raise ValueError('Unknown or duplicate omitted ID')
        if events[identity]['essential']:raise ValueError('Cannot omit essential source evidence; abstain instead')
        used.add(identity);result['adaptations'].append({'kind':'omission',**deepcopy(omission)})
    if used!=set(events):raise ValueError('Every source event needs an assignment or explicit omission')
    if {v['id'] for _,v,_,_ in scheduled}!=set(voices):raise ValueError('Every requested voice needs source-grounded material; otherwise abstain')
    # Reserve upper melody first so the bass foundation can be checked even if
    # it begins earlier. Other voices are independent physical performers.
    scheduled.sort(key=lambda item:(item[1]['role']!='melody',item[1]['id'],item[0]['start'],item[0]['end'],item[0]['id']))
    for e,v,a,pitch in scheduled:
        same=[n for n in result['notes'] if n['voice_id']==v['id']]
        active=[n for n in same if n['start']<e['end'] and n['end']>e['start']]
        if len(active)>=v['max_polyphony']:raise ValueError('Voice polyphony exceeded')
        if c['request']['mode']=='bass' and c['request']['performers']==2 and v['role']=='bass':
            if any(n['role']=='melody' and n['start']<e['end'] and n['end']>e['start'] and pitch>=n['pitch'] for n in result['notes']):raise ValueError('Bass foundation must remain below overlapping melody')
        placement={}
        if v['instrument']=='drums':
            if 'string' in a or pitch not in v['pitches']:raise ValueError('Unsupported drum placement')
        else:
            candidates=[]
            previous=max((n for n in same if n['end']<=e['start'] and n['fret']>0),key=lambda n:(n['end'],n['id']),default=None)
            for string in v['strings']:
                fret=pitch-string['open_midi']
                if pitch not in string['pitches'] or any(n['string']==string['id'] for n in active):continue
                if 'string' in a:
                    _int(a['string'],0,7);_int(a['fret'],0,v.get('max_fret',24))
                    if (a['string'],a['fret'])!=(string['id'],fret):continue
                frets=[n['fret'] for n in active if n['fret']>0]+([fret] if fret else [])
                if frets and max(frets)-min(frets)>v['max_fret_span']:continue
                if fret and previous and abs(fret-previous['fret'])>4+int((e['start']-previous['end'])/c['source']['sample_rate']*12):continue
                candidates.append((abs(fret-previous['fret']) if previous else fret,fret,string['id']))
            if not candidates:raise ValueError('No supported string/fret, occupancy, span or movement placement')
            _,fret,string=min(candidates);placement={'string':string,'fret':fret}
        n=deepcopy(e);n.pop('role_hint');n.update(voice_id=v['id'],instrument=v['instrument'],role=v['role'],source_role_hint=e['role_hint'],
            source_pitch=e['pitch'],pitch=pitch,octave=a['octave'],articulation=a['articulation'],**placement)
        result['notes'].append(n)
        result['adaptations'].append({'kind':'role-assignment','note_id':e['id'],'voice_id':v['id'],'role':v['role'],'source_role_hint':e['role_hint']})
        if a['octave']:result['adaptations'].append({'kind':'octave','note_id':e['id'],'semitones':a['octave']*12})
        if e.get('contour'):result['adaptations'].append({'kind':'contour-not-rendered','note_id':e['id'],'reason':'Preserved source evidence; initial proposal only selects discrete source pitches'})
    streams=[]
    # Drum MIDI keys name kit pieces, not sounding pitches. Equal numeric keys
    # and gates across those domains do not constitute doubled pitched parts.
    for voice in voices:
        if voices[voice]['instrument']=='drums':continue
        stream=sorted((n['start'],n['end'],n['pitch']) for n in result['notes'] if n['voice_id']==voice)
        if stream in streams:raise ValueError('Duplicate identical performer streams')
        streams.append(stream)
    result['notes'].sort(key=lambda n:(n['start'],n['voice_id'],n['id']))
    return result
