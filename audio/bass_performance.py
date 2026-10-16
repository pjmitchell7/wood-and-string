"""Conservative experimental four-string bass assignment, not fingering truth.

All output pitches are sounding MIDI. The sample bank's keyboard convention
belongs to the renderer and must not be folded into source/arrangement pitch.
"""
from dataclasses import asdict

from .performance import Performance
from .refinement import performance_from_dict

VERSION = 'jazz-bass-four-string-v1'
OPEN = (25, 33, 38, 43)  # assumed C#1, A1, D2, G2
MAX_FRET = 20
RANGES = {'solo': (25, 63), 'melody': (37, 63), 'bass': (25, 43)}


def _overlap(a, b):
    return a['start'] < b.end and a['end'] > b.start


def _candidates(note, player, assigned, register, rate):
    active = [a for a in assigned if a['player'] == player and _overlap(a, note)]
    occupied = {a['string'] for a in active}
    previous = [a for a in assigned if a['player'] == player and a['end'] <= note.start and a['fret'] > 0]
    following = [a for a in assigned if a['player'] == player and a['start'] >= note.end and a['fret'] > 0]
    last = max(previous, key=lambda a:(a['end'],a['id']), default=None)
    after = min(following, key=lambda a:(a['start'],a['id']), default=None)
    other = [a for a in assigned if a['player'] != player and _overlap(a, note)]
    result = []
    # Every octave-equivalent MIDI candidate in the bounded sounding register.
    for pitch in range(register[0], register[1]+1):
        shift = pitch-note.pitch
        if shift % 12 or any(a['pitch'] == pitch or (player == 2 and pitch >= a['pitch']) for a in other):
            continue
        for string, opened in enumerate(OPEN):
            fret = pitch-opened
            if string in occupied or not 0 <= fret <= MAX_FRET:
                continue
            fretted = [a['fret'] for a in active if a['fret'] > 0] + ([fret] if fret else [])
            if fretted and max(fretted)-min(fretted) > 4:
                continue
            if fret and last and abs(fret-last['fret']) > 4+int((note.start-last['end'])/rate*12):
                continue
            if fret and after and abs(fret-after['fret']) > 4+int((after['start']-note.end)/rate*12):
                continue
            movement = abs(fret-last['fret']) if last else fret
            result.append(((abs(shift), movement, fret, string, pitch), string, fret, pitch, shift))
    return result


def arrange_bass(performance, performers=1, *, solo_part='melody'):
    """Return a source-preserving, versioned experimental bass arrangement.

    Solo chooses one source role explicitly. Duo reserves melody for player1
    and foundation for player2; foundation must stay strictly below every
    overlapping melody note. Neither gaps nor note ends are changed. Unsupported selected
    velocity fails rather than silently substituting another dynamic layer.
    """
    if type(performance) is not Performance:
        raise ValueError('Expected Performance')
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose exactly one or two bass players')
    if type(solo_part) is not str or solo_part not in ('melody', 'bass-line'):
        raise ValueError('Choose melody or bass-line')
    if performers == 2 and solo_part != 'melody':
        raise ValueError('Two bass players use melody-and-bass, not a solo part choice')
    # Shared strict serialization validator also detaches nested source metadata.
    source = performance_from_dict(asdict(performance))
    selected_roles = {'melody','bass'} if performers == 2 else {('bass' if solo_part == 'bass-line' else 'melody')}
    if not selected_roles <= {n.role for n in source.notes}:
        raise ValueError('Selected bass arrangement requires the requested source part(s)')
    selected = [n for n in source.notes if n.role in selected_roles]
    if any(n.velocity > 120 for n in selected):
        raise ValueError('Experimental bass rendering supports selected note velocities 1 through 120')
    assigned, adaptations = [], []
    for n in source.notes:
        if n.role not in selected_roles:
            adaptations.append({'note_id':n.id, 'kind':'omission', 'role':n.role,
                                'reason':'Excluded by explicit bass part selection', 'essential':n.role=='melody'})
    ordered = sorted(selected, key=lambda n: (0 if n.role=='melody' else 1, n.start, n.end, n.id))
    for n in ordered:
        player = 1 if performers == 1 or n.role == 'melody' else 2
        register = RANGES['solo' if performers == 1 else n.role]
        candidates = _candidates(n, player, assigned, register, source.sample_rate)
        if not candidates:
            adaptations.append({'note_id':n.id, 'kind':'omission', 'role':n.role,
                                'reason':'No octave-equivalent pitch on an available string within register, hand/movement and strictly lower foundation constraints',
                                'essential':n.role=='melody'})
            continue
        _, string, fret, pitch, shift = min(candidates)
        placed = asdict(n)
        placed.update(player=player, string=string, fret=fret, pitch=pitch,
                      source_pitch=n.pitch, articulation='clean-sustain-first-variation')
        assigned.append(placed)
        if shift:
            adaptations.append({'note_id':n.id,'kind':'octave','semitones':shift,
                                'source_pitch':n.pitch,'sounding_pitch':pitch,'essential':n.role=='melody'})
        if n.contour:
            adaptations.append({'note_id':n.id,'kind':'contour-not-rendered',
                                'reason':'Discrete clean sustain sample; original contour retained'})
    assigned.sort(key=lambda n:(n['start'],n['player'],n['string'],n['id']))
    return {'version':1,'planner_version':VERSION,'instrument':'jazz-bass',
            'source_id':source.source_id,'sample_rate':source.sample_rate,'performers':performers,
            'part_selection':'melody-and-bass' if performers==2 else solo_part,
            'notes':assigned,'adaptations':adaptations,'source':asdict(source),'render_origin_sample':0,
            'physical_assumptions':{'verified_instrument_fingering':False,'open_sounding_midi':list(OPEN),
                'max_fret':MAX_FRET,'maximum_fretted_span':4,'movement_frets_per_second':12,
                'movement_allowance_frets':4,'range_policy':dict(RANGES),
                'statement':'Conservative assumed C#-A-D-G four-string tuning; not verified instrument-specific fingering. Bank keyboard offset is renderer-only.'}}
