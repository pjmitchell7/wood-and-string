"""Research-only source-evidence event segmentation. No gap filling or grid."""
import math
from statistics import median
from .performance import Note
from dataclasses import replace


def segment_with_attack_association(report, *, lookback_hops=8, fixed_velocity=80):
    """Optional monophonic comparison candidate; never a production default.

    RMS attacks can precede accepted pYIN pitch. Associate the nearest preceding
    attack only across unvoiced frames, with three stable following pitch frames.
    This is evidence association, NOT recovered pitch during the unvoiced span.
    Existing ends and contour points are untouched. Default fixed velocity80
    isolates segmentation from the unvalidated RMS-to-sample-velocity mapping.
    A resumed same pitch after any gap remains explicitly ambiguous.
    """
    if type(lookback_hops) is not int or not 1 <= lookback_hops <= 8:
        raise ValueError('Attack association is bounded to 1..8 hops')
    if type(fixed_velocity) is not int or not 1<=fixed_velocity<=127:
        raise ValueError('Specify a fixed diagnostic velocity')
    notes,audit=segment(report)
    rows=report['frames'];hop=report['hop_samples']
    origin=report.get('analysis_offset_samples',0)
    result=[];used=set()
    for note,event in zip(notes,audit['events']):
        indexes=event['raw_frame_indexes'];first=indexes[0]
        prior_end=result[-1].end if result else origin
        event['original_start']=note.start
        event['attack_association']=None
        event['onset_status']='pitch-frame boundary; attack not independently associated'
        event['energy_mapped_velocity_candidate']=note.velocity
        event['velocity']=fixed_velocity
        note=replace(note,velocity=fixed_velocity)
        stable=(len(indexes)>=3 and all(round(rows[j]['pitch'])==note.pitch for j in indexes[:3]))
        candidates=[]
        if stable:
            for j in range(max(0,first-lookback_hops),first):
                row=rows[j]
                start=max(origin,row['sample']-hop//2)
                if (j not in used and row.get('attack',False) and prior_end<=start<note.start
                        and rows[first]['sample']-row['sample']<=lookback_hops*hop
                        and all(rows[k]['pitch'] is None for k in range(j,first))
                        and all(rows[k]['rms']>=max(1e-12,row['rms']*.1) for k in range(j,first+1))
                        and all(rows[k+1]['sample']-rows[k]['sample']==hop for k in range(j,first))):
                    candidates.append((j,start))
        if candidates:
            j,start=candidates[-1];used.add(j)
            note=replace(note,start=start)
            event['start']=start
            event['attack_association']={'attack_frame_index':j,'attack_sample':rows[j]['sample'],
                'start_shift_samples':start-event['original_start'],
                'pitch_unobserved_interval':[start,event['original_start']],
                'evidence':'RMS-rise candidate precedes stable pitch; pitch association uncertain'}
            event['onset_status']='associated waveform rise; not certified physical pluck'
        if result and result[-1].pitch==note.pitch and result[-1].end<note.start:
            event['resume_uncertainty']='Same pitch resumes after gap: rest, recording dropout or estimator failure unresolved; gap preserved'
        result.append(note)
    audit['settings']['attack_association']={'lookback_hops':lookback_hops,
        'minimum_stable_pitch_frames':3,'energy_continuity_fraction_of_attack':.1,
        'end_changes':False,'fixed_velocity':fixed_velocity}
    audit['quality_status']='explicit comparison only; early pitch association and resumed-note intent uncertain'
    return result,audit


def energy_evidence(samples, frames, hop_samples):
    """Windowed RMS from the separated estimate, in source sample coordinates.

    Energy rises are attack candidates, not calibrated onset probabilities.
    No source references or synthetic humanization enter this measurement.
    """
    result = []
    half = max(1, hop_samples//2)
    for row in frames:
        center = row['sample']
        chunk = samples[max(0, center-half):min(len(samples), center+half)]
        rms = math.sqrt(sum(float(x)*float(x) for x in chunk)/len(chunk)) if len(chunk) else 0.
        result.append({**row, 'rms': rms})
    peak = max((r['rms'] for r in result), default=0.)
    last_attack = -10**12
    for i, row in enumerate(result):
        history = [r['rms'] for r in result[max(0, i-3):i]]
        prior = median(history) if history else row['rms']
        rise = row['rms']/max(prior, 1e-12)
        attack = (rise >= 1.8 and row['rms']-prior >= peak*.12
                  and row['sample']-last_attack >= hop_samples*8)
        row.update(attack=attack, attack_rise_ratio=rise,
                   attack_evidence='RMS rise >=1.8x and >=12% role peak; 8-hop refractory')
        if attack: last_attack = row['sample']
    return result


def segment(report, *, hysteresis=.7, confirmation_frames=2):
    """Return notes plus complete frame membership and boundary explanations.

    Pitch events require persistent evidence, but an attack can rearticulate an
    unchanged pitch. Never extends across an unvoiced frame. Defaults unvalidated
    musically: preserve settings and raw frames with every research result.
    """
    rows = report['frames']; hop = report['hop_samples']; total = report['input']['frames']
    if confirmation_frames < 1 or not .5 <= hysteresis < 1:
        raise ValueError('Invalid research segmentation settings')
    if any(rows[i]['sample'] >= rows[i+1]['sample'] for i in range(len(rows)-1)):
        raise ValueError('Frames must be source ordered')
    for row in rows:
        if not math.isfinite(row['rms']) or row['rms'] < 0:
            raise ValueError('Invalid measured energy')
        if row['pitch'] is not None and not math.isfinite(row['pitch']):
            raise ValueError('Invalid measured pitch')
    groups = []; decisions = []; current = []; anchor = None
    def finish(reason):
        nonlocal current
        if current:
            groups.append((anchor, current, reason)); current = []
    i = 0
    while i < len(rows):
        row = rows[i]; pitch = row['pitch']
        if pitch is None:
            finish('unvoiced boundary; no gap filled'); anchor = None; i += 1; continue
        if not current:
            anchor = round(pitch); current = [i]; i += 1; continue
        if row['sample']-rows[current[-1]]['sample'] != hop:
            finish('missing frame interval; no gap filled'); anchor = round(pitch)
        candidate = round(pitch)
        persistent = (abs(pitch-anchor) >= hysteresis and candidate != anchor
            and i+confirmation_frames <= len(rows)
            and all(r['pitch'] is not None and round(r['pitch']) == candidate
                    and abs(r['pitch']-anchor) >= hysteresis
                    and r['sample'] == row['sample']+j*hop
                    for j, r in enumerate(rows[i:i+confirmation_frames])))
        if persistent:
            finish('persistent pitch change')
            decisions.append({'sample': row['sample'], 'kind': 'pitch_boundary',
                'from': anchor, 'to': candidate, 'confirmation_frames': confirmation_frames,
                'boundary_policy': 'first candidate frame; no confirmation-latency shift'})
            anchor = candidate
        elif row.get('attack', False) and len(current) >= 2:
            finish('measured energy attack')
            decisions.append({'sample': row['sample'], 'kind': 'reattack', 'pitch': anchor,
                              'rise_ratio': row.get('attack_rise_ratio')})
        elif candidate != anchor:
            decisions.append({'sample': row['sample'], 'kind': 'pitch_excursion_retained_in_event',
                'raw_pitch': pitch, 'event_pitch': anchor,
                'reason': 'inside hysteresis or lacks persistent confirming frames'})
        current.append(i); i += 1
    finish('end of available voiced frames')
    energies = [math.sqrt(sum(rows[i]['rms']**2 for i in indexes)/len(indexes))
                for _, indexes, _ in groups]
    reference = sorted(energies)[min(len(energies)-1, int(.9*len(energies)))] if energies else 0
    notes = []; audit = []
    for number, ((pitch, indexes, reason), rms) in enumerate(zip(groups, energies)):
        group = [rows[i] for i in indexes]
        relative_db = 20*math.log10(max(rms, 1e-12)/max(reference, 1e-12))
        velocity = max(48, min(105, round(85+1.5*relative_db)))
        start = max(0, group[0]['sample']-hop//2)
        end = min(total, group[-1]['sample']+hop//2)
        identity = f"{report['role']}-phrased-{number}"
        notes.append(Note(identity, start, end, pitch, velocity, report['role'],
            sum(r['confidence'] for r in group)/len(group), 'source-evidence',
            tuple((r['sample'], r['pitch']) for r in group)))
        audit.append({'note_id': identity, 'raw_frame_indexes': indexes, 'start': start, 'end': end,
            'end_reason': reason, 'rms': rms, 'relative_db': relative_db, 'velocity': velocity,
            'pitch': pitch, 'timing': 'frame center +/- half hop; no beat quantization',
            'contour_policy': 'raw contour retained; integer sample renderer cannot reproduce slides'})
    return notes, {'events': audit, 'decisions': decisions,
        'unvoiced_frame_indexes': [i for i,r in enumerate(rows) if r['pitch'] is None],
        'settings': {'hysteresis_semitones': hysteresis, 'confirmation_frames': confirmation_frames,
                     'velocity': '85 + 1.5 * dB relative to role event RMS 90th percentile, clipped 48..105'},
        'quality_status': 'research candidate; listening acceptance not established'}
