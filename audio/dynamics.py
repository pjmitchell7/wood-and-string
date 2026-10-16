"""Pure, opt-in Meta SFZ calibration transform; no audio or asset mutation.

The calibrated quantity is first-250ms stereo RMS for a held sample, not playing
force, perceived loudness, an attack detector, or a source-confidence score.
"""
import hashlib
import math
import re

SCHEMA = 1
POLICY = 'meta-250ms-rms-18db-v80-v1'
LIMIT_DB = 12.0
LAYERS = ((1, 48), (49, 96), (97, 127))
VALIDATION_VELOCITIES = {1: (1, 24, 48), 2: (49, 80, 96), 3: (97, 112, 127)}
HEADER = '<control> default_path=ogg/\n<global> ampeg_release=0.05 pitch_random=0 amp_random=0 off_mode=normal\n'


def text_hash(text):
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def velocity_db(velocity):
    if type(velocity) is not int or not 1 <= velocity <= 127:
        raise ValueError('Velocity must be an integer from 1 to 127')
    return 18.0 * (velocity - 80) / 126.0


def velocity_curve():
    """All 127 points explicit; normalized to <=1 for SFZ amp_velcurve."""
    return {v: 10 ** ((velocity_db(v) - velocity_db(127)) / 20) for v in range(1, 128)}


def region_gain_db(unit_rms, reference_rms):
    """Actual emitted region volume, including the common curve offset."""
    if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 1e-8
           for x in (unit_rms, reference_rms)):
        raise ValueError('Calibration RMS is nonfinite, silent or below the usable floor')
    value = 20 * math.log10(reference_rms / unit_rms) + velocity_db(127)
    if abs(value) > LIMIT_DB:
        raise ValueError('Required region volume exceeds the fixed +/-12dB bound')
    return value


def _inventory(base_text):
    """Parse only the installed map's six explicit-string masters, fail closed."""
    clean = re.sub(r'//[^\n]*', '', base_text)
    masters = re.split(r'<master>', clean)[1:]
    found = {}; excluded = []
    for master in masters:
        prefix = master.split('<group>', 1)[0].strip()
        match = re.fullmatch(r'lochan=(\d+)\s+hichan=(\d+)', prefix)
        if not match or match[1] != match[2]:
            raise ValueError('Unexpected master map')
        channel = int(match[1])
        if channel == 1:
            continue
        string = channel - 2
        if string not in range(6) or string in found:
            raise ValueError('Unexpected or duplicate string master')
        parsed = []; number = -1
        for number, group in enumerate(master.split('<group>')[1:]):
            if number >= 3:
                raise ValueError('Unexpected layer count')
            head, *items = group.split('<region>')
            tokens = dict(token.split('=', 1) for token in head.split())
            lo, hi = LAYERS[number]
            expected = {'group': str(string+1), 'off_by': str(string+1),
                        'lovel': str(lo), 'hivel': str(hi),
                        f'amp_velcurve_{lo}': '0.708', f'amp_velcurve_{hi}': '1'}
            if tokens != expected or not items:
                raise ValueError('Unexpected native layer controls')
            fallback_count = 0
            for item in items:
                fallback = re.fullmatch(r'\s*lokey=47\s+hikey=47\s+pitch_keycenter=46\s+sample=(INSGuitarSteel_StringLowE_Vel0[123]_As2_01\.ogg)\s*', item)
                if fallback:
                    if string != 0 or f'_Vel{number+1:02d}_' not in fallback[1]:
                        raise ValueError('Unexpected retuned fallback')
                    fallback_count += 1
                    excluded.append(dict(string=0, pitch=47, pitch_keycenter=46, layer=number+1,
                        sample=fallback[1], reason='Retuned fallback, excluded by planner native-pitch coverage; not calibrated or emitted'))
                    continue
                match = re.fullmatch(r'\s*key=(\d+)\s+sample=([A-Za-z0-9_]+\.ogg)\s*', item)
                if not match:
                    raise ValueError('Unexpected region syntax')
                pitch = int(match[1]); sample = match[2]
                if not 0 <= pitch <= 127 or f'_Vel{number+1:02d}_' not in sample:
                    raise ValueError('Region layer or pitch mismatch')
                parsed.append(dict(id=f'{string}:{pitch}:{number+1}', string=string,
                    pitch=pitch, layer=number+1, lovel=lo, hivel=hi, sample=sample,
                    native_group=' '.join(head.split())))
            if fallback_count != (1 if string == 0 else 0):
                raise ValueError('Unexpected fallback inventory')
        if number != 2 or len({r['id'] for r in parsed}) != len(parsed):
            raise ValueError('Incomplete or duplicate layer inventory')
        pitches = [{r['pitch'] for r in parsed if r['layer'] == layer} for layer in (1, 2, 3)]
        if not pitches[0] or any(p != pitches[0] for p in pitches):
            raise ValueError('Layers have different pitch coverage')
        found[string] = parsed
    if set(found) != set(range(6)):
        raise ValueError('Six explicit-string masters are required')
    return [r for string in range(6) for r in found[string]], excluded


