"""Post-proposal symbolic explanations; never model evidence or a music score.

The frozen evaluator remains authoritative. These records expose its context
and the consequences of a proposal without adding or weighting criteria.
"""
from copy import deepcopy
import hashlib

from .arranger_contract import validate_context, validate_proposal
from .arranger_evaluation import evaluate_case
from .arranger_prompt import canonical

VERSION='arranger-descriptive-diagnostics-v1'


def diagnose(case,proposal):
    """Describe accepted source/assignment facts on the original sample clock.

    Roles/confidence and desired-output text are supplied symbolic evidence,
    not recovered audio truth. Malformed proposals remain contract failures;
    no repair, generation, rendering or listening takes place here.
    """
    evaluation=evaluate_case(case,proposal)
    context=validate_context(case['input']);source=context['source']
    clock={key:source[key] for key in ('sample_rate','origin_sample','end_sample')}
    window={'start':clock['origin_sample'],'end':clock['end_sample']}
    report={'version':VERSION,'case_id':context['case_id'],'source_clock':clock,
        'context_sha256':hashlib.sha256(canonical(context)).hexdigest(),
        'proposal_sha256':hashlib.sha256(canonical(proposal)).hexdigest(),
        'evaluation':evaluation,'note_trace':[],'findings':[],
        'reason_text':{'abstention':None,'omissions':[],'quality_assessed':False},
        'scope':'Virtual symbolic proposal only. Existing expectation criteria are unchanged; no composite grade, audio recognition, audibility or musical acceptance.',
        'uncertainty':['Source roles, confidence and harmonic alternatives remain supplied evidence, not independently verified audio facts.',
                       'A supported symbolic articulation name does not establish how a rendered instrument sounds.']}
    findings=report['findings']
    def finding(kind,expected,observed,unit,check,*,interval=None,delta=None):
        findings.append({'kind':kind,'stage':'symbolic_arrangement','window':deepcopy(interval or window),
            'unit':unit,'expected':deepcopy(expected),'observed':deepcopy(observed),
            'difference':deepcopy(delta),'next_discriminating_check':check})
    if evaluation['hard_violation'] is not None:
        finding('contract_rejection',{'valid_source_grounded_proposal':True},
                {'validation_error':evaluation['hard_violation']},'contract outcome',
                'Inspect the unchanged whole response against the named source IDs and declared voice capabilities. Preserve the rejected response; do not repair it silently.')
        return report
    resolved=validate_proposal(context,proposal);notes={note['id']:note for note in resolved['notes']}
    omissions={row['note_id']:row['reason'] for row in proposal['omissions']}
    protected=set(case['expectations']['protected_ids'])
    for event in sorted(context['events'],key=lambda row:(row['start'],row['end'],row['id'])):
        assigned=notes.get(event['id'])
        row={'note_id':event['id'],'window':{'start':event['start'],'end':event['end']},
             'source_pitch_midi':event['pitch'],'source_velocity_midi':event['velocity'],
             'source_role_hint':event['role_hint'],'source_essential':event['essential'],
             'source_confidence_metadata':event.get('confidence'),'source_phrase':event['phrase'],
             'source_contour_points':len(event.get('contour',[])),
             'protected_by_case_expectation':event['id'] in protected,
             'outcome':'assigned' if assigned else 'explicit_omission' if event['id'] in omissions else 'no_plan',
             'assignment':None,'omission_reason':omissions.get(event['id'])}
        if assigned:
            row['assignment']={key:deepcopy(assigned[key]) for key in
                ('voice_id','instrument','role','start','end','pitch','velocity','articulation','octave','string','fret') if key in assigned}
            row['pitch_change_semitones']=assigned['pitch']-event['pitch']
            row['gate_change_samples']={'start':assigned['start']-event['start'],'end':assigned['end']-event['end']}
        report['note_trace'].append(row)
    report['reason_text'].update(abstention=proposal['abstention'],omissions=deepcopy(proposal['omissions']))
    finding('proposal_status',{'status':evaluation['required_status']},{'status':resolved['status']},'plan status',
            'Compare the stated missing facts and voice constraints with the retained response before choosing a separately versioned change.')
    missing=sorted(protected-set(notes))
    finding('protected_event_retention',{'retained_ids':sorted(protected)},
            {'retained_ids':sorted(protected&set(notes)),'missing_ids':missing},'source event IDs',
            'Inspect each missing event in note_trace. Whole-plan abstention is different from an explicit optional-note omission.',
            delta={'retained_minus_expected_events':len(protected&set(notes))-len(protected)})
    for identity in case['expectations']['required_omissions']:
        event=next(event for event in context['events'] if event['id']==identity)
        finding('required_omission',{'note_id':identity,'explicitly_omitted':True},
                {'note_id':identity,'explicitly_omitted':identity in omissions,'assignment':deepcopy(notes.get(identity)),
                 'whole_plan_abstained':resolved['status']=='abstain'},'source event disposition',
                'Compare the optional event and intended part with the actual assignment or stated omission reason.',
                interval={'start':event['start'],'end':event['end']})
    for allowed in case['expectations']['allowed_assignments']:
        note=notes.get(allowed['note_id'])
        if note is None:continue
        actual={key:note[key] for key in ('voice_id','octave','articulation')}
        matches=all(actual[key] in allowed[plural] for key,plural in
                    (('voice_id','voice_ids'),('octave','octaves'),('articulation','articulations')))
        if not matches:
            finding('assignment_alternative',allowed,{'note_id':note['id'],**actual},'voice ID, octaves and articulation name',
                'Check the source role and exact permitted alternatives before changing the musical interpretation.',
                interval={'start':note['start'],'end':note['end']})
    for rest in case['expectations']['required_rest_windows']:
        a,b=rest['start'],rest['end']
        if type(a) is not int or type(b) is not int or not clock['origin_sample']<=a<b<=clock['end_sample']:
            raise ValueError('Expected rest window outside source clock')
        overlaps=[{'note_id':note['id'],'start':max(a,note['start']),'end':min(b,note['end']),
                   'overlap_samples':min(b,note['end'])-max(a,note['start'])}
                  for note in notes.values() if note['voice_id']==rest['voice_id'] and note['start']<b and note['end']>a]
        finding('required_rest',{'voice_id':rest['voice_id'],'assigned_events_overlapping':0},
            {'voice_id':rest['voice_id'],'overlapping_events':overlaps},'absolute source samples; assigned event count',
            'Inspect whether the assignment crosses an intended rest. This is command occupancy, not audible ringing or release.',
            interval={'start':a,'end':b},delta={'overlapping_event_count':len(overlaps)})
    for relation in case['expectations']['foundation_below']:
        crossings=[{'lower_note_id':lo['id'],'upper_note_id':hi['id'],'lower_midi':lo['pitch'],'upper_midi':hi['pitch'],
                    'start':max(lo['start'],hi['start']),'end':min(lo['end'],hi['end'])}
                   for lo in notes.values() if lo['voice_id']==relation['lower_voice_id']
                   for hi in notes.values() if hi['voice_id']==relation['upper_voice_id']
                   and lo['pitch']>=hi['pitch'] and lo['start']<hi['end'] and lo['end']>hi['start']]
        finding('foundation_register',relation,{'crossing_pairs':crossings},'MIDI pitch and absolute source samples',
            'Inspect each overlapping pair and any octave choices; numeric register order alone does not judge musical balance.')
    for group in case['expectations']['distinct_voice_groups']:
        streams={voice:sorted((note['start'],note['end'],note['pitch']) for note in notes.values() if note['voice_id']==voice) for voice in group}
        empty=[voice for voice,stream in streams.items() if not stream]
        duplicates=[[a,b] for index,a in enumerate(group) for b in group[index+1:] if streams[a] and streams[a]==streams[b]]
        finding('distinct_voice_material',{'voice_ids':group,'empty_voices':[],'identical_command_pairs':[]},
            {'empty_voices':empty,'identical_command_pairs':duplicates},'voice IDs and exact pitch/gate commands',
            'Inspect the intended roles and retained commands; different command lists do not establish perceptually distinct or complementary parts.')
    return report
