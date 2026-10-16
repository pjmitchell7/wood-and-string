"""Pure header/map inspection and predeclared probe plan; NO native execution.

--freeze writes a fresh plan only. Native sfizz execution requires a separately
reviewed runner, explicit root go and terminal GPU work. No playback is provided.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import wave

from . import acquire_emilyguitar as acquisition

APP = Path(__file__).resolve().parents[2]
ACQUIRED = APP / 'audio/research/emilyguitar-002'
BANK = ACQUIRED / 'bank'
OUT = APP / 'audio/research/emilyguitar-probe-001'
SR = 44100
ORIGIN = 5336117
OPCODES = {'lokey', 'hikey', 'pitch_keycenter', 'bend_up', 'bend_down', 'ampeg_release',
           'lovel', 'hivel', 'amp_velcurve_40', 'amp_velcurve_80', 'amp_velcurve_120',
           'sample', 'lorand', 'hirand', 'trigger', 'volume', 'amp_random', 'rt_decay'}


def wav_header(path):
    """Check chunk geometry and uncompressed PCM metadata, without sample reads."""
    size = path.stat().st_size
    chunks = []
    with path.open('rb') as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise ValueError('Expected ordinary RIFF/WAVE')
        if struct.unpack_from('<I', header, 4)[0] + 8 != size:
            raise ValueError('RIFF length differs from retained file')
        position = 12
        while position < size:
            source.seek(position)
            tag, length = struct.unpack('<4sI', source.read(8))
            end = position + 8 + length
            if end + length % 2 > size:
                raise ValueError('Chunk extends beyond file')
            chunks.append({'tag': tag.decode('ascii'), 'bytes': length, 'offset': position + 8})
            position = end + length % 2
        if position != size or sum(row['tag'] == 'data' for row in chunks) != 1:
            raise ValueError('Unexpected WAV chunk closure')
    with wave.open(str(path), 'rb') as source:
        values = source.getparams()
    data = next(row for row in chunks if row['tag'] == 'data')
    if (values.nchannels != 1 or values.sampwidth != 3 or values.framerate != SR
            or values.comptype != 'NONE' or values.nframes <= 0
            or data['bytes'] != values.nframes * values.nchannels * values.sampwidth):
        raise ValueError('Expected complete mono PCM24/44100 source')
    return {'bytes': size, 'channels': values.nchannels, 'sample_width_bytes': values.sampwidth,
            'sample_rate': values.framerate, 'frames': values.nframes,
            'stored_seconds': values.nframes / SR, 'chunks': chunks,
            'limit': 'Stored duration only; no measurement of audible sustain, silence or tuning.'}


def parse_basic(text):
    """Narrow parser for this pinned group/region-only map; fail on new syntax."""
    text = re.sub(r'//[^\n]*', '', text)
    pieces = re.split(r'<([^<>]+)>', text)
    if pieces[0].strip() or len(pieces) % 2 != 1:
        raise ValueError('Unsupported SFZ preamble/tags')
    group, rows = {}, []
    for index in range(1, len(pieces), 2):
        tag, body = pieces[index].strip(), pieces[index + 1]
        pairs = re.findall(r'(\w+)\s*=\s*([^\s<>]+)', body)
        if re.sub(r'(\w+)\s*=\s*([^\s<>]+)', '', body).strip():
            raise ValueError('Unparsed SFZ content')
        values = dict(pairs)
        if len(values) != len(pairs) or set(values) - OPCODES:
            raise ValueError('Duplicate or unsupported opcode')
        if tag == 'group':
            group = values
            continue
        if tag != 'region':
            raise ValueError('Unsupported SFZ tag')
        effective = dict(group, **values)
        sample = effective['sample'].replace('\\', '/')
        if ('..' in Path(sample).parts or sample.startswith('/') or ':' in sample
                or sample.split('/')[0] not in ('notes', 'release', 'noises')):
            raise ValueError('Unexpected sample path')
        low, high = int(effective['lokey']), int(effective['hikey'])
        vlo, vhi = int(effective.get('lovel', 1)), int(effective.get('hivel', 127))
        rlo, rhi = float(effective.get('lorand', 0)), float(effective.get('hirand', 1))
        if not (0 <= low <= high <= 127 and 1 <= vlo <= vhi <= 127 and 0 <= rlo < rhi <= 1):
            raise ValueError('Invalid key/velocity/random interval')
        category = sample.split('/')[0]
        root = int(effective.get('pitch_keycenter', 60))
        candidate = category != 'noises' and max(low, 40) <= min(high, 84)
        rows.append({'id': f'basic-{len(rows):03d}', 'sample': sample, 'category': category,
                     'key_range': [low, high], 'velocity_range': [vlo, vhi],
                     'random_interval': [rlo, rhi], 'pitch_keycenter': root,
                     'root_basis': 'explicit_opcode' if 'pitch_keycenter' in effective
                                   else 'SFZ_default_60_assumption_requires_native_omitted_vs_explicit_control',
                     'candidate_40_84': candidate, 'opcodes': effective})
    return rows


def isolated_map(row, explicit_default=True):
    """Deterministic diagnostic ablation, not the original random bank mapping."""
    values = {key: value for key, value in row['opcodes'].items()
              if key not in ('lorand', 'hirand', 'amp_random')}
    values['sample'] = row['sample']
    values['amp_random'] = '0'
    if explicit_default and 'pitch_keycenter' not in values:
        values['pitch_keycenter'] = str(row['pitch_keycenter'])
    return '<region>\n' + '\n'.join(f'{key}={value}' for key, value in values.items()) + '\n'


def inspect_bank():
    receipt = json.loads((ACQUIRED / 'acquisition.json').read_text(encoding='utf-8'))
    if receipt['state'] != 'complete' or receipt['wav_git_exact_count'] != 323:
        raise ValueError('Complete verified acquisition required')
    inventory_path = ACQUIRED / 'inventory.json'
    if acquisition.sha(inventory_path) != receipt['inventory_sha256']:
        raise ValueError('Acquisition inventory changed')
    inventory = {row['path']: row for row in json.loads(inventory_path.read_text())}
    headers = {}
    for name, row in inventory.items():
        if not name.endswith('.wav'):
            continue
        path = BANK / name
        if acquisition.sha(path) != row['sha256']:
            raise ValueError('Verified sample changed: ' + name)
        headers[name] = dict(wav_header(path), sha256=row['sha256'], git_blob_sha1=row['actual_git_blob_sha1'])
    mapping = BANK / 'emily_basic.sfz'
    if acquisition.sha(mapping) != inventory['emily_basic.sfz']['sha256']:
        raise ValueError('Basic map changed')
    rows = parse_basic(mapping.read_text())
    if len(rows) != 323 or {row['sample'] for row in rows} != set(headers):
        raise ValueError('Basic map does not reference each verified WAV exactly once')
    counts = {key: sum(row['category'] == key for row in rows) for key in ('notes', 'release', 'noises')}
    if counts != {'notes': 216, 'release': 72, 'noises': 35}:
        raise ValueError('Unexpected basic-map category counts')
    for pitch in range(40, 85):
        for velocity in range(1, 128):
            matches = [row for row in rows if row['category'] == 'notes'
                       and row['key_range'][0] <= pitch <= row['key_range'][1]
                       and row['velocity_range'][0] <= velocity <= row['velocity_range'][1]]
            intervals = sorted(row['random_interval'] for row in matches)
            if len(matches) != 3 or intervals[0][0] != 0 or intervals[-1][1] != 1:
                raise ValueError('Missing note coverage/variation')
            if any(abs(left[1] - right[0]) > 1e-9 for left, right in zip(intervals, intervals[1:])):
                raise ValueError('Gap/overlap in declared random intervals')
    durations = {}
    for category in counts:
        selected = [data['frames'] for name, data in headers.items() if name.startswith(category + '/')]
        durations[category] = {'files': len(selected), 'min_frames': min(selected), 'max_frames': max(selected),
                               'min_seconds': min(selected) / SR, 'max_seconds': max(selected) / SR}
    return {'acquisition_receipt_sha256': acquisition.sha(ACQUIRED / 'acquisition.json'),
            'inventory_sha256': receipt['inventory_sha256'], 'basic_map_sha256': acquisition.sha(mapping),
            'headers': headers, 'regions': rows, 'counts': counts, 'durations': durations,
            'candidate_regions': {key: sum(row['category'] == key and row['candidate_40_84'] for row in rows)
                                  for key in counts},
            'implicit_root_regions': [row['id'] for row in rows if row['root_basis'] != 'explicit_opcode'],
            'coverage': 'Every MIDI40..84/velocity1..127 cell has three declared note variations; '
                        'random interval endpoints/engine selection semantics are not rendered evidence.'}


def build_plan(report):
    regions = report['regions']
    notes = [row for row in regions if row['category'] == 'notes']
    releases = [row for row in regions if row['category'] == 'release']
    cells = []
    for row in notes:
        velocity = (row['velocity_range'][0] + row['velocity_range'][1]) // 2
        cells.append({'id': 'source-' + row['id'], 'kind': 'single_source_note', 'region_ids': [row['id']],
                      'pitch': row['pitch_keycenter'], 'velocity': velocity,
                      'on_sample': 0, 'off_sample': SR, 'frames': 3 * SR // 2,
                      'candidate_40_84': row['candidate_40_84'], 'retain_float': False})
    for row in releases:
        attack = next(note for note in notes if note['key_range'] == row['key_range']
                      and note['velocity_range'][0] <= 60 <= note['velocity_range'][1])
        cells.append({'id': 'release-' + row['id'], 'kind': 'paired_note_off_release_and_attack_only_control',
                      'region_ids': [attack['id'], row['id']], 'pitch': row['pitch_keycenter'], 'velocity': 60,
                      'on_sample': 0, 'off_sample': round(.4 * SR), 'frames': SR,
                      'retain_float': False, 'repeat_without_release_region': True})
    # Known commands only. Virtual string IDs are assumptions, not source-string provenance.
    def event(string, pitch, start, end, velocity=80):
        opens = [40, 45, 50, 55, 59, 64]
        if not 0 <= pitch - opens[string] <= 20:
            raise ValueError('Control is outside declared virtual fingering')
        return {'string': string, 'fret': pitch - opens[string], 'pitch': pitch, 'velocity': velocity,
                'start': ORIGIN + round(start * SR), 'end': ORIGIN + round(end * SR)}
    controls = [
        {'id': 'held', 'events': [event(5, 64, .1, 3.6)]},
        {'id': 'early-off', 'events': [event(5, 64, .1, .6)]},
        {'id': 'same-pitch-touch', 'events': [event(5, 64, .1, .6), event(5, 64, .6, 1.1)]},
        {'id': 'different-pitch-touch', 'events': [event(5, 64, .1, .6), event(5, 65, .6, 1.1)]},
        {'id': 'explicit-rest', 'events': [event(5, 64, .1, .6), event(5, 64, .9, 1.4)]},
        {'id': 'different-string-ring', 'events': [event(0, 40, .1, 3.6), event(5, 64, .6, 1.1)]},
        {'id': 'low-string-alone', 'events': [event(0, 40, .1, 3.6)]},
        {'id': 'high-string-alone', 'events': [event(5, 64, .6, 1.1)]},
        {'id': 'held-past-stored-end', 'events': [event(0, 40, .1, 6.1)], 'frames': 7 * SR},
        {'id': 'same-pitch-repeatability-copy', 'events': [event(5, 64, .1, .6), event(5, 64, .6, 1.1)]},
    ]
    for control in controls:
        control.setdefault('frames', 5 * SR)
        control.update(render_origin_sample=ORIGIN, retain_float=True, note_variation=1, release_variation=1,
                       mode='native_note_off_no_adapter_choke_or_fade')
    dynamic = [{'pitch': pitch, 'velocity': velocity, 'variation': 1, 'frames': 3 * SR // 2,
                'on_sample': 0, 'off_sample': SR, 'retain_float': False}
               for pitch in (40, 60, 84) for velocity in (1, 40, 41, 80, 81, 120, 121, 127)]
    implicit_notes = [row['id'] for row in notes if row['root_basis'] != 'explicit_opcode']
    implicit_releases = [row['id'] for row in releases if row['root_basis'] != 'explicit_opcode']
    retained_float_bytes = sum(row['frames'] * 2 * 4 for row in controls)
    if retained_float_bytes + 4 * 1024**2 > 64 * 1024**2:
        raise ValueError('Planned output/scratch allowance exceeded')
    return {'version': 1, 'state': 'frozen_plan_only_not_authorized_for_native_execution',
            'bank': str(BANK), 'sample_rate': SR,
            'candidate_profile': {'opens': [40, 45, 50, 55, 59, 64], 'frets': 20, 'pitch_range': [40, 84],
                                  'source_string_authenticity': False, 'mode_enabled': False},
            'diagnostic_mapping': {'policy': 'One selected sample region; preserve original nonrandom opcodes. '
                                  'Remove lorand/hirand and set amp_random=0. No gain calibration, EQ, loop, amp or reverb.',
                                  'source_regions_outside_candidate_range': 'Measure and label, never expand playable profile.',
                                  'implicit_root': 'Explicit 60 in isolated map, plus omitted-vs-explicit paired native control.'},
            'source_cells': cells, 'dynamic_cells': dynamic, 'controls': controls,
            'implicit_root_pair_regions': implicit_notes + implicit_releases,
            'source_geometry': {'note_regions': len(notes), 'release_regions': len(releases),
                                'noise_headers_only': 35, 'implicit_note_root_pairs': len(implicit_notes),
                                'implicit_release_root_pairs': len(implicit_releases)},
            'control_scheduler': 'Exact integer sample boundaries; note-offs precede note-ons at a shared '
                                 'sample. One synth per virtual string, no original audio mixed in.',
            'native_setup_required': 'Existing managed owned worker; new synth per source cell/control string; '
                                     '44100 stereo float, block256, freewheeling with live sample/oscillator '
                                     'quality captured and pinned as in audio.render._configure_offline.',
            'metrics': ['All raw finite/no normalization; full sample peak and 100/250/1000ms RMS',
                        'Two independent pitch estimates for every attack source, with agreement/cents/ambiguity; '
                        'do not force a pitch for noise/release',
                        'Omitted vs explicit C4 root output equality; gate source-sample timing unchanged',
                        'Release-pair minus attack-only: pre-note-off identity and post-note-off difference',
                        'Exact deterministic repeat hash; separate-string output vs isolated-source sum residual',
                        'Residual around same/different-pitch reattack, tail RMS after note-off/rest, sample-boundary jump',
                        'Long held gate vs stored sample end: no inferred looping or invented sustain',
                        'Velocity boundary energy changes per pitch; never claim monotonicity or fix silently'],
            'decision_rules': ['Fail any missing/mismatched/unsafe sample or nonfinite output',
                               'Flag >50-cent disagreement with declared source root or between pitch estimates; '
                               'ambiguous estimates require review, not auto-retuning',
                               'Require repeat hashes and C4 omitted/explicit controls equal under deterministic map',
                               'Report every >1dB energy drop at increasing adjacent velocity-layer boundary; no calibration promotion',
                               'Native same-string residual is a measured baseline, not proof of physical damping; '
                               'a future choke/fade adapter requires a separately frozen test',
                               'No instrument-mode enablement or listening acceptance from these controls'],
            'budget': {'wall_seconds': 180, 'sampled_process_memory_stop_bytes': 1024**3,
                       'scratch_and_retained_cap_bytes': 64 * 1024**2,
                       'planned_retained_stereo_float_bytes': retained_float_bytes,
                       'metadata_and_working_margin_bytes': 4 * 1024**2,
                       'policy': 'Stream source-cell/dynamic metrics and discard their blocks; retain only ten controls. '
                                 'No copy of bank; no PCM listening copies in this plan. Bounds are proposed, not measured runtime.'},
            'required_before_native': ['Root reviews concrete runner/code/native dependency pins and this plan',
                                       'Root confirms GPU phase terminal and grants native slot',
                                       'Live combined shared storage/free disk check includes these files and all reservations']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true', required=True)
    parser.parse_args()
    report = inspect_bank()
    plan = build_plan(report)
    OUT.mkdir(exist_ok=False)
    acquisition.final_once(OUT / 'headers-and-map.json', report)
    plan.update(inspection_sha256=acquisition.sha(OUT / 'headers-and-map.json'),
                source_sha256=acquisition.sha(Path(__file__)),
                source_acquisition_sha256=report['acquisition_receipt_sha256'],
                sfizz_dll_sha256=acquisition.sha(APP / 'audio/vendor/sfizz/bin/Release/sfizz.dll'),
                render_adapter_sha256=acquisition.sha(APP / 'audio/render.py'))
    acquisition.final_once(OUT / 'frozen-plan.json', plan)
    with (OUT / 'source-probe_emilyguitar.py').open('xb') as stream:
        stream.write(Path(__file__).read_bytes())
    print(json.dumps({'state': plan['state'], 'plan': str(OUT / 'frozen-plan.json'),
                      'plan_sha256': acquisition.sha(OUT / 'frozen-plan.json'),
                      'counts': report['counts'], 'candidate_regions': report['candidate_regions'],
                      'durations': report['durations'], 'budget': plan['budget']}, indent=2))


if __name__ == '__main__':
    main()