def regions(base_text):
    return _inventory(base_text)[0]


def excluded_remaps(base_text):
    return _inventory(base_text)[1]


def measurement_region_sfz(region, *, native=False, gain_db=None):
    """One-region probe map. Never used by the production renderer."""
    if native:
        if gain_db is not None:
            raise ValueError('Native measurement cannot have calibrated gain')
        group = region['native_group']
    else:
        group = (f"group={region['string']+1} off_by={region['string']+1} "
                 f"lovel={region['lovel']} hivel={region['hivel']}")
        if gain_db is None:
            group += ' amp_velcurve_1=1 amp_velcurve_127=1'
        else:
            if not math.isfinite(gain_db) or abs(gain_db) > LIMIT_DB:
                raise ValueError('Unsafe calibration gain')
            group += ' ' + ' '.join(f'amp_velcurve_{v}={a:.12g}' for v, a in velocity_curve().items())
    tail = '' if gain_db is None else f' volume={gain_db:.12g}'
    return HEADER + f"<group> {group}\n<region> key={region['pitch']} sample={region['sample']}{tail}\n"


def calibrated_string_sfz(base_text, string, manifest):
    """Return a calibrated exact-string map only for a fully accepted manifest.

    Pure validation checks map identity and all region records. The caller must
    separately verify installed sample/DLL hashes against manifest provenance.
    A reference-window pass does not certify other windows or musical quality.
    """
    if type(string) is not int or string not in range(6):
        raise ValueError('Invalid string')
    inventory = regions(base_text)
    if (not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA or manifest.get('policy') != POLICY
            or manifest.get('source_sfz_text_sha256') != text_hash(base_text)
            or manifest.get('status') != 'accepted-reference-only'
            or manifest.get('reference_window_ms') != 250
            or manifest.get('gain_limit_db') != LIMIT_DB
            or manifest.get('excluded_remaps') != excluded_remaps(base_text)):
        raise ValueError('Unaccepted or mismatched calibration manifest')
    entries = manifest.get('regions')
    if not isinstance(entries, list) or len(entries) != len(inventory):
        raise ValueError('Incomplete calibration inventory')
    indexed = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                or entry['id'] in indexed):
            raise ValueError('Duplicate or malformed calibration record')
        indexed[entry.get('id')] = entry
    lines = [HEADER]
    boundaries = {}
    curve = ' '.join(f'amp_velcurve_{v}={a:.12g}' for v, a in velocity_curve().items())
    for region in inventory:
        entry = indexed.get(region['id'], {})
        if (any(entry.get(k) != region[k] for k in ('string', 'pitch', 'layer', 'sample'))
                or entry.get('status') != 'accepted'
                or not isinstance(entry.get('sample_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', entry['sample_sha256'])):
            raise ValueError('Incomplete or rejected calibration region')
        gain = region_gain_db(entry.get('unit_rms'), entry.get('reference_rms'))
        pair = (region['string'], region['pitch'])
        reference, levels = boundaries.setdefault(pair, (entry['reference_rms'], {}))
        if entry['reference_rms'] != reference:
            raise ValueError('Layers disagree on the shared native reference')
        value = entry.get('gain_db')
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value-gain) > 1e-9:
            raise ValueError('Calibration gain disagrees with measurements')
        validations = entry.get('validation', [])
        if (not isinstance(validations, list) or any(not isinstance(v, dict) for v in validations)
                or [v.get('velocity') for v in validations] != list(VALIDATION_VELOCITIES[region['layer']])):
            raise ValueError('Missing frozen validation velocities')
        for validation in validations:
            try:
                rms = validation['measurement']['rms']['250ms']
                if type(rms) not in (int, float) or not math.isfinite(rms) or rms <= 0:
                    raise ValueError('Invalid validation RMS')
                error = 20*math.log10(rms/entry['reference_rms'])-velocity_db(validation['velocity'])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                raise ValueError('Invalid reference-window validation') from None
            if not math.isfinite(error) or abs(error) > .1:
                raise ValueError('Reference-window validation failed')
            levels[validation['velocity']] = rms
        if region['string'] == string:
            lines.append(f"<group> group={string+1} off_by={string+1} lovel={region['lovel']} hivel={region['hivel']} {curve}\n")
            lines.append(f"<region> key={region['pitch']} sample={region['sample']} volume={gain:.12g}\n")
    # Individually acceptable errors can still reverse a layer boundary.
    # Recompute the frozen rule rather than trusting the manifest status.
    for _, levels in boundaries.values():
        for lower, upper in ((48, 49), (96, 97)):
            if 20 * math.log10(levels[upper] / levels[lower]) < -.05:
                raise ValueError('Reference-window layer boundary is inverted')
    return ''.join(lines)
