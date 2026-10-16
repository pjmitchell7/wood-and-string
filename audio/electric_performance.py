"""Pure experimental assignment of observed notes to one/two electric guitars.

This does not recognize an instrument, transcribe music, infer harmony or prove
playability. Coverage is the declared Emilyguitar direct-sustain profile, not
the unrelated acoustic SFZ. No asset is opened and no source gate is extended.
"""
from dataclasses import asdict

from .emilyguitar import MAX_FRET, OPEN, PITCH_RANGE, PROFILE_VERSION
from .performance import Performance
from .refinement import performance_from_dict

VERSION = 'electric-standard-six-string-v1'
ARTICULATION = 'direct-sustain-first-variation'
MAX_FRETTED_NOTES = 4
MAX_FRETTED_SPAN = 4
MOVEMENT_ALLOWANCE = 4
MOVEMENT_PER_SECOND = 12
RATE = 44100
MAX_SECONDS = 30


def string_pitches():
    """Declared pitch/fret coverage only; this is not installed-bank attestation."""
    low, high = PITCH_RANGE
    return tuple(frozenset(range(max(low, opened), min(high, opened + MAX_FRET) + 1))
                 for opened in OPEN)


def _overlap(placed, note):
    return placed['start'] < note.end and placed['end'] > note.start


def _candidates(note, player, assigned, rate):
    overlapping = [placed for placed in assigned if _overlap(placed, note)]
    active = [placed for placed in overlapping if placed['player'] == player]
    occupied = {placed['string'] for placed in active}
    fretted = [placed['fret'] for placed in active if placed['fret']]
    # A long source gate can overlap several successive phrases. Count fingers
    # at actual simultaneous intervals, rather than treating all those phrases
    # as one chord. At a shared sample, note-offs precede note-ons.
    changes = sorted((sample, change) for placed in active if placed['fret']
                     for sample, change in ((max(note.start, placed['start']), 1),
                                            (min(note.end, placed['end']), -1)))
    fingers = peak_fingers = 0
    for _, change in changes:
        fingers += change
        peak_fingers = max(peak_fingers, fingers)
    sounding = {placed['pitch'] for placed in overlapping}
    melody_ceiling = min((placed['pitch'] for placed in overlapping
                          if placed['role'] == 'melody'), default=None)
    previous = [placed for placed in assigned if placed['player'] == player
                and placed['end'] <= note.start and placed['fret']]
    following = [placed for placed in assigned if placed['player'] == player
                 and placed['start'] >= note.end and placed['fret']]
    last = max(previous, key=lambda placed: (placed['end'], placed['id']), default=None)
    # For one historical fret, distance to a candidate is constant. The nearest
    # temporal boundary has the smallest allowance and therefore implies every
    # farther constraint for that fret. Retain at most MAX_FRET exact anchors
    # in each direction, while preserving the original last-note cost below.
    previous_by_fret, following_by_fret = {}, {}
    for placed in previous:
        fret = placed['fret']
        previous_by_fret[fret] = max(placed['end'], previous_by_fret.get(fret, placed['end']))
    for placed in following:
        fret = placed['fret']
        following_by_fret[fret] = min(placed['start'], following_by_fret.get(fret, placed['start']))
    result = []
    # Enumerate all supported octave equivalents, including extreme source MIDI
    # registers. Pitch fidelity is the first cost, before any fingering cost.
    for pitch in range(PITCH_RANGE[0], PITCH_RANGE[1] + 1):
        shift = pitch - note.pitch
        if shift % 12 or pitch in sounding:
            continue
        if note.role == 'bass' and melody_ceiling is not None and pitch >= melody_ceiling:
            continue
        for string, opened in enumerate(OPEN):
            fret = pitch - opened
            if string in occupied or not 0 <= fret <= MAX_FRET:
                continue
            if fret and (peak_fingers >= MAX_FRETTED_NOTES or
                         any(abs(fret - other) > MAX_FRETTED_SPAN for other in fretted)):
                continue
            if fret:
                # Check all temporal reservations, not just the last sorted ID.
                # This also prevents a chain of near-instant four-fret jumps
                # accumulating unlimited movement under the fixed allowance.
                if any(abs(fret - prior_fret) > MOVEMENT_ALLOWANCE +
                       (note.start - prior_end) * MOVEMENT_PER_SECOND // rate
                       for prior_fret, prior_end in previous_by_fret.items()):
                    continue
                if any(abs(fret - next_fret) > MOVEMENT_ALLOWANCE +
                       (next_start - note.end) * MOVEMENT_PER_SECOND // rate
                       for next_fret, next_start in following_by_fret.items()):
                    continue
            movement = abs(fret - last['fret']) if last is not None else fret
            result.append(((abs(shift), movement, fret, string, pitch),
                           string, fret, pitch, shift))
    return result


