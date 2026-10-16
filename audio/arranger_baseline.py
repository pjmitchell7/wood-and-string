"""Bounded symbolic baseline, not an audio analyzer or musical-quality judge.

Beam width 32, at most 50,000 expanded successors. Exhaustion is reported as
search uncertainty, never proof that no playable arrangement exists. Source
events are the only note inventory. No benchmark expectations are consumed.
"""
from .arranger_contract import VERSION, validate_context, validate_proposal

BEAM_WIDTH = 32
MAX_EXPANSIONS = 50000
OCTAVES = (0, -1, 1, -2, 2)


def _abstain(context, reason):
    p = dict(version=VERSION, status='abstain', assignments=[], omissions=[], abstention=reason)
    validate_proposal(context, p)
    return p


def _overlap(a, b):
    return a['start'] < b['end'] and a['end'] > b['start']


def _physical(context, assignments, events, voices):
    """Conservative partial feasibility, with final contract validation separate."""
    notes = []
    for a in assignments:
        e = events[a['note_id']]; v = voices[a['voice_id']]
        notes.append(dict(e, voice_id=v['id'], role=v['role'], instrument=v['instrument'],
                          pitch=e['pitch']+12*a['octave'],
                          string=a.get('string'), fret=a.get('fret')))
    for v in voices.values():
        prior = []
        for n in sorted((n for n in notes if n['voice_id']==v['id']), key=lambda n:(n['start'],n['end'],n['id'])):
            active = [p for p in prior if _overlap(n,p)]
            if len(active) >= v['max_polyphony']: return False
            if v['instrument'] != 'drums':
                if any(p['string']==n['string'] for p in active): return False
                frets = [p['fret'] for p in active+[n] if p['fret']>0]
                if frets and max(frets)-min(frets)>v['max_fret_span']: return False
                previous = max((p for p in prior if p['end']<=n['start'] and p['fret']>0),
                               key=lambda p:(p['end'],p['id']), default=None)
                if n['fret'] and previous and abs(n['fret']-previous['fret']) > 4+int((n['start']-previous['end'])/context['source']['sample_rate']*12):
                    return False
            prior.append(n)
    if context['request']['mode']=='bass' and context['request']['performers']==2:
        if any(b['pitch']>=m['pitch'] and _overlap(b,m) for b in notes if b['role']=='bass'
               for m in notes if m['role']=='melody'): return False
    return True


def _choices(e, voices):
    for v in sorted(voices.values(), key=lambda v:(v['role']!=e['role_hint'],v['id'])):
        if (e['role_hint']=='drums') != (v['role']=='drums'): continue
        if e['essential'] and e['role_hint']=='melody' and v['role']!='melody': continue
        if not v.get('velocity_range',[1,127])[0]<=e['velocity']<=v.get('velocity_range',[1,127])[1]: continue
        for octave in OCTAVES:
            pitch=e['pitch']+12*octave
            if not v.get('register',[0,127])[0]<=pitch<=v.get('register',[0,127])[1]: continue
            base=dict(note_id=e['id'],voice_id=v['id'],octave=octave,articulation=sorted(v['articulations'])[0])
            if v['instrument']=='drums':
                if octave==0 and pitch in v['pitches']: yield base
            else:
                for string in sorted(v['strings'],key=lambda s:(pitch-s['open_midi'],s['id'])):
                    if pitch in string['pitches']:
                        yield dict(base,string=string['id'],fret=pitch-string['open_midi'])


def make_baseline(context):
    """Return a strict validated proposal; never change supplied evidence.

    Ranking minimizes optional omissions, role mismatch, then octave distance;
    stable ties favor coverage of requested voices and lower frets. Search
    failure is an explicit abstention, not an invented fallback arrangement.
    """
    c=validate_context(context)
    if any(f['blocking'] for f in c['missing_facts']):
        return _abstain(c,'Blocking source evidence is unresolved; no notes were invented.')
    events={e['id']:e for e in c['events']};voices={v['id']:v for v in c['capabilities']['voices']}
    order=sorted(events.values(),key=lambda e:(not e['essential'],e['role_hint']!='melody',e['start'],e['end'],e['id']))
    # State: assignments, omissions, (omissions, role mismatches, octave cost).
    beam=[([],[],(0,0,0))];expansions=0
    for e in order:
        next_states=[];choices=list(_choices(e,voices))
        for assignments,omissions,cost in beam:
            for choice in choices:
                expansions+=1
                if expansions>MAX_EXPANSIONS:
                    return _abstain(c,'Bounded symbolic search exhausted its expansion budget; feasibility remains unresolved.')
                proposed=assignments+[choice]
                if _physical(c,proposed,events,voices):
                    mismatch=int(voices[choice['voice_id']]['role']!=e['role_hint'])
                    next_states.append((proposed,omissions,(cost[0],cost[1]+mismatch,cost[2]+abs(choice['octave']))))
            if not e['essential']:
                next_states.append((assignments,omissions+[dict(note_id=e['id'],reason='Bounded source-role and physical allocation omitted this optional event.')],
                                    (cost[0]+1,cost[1],cost[2])))
        if not next_states:
            return _abstain(c,'No retained search state can place essential evidence within the supplied capabilities; feasibility beyond this search is unknown.')
        def rank(state):
            aa,oo,cost=state
            return (*cost,-len({a['voice_id'] for a in aa}),sum(a.get('fret',0) for a in aa),
                    tuple((a['note_id'],a['voice_id'],OCTAVES.index(a['octave']),a.get('string',-1)) for a in aa),
                    tuple(o['note_id'] for o in oo))
        beam=sorted(next_states,key=rank)[:BEAM_WIDTH]
    for assignments,omissions,_ in beam:
        p=dict(version=VERSION,status='plan',assignments=assignments,omissions=omissions,abstention='')
        try: validate_proposal(c,p)
        except ValueError: continue
        return p
    return _abstain(c,'Retained candidates failed the final contract, including requested voice material or distinct streams; no valid plan is asserted.')
