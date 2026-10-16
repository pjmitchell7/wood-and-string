"""Bounded annotation comparison; no timing fit or production interpretation."""
import math


def _validate(rows, predictions):
    if type(rows) is not list or len(rows) > 1000:
        raise ValueError('Expected at most 1000 events')
    seen, result = set(), []
    for row in rows:
        if type(row) is not dict or not {'id','start','end','pitch'} <= row.keys():
            raise ValueError('Missing event fields')
        ident = row['id']
        if type(ident) is not str or not ident or len(ident) > 256 or ident in seen:
            raise ValueError('Invalid or duplicate event ID')
        seen.add(ident)
        if any(type(row[k]) is not int for k in ('start','end')) or not 0 <= row['start'] < row['end']:
            raise ValueError('Invalid event times')
        pitch = row['pitch']
        if (type(pitch) not in ((int,) if predictions else (int,float))
                or not math.isfinite(pitch) or not 0 <= pitch <= 127):
            raise ValueError('Invalid pitch')
        confidence = row.get('confidence')
        if confidence is not None and (type(confidence) not in (str,int,float)
                or (type(confidence) in (int,float) and not math.isfinite(confidence))):
            raise ValueError('Invalid reference confidence metadata')
        result.append({k:row[k] for k in ('id','start','end','pitch')} | {'confidence':confidence})
    return sorted(result, key=lambda r:(r['start'],r['end'],r['id']))


def _matching(predictions, references, onset_limit, full):
    edges = [[j for j,r in enumerate(references)
              if abs(p['pitch']-r['pitch']) <= .5 and abs(p['start']-r['start']) <= onset_limit
              and (not full or abs(p['end']-r['end']) <= max(onset_limit,(r['end']-r['start'])/5))]
             for p in predictions]
    # Alternating-path breadth-first augmentation: maximum cardinality,
    # deterministic traversal, deliberately no minimum-cost assertion.
    pm, rm = {}, {}
    for root in range(len(predictions)):
        queue, visited, parent, free = [root], {root}, {}, None
        for i in queue:
            for j in edges[i]:
                if j in parent: continue
                parent[j] = i
                if j not in rm:
                    free = j
                    break
                other = rm[j]
                if other not in visited:
                    visited.add(other); queue.append(other)
            if free is not None: break
        while free is not None:
            i = parent[free]
            previous = pm.get(i)
            pm[i], rm[free] = free, i
            free = previous
    pairs=[]
    for i,j in sorted(pm.items()):
        p,r=predictions[i],references[j]
        intersection=max(0,min(p['end'],r['end'])-max(p['start'],r['start']))
        union=max(p['end'],r['end'])-min(p['start'],r['start'])
        pairs.append({'event_id':p['id'],'reference_id':r['id'],
                      'reference_pitch':r['pitch'],'reference_confidence':r['confidence'],
                      'pitch_error_semitones':p['pitch']-r['pitch'],
                      'onset_error_samples':p['start']-r['start'],
                      'offset_error_samples':p['end']-r['end'],
                      'event_duration_samples':p['end']-p['start'],
                      'reference_duration_samples':r['end']-r['start'],
                      'duration_error_samples':(p['end']-p['start'])-(r['end']-r['start']),
                      'interval_iou':intersection/union})
    return {'matched':len(pm),'missed':len(references)-len(rm),
            'unmatched_to_reference':len(predictions)-len(pm),
            'reference_count':len(references),'event_count':len(predictions),'pairs':pairs,
            'missed_reference_ids':[r['id'] for j,r in enumerate(references) if j not in rm],
            'unmatched_event_ids':[p['id'] for i,p in enumerate(predictions) if i not in pm]}


def _occupancy(predictions, references, start, end):
    by_pitch={}
    for rows,side in ((predictions,0),(references,1)):
        for r in rows:
            a,b=max(start,r['start']),min(end,r['end'])
            if a>=b:continue
            pitch=math.floor(r['pitch']+.5)
            points=by_pitch.setdefault(pitch,{})
            points.setdefault(a,[0,0])[side]+=1
            points.setdefault(b,[0,0])[side]-=1
    records=[]
    keys=('event_union_samples','reference_union_samples','intersection_samples',
          'missing_samples','outside_annotation_samples','event_duplicate_overlap_samples',
          'reference_duplicate_overlap_samples')
    for pitch,points in sorted(by_pitch.items()):
        counts=[0,0]; previous=start; totals=dict.fromkeys(keys,0)
        for t,delta in sorted(points.items()):
            n=t-previous;p,r=counts
            conditions=(p>0,r>0,p>0 and r>0,p==0 and r>0,p>0 and r==0,p>1,r>1)
            for key,condition in zip(keys,conditions):
                if condition:totals[key]+=n
            counts=[counts[i]+delta[i] for i in (0,1)];previous=t
        records.append({'pitch_bin':pitch,**totals})
    return {'pitch_bin_policy':'nearest_semitone_half_up; distinct from <=0.5 matching',
            'interval_policy':'half_open_clipped_to_window', 'by_pitch':records,
            'totals_pitch_samples':{key:sum(r[key] for r in records) for key in keys}}


def score_events(events, references, *, start_sample, end_sample, sample_rate):
    """Compare annotations without assuming unmatched mixture notes are false.

    Left-censored references are occupancy-only. Right-censored references
    retain onset scoring but are excluded from full-note denominators. Their
    unmatched predictions are still reported, so full-note unmatched counts
    are not a false-positive statistic. Pair IoU uses original intervals;
    occupancy alone is clipped to the selected window.
    """
    if (any(type(x) is not int for x in (start_sample,end_sample,sample_rate))
            or not 0 <= start_sample < end_sample or not 8000 <= sample_rate <= 192000):
        raise ValueError('Invalid scoring window or rate')
    p,r=_validate(events,True),_validate(references,False)
    inside=lambda x:x['start']<end_sample and x['end']>start_sample
    active_p,active_r=[x for x in p if inside(x)],[x for x in r if inside(x)]
    onset_p=[x for x in active_p if x['start']>=start_sample]
    onset_r=[x for x in active_r if x['start']>=start_sample]
    full_r=[x for x in onset_r if x['end']<=end_sample]
    return {'version':1,'start_sample':start_sample,'end_sample':end_sample,'sample_rate':sample_rate,
            'matching':'maximum_cardinality_not_minimum_cost; no time fit',
            'pitch_tolerance_semitones':.5,
            'onset_tolerance_samples':{'20ms':sample_rate*.02,'50ms':sample_rate*.05},
            'full_note_offset_policy':'max(50ms,20percent_reference_duration)',
            'onset_20ms':_matching(onset_p,onset_r,sample_rate*.02,False),
            'onset_50ms':_matching(onset_p,onset_r,sample_rate*.05,False),
            'full_note_50ms':_matching(onset_p,full_r,sample_rate*.05,True),
            'left_censored_reference_ids':[x['id'] for x in active_r if x['start']<start_sample],
            'right_censored_reference_ids':[x['id'] for x in active_r if x['end']>end_sample],
            'left_censored_event_ids':[x['id'] for x in active_p if x['start']<start_sample],
            'outside_window_reference_ids':[x['id'] for x in r if not inside(x)],
            'outside_window_event_ids':[x['id'] for x in p if not inside(x)],
            'references':r,
            'occupancy':_occupancy(active_p,active_r,start_sample,end_sample)}