def arrange_electric(performance, performers=1, *, render_origin_sample=0):
    """Return detached source evidence, dry events and explicit compromises.

    Solo considers all observed roles. Duo reserves melody for player 1 and
    bass/harmony for player 2. Source bass stays strictly below all overlapping
    placed melody, including a later entrance. No overlapping output unisons
    are doubled, within a guitar or across guitars. Harmony may cross melody.

    render_origin_sample is an explicit source-clock passage origin, independent
    of the retained analysis_offset_samples metadata. Every note must fit the
    renderer's 30-second, 44100 Hz window. An essential omission or empty player
    makes renderable false; notes/ledgers remain available for explicit review.
    No decision here establishes acoustic fidelity or sample sustain duration.
    """
    if type(performance) is not Performance:
        raise ValueError('Expected Performance')
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose exactly one or two electric guitarists')
    if type(render_origin_sample) is not int or render_origin_sample < 0:
        raise ValueError('Expected a nonnegative integer source passage origin')
    # Shared serialization rejects loose bool/integer coercions and detaches
    # beats, contours and all other retained source metadata before planning.
    source = performance_from_dict(asdict(performance))
    if source.sample_rate != RATE:
        raise ValueError('Electric assignment requires the 44100 Hz source sample clock')
    if not source.notes:
        raise ValueError('Electric assignment requires observed source notes')
    if any(len(note.id) > 200 for note in source.notes):
        raise ValueError('Electric event identities must fit the 200-character renderer bound')
    if any(not render_origin_sample <= note.start < note.end <= render_origin_sample + MAX_SECONDS * RATE
           for note in source.notes):
        raise ValueError('Observed notes must fit the explicit 30-second source passage')
    roles = {note.role for note in source.notes}
    if performers == 2 and ('melody' not in roles or not roles.intersection({'bass', 'harmony'})):
        raise ValueError('Electric duo requires observed melody and distinct accompaniment material')

    assigned, adaptations, issues = [], [], []
    ordered = sorted(source.notes, key=lambda note: (
        {'melody': 0, 'bass': 1, 'harmony': 2}[note.role], note.start, note.end, note.id))
    for note in ordered:
        player = 1 if performers == 1 or note.role == 'melody' else 2
        candidates = _candidates(note, player, assigned, source.sample_rate)
        if not candidates:
            reason = ('No octave-equivalent pitch satisfies string occupancy, four-fret hand/span, '
                      'bounded movement, no overlapping unison and lower-foundation constraints')
            adaptations.append({'note_id': note.id, 'kind': 'omission', 'role': note.role,
                                'reason': reason, 'essential': note.role == 'melody'})
            if note.role == 'melody':
                issues.append({'kind': 'essential-note-unplaced', 'note_id': note.id, 'reason': reason})
        else:
            _, string, fret, pitch, shift = min(candidates)
            placed = asdict(note)
            placed.update(player=player, string=string, fret=fret, pitch=pitch,
                          source_pitch=note.pitch, articulation=ARTICULATION)
            assigned.append(placed)
            if shift:
                adaptations.append({'note_id': note.id, 'kind': 'octave', 'semitones': shift,
                                    'source_pitch': note.pitch, 'sounding_pitch': pitch,
                                    'essential': note.role == 'melody'})
        if note.contour:
            adaptations.append({'note_id': note.id, 'kind': 'contour-not-rendered',
                                'reason': 'Original contour retained as evidence; direct sustain events do not reproduce bends/vibrato'})

    assigned.sort(key=lambda note: (note['start'], note['player'], note['string'], note['id']))
    represented = {note['player'] for note in assigned}
    for player in range(1, performers + 1):
        if player not in represented:
            issues.append({'kind': 'performer-without-material', 'player': player,
                           'reason': 'No source event could be assigned to this requested performer'})
    return {'version': 1, 'planner_version': VERSION, 'profile_version': PROFILE_VERSION,
            'instrument': 'electric', 'source_id': source.source_id,
            'sample_rate': source.sample_rate, 'performers': performers,
            'render_origin_sample': render_origin_sample,
            'renderable': bool(assigned) and not issues,
            'notes': assigned, 'adaptations': adaptations, 'issues': issues,
            'source': asdict(source),
            'physical_assumptions': {
                'verified_instrument_fingering': False, 'open_sounding_midi': list(OPEN),
                'max_fret': MAX_FRET, 'sounding_pitch_range': list(PITCH_RANGE),
                'maximum_fretted_notes': MAX_FRETTED_NOTES, 'maximum_fretted_span': MAX_FRETTED_SPAN,
                'movement_frets_per_second': MOVEMENT_PER_SECOND,
                'movement_allowance_frets': MOVEMENT_ALLOWANCE,
                'statement': 'Conservative standard E-A-D-G-B-E assumptions; experimental observed-note assignment, not guitar recognition or verified fingering.'}}
