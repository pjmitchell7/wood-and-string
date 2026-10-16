"""Versioned source-sample authority and conservative guitar assignment.

No beat quantization. Bounded phrase lookahead, not a fingering oracle.
"""
from dataclasses import dataclass, asdict, field
import math
import re
from pathlib import Path

OPEN = (40, 45, 50, 55, 59, 64)  # low to high
NAMES = ('LowE', 'A', 'D', 'G', 'B', 'HighE')

@dataclass(frozen=True)
class Note:
    id: str
    start: int
    end: int
    pitch: int
    velocity: int = 80
    role: str = 'melody'
    confidence: float = 1.0
    phrase: str = 'phrase-1'
    contour: tuple = ()  # (source sample, MIDI float), retained even if unrendered

@dataclass
class Performance:
    source_id: str
    sample_rate: int
    notes: list[Note]
    version: int = 1
    analysis_offset_samples: int = 0
    beats: list = field(default_factory=list)

    def validate(self):
        if self.version != 1 or not 8000 <= self.sample_rate <= 192000:
            raise ValueError('Unsupported performance version or sample rate')
        ids = set()
        for n in self.notes:
            if not n.id or n.id in ids: raise ValueError('Duplicate or empty note ID')
            ids.add(n.id)
            if any(type(x) is not int for x in (n.start, n.end, n.pitch, n.velocity)):
                raise ValueError('Sample times, pitch and velocity must be integers')
            if not (0 <= n.start < n.end and 0 <= n.pitch <= 127 and 1 <= n.velocity <= 127):
                raise ValueError('Invalid note range')
            if not math.isfinite(n.confidence) or not 0 <= n.confidence <= 1:
                raise ValueError('Invalid confidence')
            if n.role not in ('melody', 'bass', 'harmony'): raise ValueError('Invalid role')
            for sample, pitch in n.contour:
                if not n.start <= sample <= n.end or not math.isfinite(pitch):
                    raise ValueError('Invalid pitch contour')

def sampled_pitches():
    """Read actual SFZ coverage; its low-E range has a missing pitch."""
    text = (Path(__file__).parent/'provenance/meta-steel-guitar.sfz').read_text()
    result = {}
    for i in range(6):
        section = text.split(f'<master> lochan={i+2} hichan={i+2}')[1].split('<master>')[0]
        result[i] = frozenset(int(x) for x in re.findall(r'<region> key=(\d+)', section))
    return result

def _candidates(n, assigned, player, coverage, sample_rate):
    active = [a for a in assigned if a['player']==player and a['start']<n.end and a['end']>n.start]
    occupied = {a['string'] for a in active}
    previous = [a for a in assigned if a['player']==player and a['end']<=n.start and a['fret']>0]
    last = max(previous, key=lambda a:a['end'], default=None)
    following = [a for a in assigned if a['player']==player and a['start']>=n.end and a['fret']>0]
    after = min(following, key=lambda a:a['start'], default=None)
    result = []
    for shift in (0, -12, 12, -24, 24):
        for s in range(6):
            pitch = n.pitch+shift
            if s in occupied or pitch not in coverage[s]: continue
            fret = pitch-OPEN[s]
            frets = [a['fret'] for a in active if a['fret']>0]+([fret] if fret else [])
            if frets and (max(frets)-min(frets)>4 or len(frets)>4): continue
            if fret and last and abs(fret-last['fret'])>4+int((n.start-last['end'])/sample_rate*12): continue
            if fret and after and abs(fret-after['fret'])>4+int((after['start']-n.end)/sample_rate*12): continue
            movement=abs(fret-last['fret']) if last else fret
            result.append((abs(shift)*20+movement+fret*.1, s, fret, pitch, shift))
    return result


def _placed(n, player, candidate):
    _, s, fret, pitch, shift = candidate
    a=asdict(n)
    a.update(player=player,string=s,fret=fret,pitch=pitch,source_pitch=n.pitch,articulation='sampled-pluck')
    return a


def arrange(performance: Performance, performers=1, *, planner='greedy'):
    performance.validate()
    if performers not in (1, 2): raise ValueError('Choose exactly one or two guitarists')
    if planner not in ('greedy', 'lookahead', 'phrase'): raise ValueError('Unknown fingering planner')
    coverage = sampled_pitches()
    assigned, adaptations = [], []
    if len(performance.notes)>4000: raise ValueError('Diagnostic planning is limited to 4000 notes')
    # Reserve the complete melody first, including later entrances. Accompaniment
    # is fitted around these reservations; never secretly truncate a source note.
    ordered = sorted(performance.notes, key=lambda n:({'melody':0,'bass':1,'harmony':2}[n.role], n.start, n.id))
    for index, n in enumerate(ordered):
        player = 1 if performers == 1 or n.role == 'melody' else 2
        candidates = _candidates(n, assigned, player, coverage, performance.sample_rate)
        if not candidates:
            adaptations.append({'note_id':n.id,'kind':'omission','reason':'No sampled, unoccupied string satisfying conservative hand constraints','essential':n.role=='melody'})
            continue
        chosen = min(candidates)
        # Never buy a future benefit by changing this note's pitch. Only break
        # ties among equally faithful current choices, and only inside a phrase.
        upcoming = []
        if planner != 'greedy' and n.role == 'melody':
            previous = n
            for following in ordered[index+1:index+1+(2 if planner == 'phrase' else 1)]:
                if (following.role != n.role or following.phrase != n.phrase
                        or not previous.start < following.start <= previous.end):
                    break
                upcoming.append(following)
                previous = following
        if upcoming:
            faithful = [c for c in candidates if abs(c[4]) == abs(chosen[4])]
            # Earlier ended notes cannot become active again. Keep the latest
            # fretted predecessor plus every still-live reservation; this is
            # equivalent to scanning the full melody history at each branch.
            history = [a for a in assigned if a['player']==player and a['end']<=n.start and a['fret']>0]
            last = max(history, key=lambda a:a['end'], default=None)
            context = [a for a in assigned if a['player']==player and a['end']>n.start]
            if last is not None: context.append(last)
            def future_cost(candidate):
                states = [(0, 0., [_placed(n, player, candidate)])]
                for following in upcoming:
                    expanded = []
                    for penalty, movement, hypothetical in states:
                        futures = _candidates(following, context+hypothetical,
                                              player, coverage, performance.sample_rate)
                        if not futures:
                            expanded.append((penalty+100, movement, hypothetical))
                        for future in futures:
                            expanded.append((penalty+abs(future[4]), movement+future[0],
                                             hypothetical+[_placed(following, player, future)]))
                    # Fixed beam and two-note horizon bound search. Current pitch
                    # fidelity takes priority; future movement only orders beam.
                    states = sorted(expanded, key=lambda state: state[:2])[:8]
                return (min(state[0] for state in states), candidate)
            chosen = min(faithful, key=future_cost)
        _, s, fret, pitch, shift = chosen
        a = _placed(n, player, chosen)
        assigned.append(a)
        if shift: adaptations.append({'note_id':n.id,'kind':'octave','semitones':shift,'essential':n.role=='melody'})
        if n.contour: adaptations.append({'note_id':n.id,'kind':'contour-not-rendered','reason':'Baseline uses discrete sampled pitch; original contour retained'})
    assigned.sort(key=lambda n:(n['start'],n['player'],n['string']))
    return {'version':1,'source_id':performance.source_id,'sample_rate':performance.sample_rate,'performers':performers,'notes':assigned,'adaptations':adaptations,'source':asdict(performance),'render_origin_sample':0}
