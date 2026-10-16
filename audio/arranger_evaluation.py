"""Symbolic case diagnostics; expectations never enter model evidence/prompts."""
from copy import deepcopy

from .arranger_contract import validate_context, validate_proposal


def evaluate_case(case, proposal):
    if type(case) is not dict or set(case)!={'input','expectations'}:
        raise ValueError('Expected separate input and evaluation expectations')
    c=validate_context(case['input']);expected=case['expectations']
    required={'required_status','protected_ids','required_omissions','allowed_assignments',
              'required_rest_windows','distinct_voice_groups','foundation_below',
              'preserve_all_source_gates','preserve_source_velocity','preserve_harmonic_alternatives','diagnostics'}
    if type(expected) is not dict or set(expected)!=required:
        raise ValueError('Unexpected benchmark expectation fields')
    events={e['id']:e for e in c['events']};voices={v['id']:v for v in c['capabilities']['voices']}
    if expected['required_status'] not in ('plan','abstain'):
        raise ValueError('Invalid expected status')
    for field in ('protected_ids','required_omissions'):
        values=expected[field]
        if type(values) is not list or len(set(values))!=len(values) or not set(values)<=set(events):
            raise ValueError('Invalid expected source IDs')
    result={'case_id':c['case_id'],'required_status':expected['required_status'],
            'status':proposal.get('status') if type(proposal) is dict else None,
            'hard_violation':None,'expectation_failures':[],
            'protected_total':len(expected['protected_ids']),'protected_retained':0,
            'assigned_events':0,'explicit_omissions':[],'octave_changes':[],
            'manual_text_review_required':False,'music_quality_assessed':False}
    try:resolved=validate_proposal(c,proposal)
    except ValueError as error:
        result['hard_violation']=str(error)
        return result
    failures=result['expectation_failures']
    if resolved['status']!=expected['required_status']:failures.append('Unexpected plan/abstention status')
    notes={n['id']:n for n in resolved['notes']}
    result['protected_retained']=len(set(expected['protected_ids'])&set(notes))
    result['assigned_events']=len(notes)
    result['explicit_omissions']=deepcopy(proposal['omissions'])
    result['octave_changes']=[dict(note_id=n['id'],semitones=12*n['octave']) for n in notes.values() if n['octave']]
    result['manual_text_review_required']=bool(proposal['abstention'] or proposal['omissions'])
    if resolved['status']=='abstain':return result
    if set(expected['protected_ids'])-set(notes):failures.append('Protected source IDs were not retained')
    omitted={o['note_id'] for o in proposal['omissions']}
    if set(expected['required_omissions'])-omitted:failures.append('Required explicit omissions missing')
    for allowed in expected['allowed_assignments']:
        if type(allowed) is not dict or set(allowed)!={'note_id','voice_ids','octaves','articulations'} or allowed['note_id'] not in events:
            raise ValueError('Invalid allowed assignment expectation')
        n=notes.get(allowed['note_id'])
        if n and (n['voice_id'] not in allowed['voice_ids'] or n['octave'] not in allowed['octaves'] or n['articulation'] not in allowed['articulations']):
            failures.append('Assignment outside expected alternatives: '+n['id'])
    for rest in expected['required_rest_windows']:
        if type(rest) is not dict or set(rest)!={'voice_id','start','end'} or rest['voice_id'] not in voices:
            raise ValueError('Invalid expected rest voice')
        if any(n['voice_id']==rest['voice_id'] and n['start']<rest['end'] and n['end']>rest['start'] for n in notes.values()):
            failures.append('Source gate occupies required rest: '+rest['voice_id'])
    for group in expected['distinct_voice_groups']:
        if type(group) is not list or len(group)<2 or not set(group)<=set(voices):raise ValueError('Invalid distinct voice group')
        streams=[]
        for voice_id in group:
            stream=sorted((n['start'],n['end'],n['pitch']) for n in notes.values() if n['voice_id']==voice_id)
            if not stream or stream in streams:failures.append('Empty or duplicated expected voice: '+voice_id)
            streams.append(stream)
    for relation in expected['foundation_below']:
        if type(relation) is not dict or set(relation)!={'lower_voice_id','upper_voice_id'} or not set(relation.values())<=set(voices):
            raise ValueError('Invalid expected register relation')
        if any(lo['pitch']>=hi['pitch'] and lo['start']<hi['end'] and lo['end']>hi['start']
               for lo in notes.values() if lo['voice_id']==relation['lower_voice_id']
               for hi in notes.values() if hi['voice_id']==relation['upper_voice_id']):
            failures.append('Foundation crosses upper voice')
    if expected['preserve_all_source_gates'] and any((n['start'],n['end'])!=(events[n['id']]['start'],events[n['id']]['end']) for n in notes.values()):
        failures.append('Source gates changed')
    if expected['preserve_source_velocity'] and any(n['velocity']!=events[n['id']]['velocity'] for n in notes.values()):
        failures.append('Source velocity changed')
    if expected['preserve_harmonic_alternatives'] and resolved['evidence']['harmonic_hypotheses']!=c['harmonic_hypotheses']:
        failures.append('Harmonic alternatives changed')
    return result
