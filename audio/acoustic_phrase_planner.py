"""Experimental bounded acoustic fingering search; never selected by production.

The fixed policy was declared before the GuitarSet development check. Source
strings, annotations, reference answers and renderer measurements are not inputs.
Every transition uses the existing sampled coverage and conservative constraints.
"""
from dataclasses import asdict, dataclass

from audio.performance import Performance, _candidates, _placed, sampled_pitches

VERSION = 'acoustic-phrase-beam-v1'
BEAM_WIDTH = 128
MAX_SUCCESSORS = 250_000
MAX_NOTES = 4_000
ROLE_ORDER = {'melody': 0, 'bass': 1, 'harmony': 2}
COST_FIELDS = ('essential_omissions', 'essential_octave_semitones',
               'other_omissions', 'other_octave_semitones',
               'position_cost_tenths')


@dataclass(frozen=True)
class _Path:
    parent: object
    note: object
    placed: object


@dataclass(frozen=True)
class _State:
    cost: tuple
    context: tuple
    path: object


def _context(assigned, boundary):
    """Exact future context, including stable ties for the old movement rule.

All remaining starts are >= boundary. Earlier unfretted notes cannot affect a
future candidate; of ended fretted notes only the latest for each player can.
Keep the original order: max/min in _candidates break equal-time ties by order.
"""
    last = {}
    for index, note in enumerate(assigned):
        if note['end'] <= boundary and note['fret'] > 0:
            previous = last.get(note['player'])
            if previous is None or note['end'] > assigned[previous]['end']:
                last[note['player']] = index
    keep = set(last.values())
    return tuple(note for index, note in enumerate(assigned)
                 if note['end'] > boundary or index in keep)


def _key(context):
    # Exactly the fields consumed by _candidates; note identities stay in _Path.
    return tuple((n['player'], n['string'], n['fret'], n['start'], n['end'])
                 for n in context)


def _cost(previous, note, candidate):
    result = list(previous)
    essential = note.role == 'melody'
    if candidate is None:
        result[0 if essential else 2] += 1
    else:
        shift = abs(candidate[4])
        result[1 if essential else 3] += shift
        # The existing candidate cost is octave*20 + movement + fret/10.
        # Octaves already have explicit higher-priority costs above.
        result[4] += round((candidate[0] - shift * 20) * 10)
    return tuple(result)


def arrange(performance: Performance, performers=1):
    """Search entire role-ordered phrase sequence with a fixed-width beam.

Melody has the existing essential status and player1 routing; other roles route
to player2 in a duet. Phrase labels/gates/contours are evidence, never merged.
The beam keeps alternate histories until the end, rather than a fixed two-note
lookahead. Beam pruning is heuristic. Hitting the successor cap returns an
explicitly incomplete, non-renderable partial plan, with no greedy fallback.
"""
    performance.validate()
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose exactly one or two guitarists')
    if len(performance.notes) > MAX_NOTES:
        raise ValueError('Experimental planning is limited to 4000 notes')
    ordered = sorted(performance.notes,
                     key=lambda n: (ROLE_ORDER[n.role], n.start, n.id))
    coverage = sampled_pitches()
    # Lower-priority roles can start before already reserved melody. The suffix
    # minimum across ALL roles prevents incorrectly dropping those reservations.
    bounds = [float('inf')] * (len(ordered) + 1)
    for i in range(len(ordered) - 1, -1, -1):
        bounds[i] = min(bounds[i + 1], ordered[i].start)
    states = [_State((0, 0, 0, 0, 0), (), None)]
    successors = deduplicated = pruned = processed = 0
    exhausted = False
    for index, note in enumerate(ordered):
        player = 1 if performers == 1 or note.role == 'melody' else 2
        layer = {}
        for state in states:
            choices = sorted(_candidates(note, state.context, player, coverage,
                                         performance.sample_rate)) + [None]
            for candidate in choices:
                if successors >= MAX_SUCCESSORS:
                    exhausted = True
                    break
                successors += 1
                placed = None if candidate is None else _placed(note, player, candidate)
                context = _context(state.context + (() if placed is None else (placed,)),
                                   bounds[index + 1])
                child = _State(_cost(state.cost, note, candidate), context,
                               _Path(state.path, note, placed))
                key = _key(context)
                previous = layer.get(key)
                if previous is not None:
                    deduplicated += 1
                if previous is None or child.cost < previous.cost:
                    layer[key] = child
            if exhausted:
                break
        if exhausted:
            # Discard an incomplete layer, whose enumeration order would bias
            # the selected partial result. Prior completed layers remain valid.
            break
        ranked = sorted(layer.values(), key=lambda state: state.cost)
        pruned += max(0, len(ranked) - BEAM_WIDTH)
        states = ranked[:BEAM_WIDTH]
        processed = index + 1
    best = min(states, key=lambda state: state.cost)
    path = []
    step = best.path
    while step is not None:
        path.append(step)
        step = step.parent
    assigned, adaptations = [], []
    for step in reversed(path):
        note, placed = step.note, step.placed
        if placed is None:
            adaptations.append({'note_id': note.id, 'kind': 'omission',
                                'reason': 'Omitted by bounded phrase search; not a proof of physical impossibility',
                                'essential': note.role == 'melody'})
            continue
        assigned.append(placed)
        shift = placed['pitch'] - note.pitch
        if shift:
            adaptations.append({'note_id': note.id, 'kind': 'octave',
                                'semitones': shift, 'essential': note.role == 'melody'})
        if note.contour:
            adaptations.append({'note_id': note.id, 'kind': 'contour-not-rendered',
                                'reason': 'Discrete sampled pitch; source contour retained unchanged'})
    for note in ordered[processed:]:
        adaptations.append({'note_id': note.id, 'kind': 'omission',
                            'reason': 'Search successor cap reached before this note was resolved',
                            'essential': note.role == 'melody', 'unresolved': True})
    assigned.sort(key=lambda n: (n['start'], n['player'], n['string']))
    return {'version': 1, 'source_id': performance.source_id,
            'sample_rate': performance.sample_rate, 'performers': performers,
            'notes': assigned, 'adaptations': adaptations,
            'source': asdict(performance), 'render_origin_sample': 0,
            'planner_version': VERSION, 'renderable': not exhausted and bool(assigned),
            'search': {'complete': not exhausted, 'exhausted': exhausted,
                       'beam_width': BEAM_WIDTH, 'successor_cap': MAX_SUCCESSORS,
                       'successors': successors, 'processed_notes': processed,
                       'unresolved_notes': len(ordered) - processed,
                       'equivalent_states_merged': deduplicated,
                       'beam_states_pruned': pruned,
                       'cost': dict(zip(COST_FIELDS, best.cost)),
                       'cost_scope': 'Completed prefix only' if exhausted else 'All input notes',
                       'optimality': 'Not claimed; fixed beam search may discard a better fingering',
                       'constraint_scope': 'Unchanged sampled coverage, string occupancy, conservative hand and movement rules; not a biomechanical or musical judgment'}}
