"""Pure bounded stereo compositor; no rendering, effects, files or activation.

Gain-one, lineage and complete-tail metadata are typed caller declarations.
The retained PCM sum and every mute-subset bound are independently checked
arithmetic. They do not establish renderer history or listening acceptance.
"""
import array
import hashlib
import json
import math
import sys

from .band_plan import (RATE, MAX_FRAMES, canonical, check_cancel, integer,
                        is_sha, need, shape, sha256, validate_plan)

VERSION = 'band-mix-v1'
FLOAT_VERSION = 'band-float-stem-v1'
MAX_INPUT_BYTES = 128 * 1024**2
MAX_OUTPUT_BYTES = 32 * 1024**2
SCOPE = ('Caller-declared gain-one, complete-tail, lineage and per-instrument effects; '
         'retained PCM arithmetic verified; no renderer, physical release, source '
         'recognition, effects processing, native execution or listening acceptance.')
_FLOAT_FIELDS = ('version', 'format', 'sample_rate', 'origin_sample', 'frames',
                 'gain_one_declared', 'complete_tail_declared', 'effects', 'sha256')


def _frames_limit(value):
    need(integer(value, 1, MAX_FRAMES), 'Invalid bounded frame policy')


def _copy_samples(values, maximum, cancel):
    need(type(values) in (list, tuple, array.array)
         and (type(values) is not array.array or values.typecode in ('f', 'd')),
         'Ordinary interleaved float buffer required')
    length = len(values)
    need(0 < length <= 2*maximum and length % 2 == 0, 'Invalid bounded stereo buffer extent')
    copied = array.array('d')
    try:
        for index in range(length):
            if index % 2048 == 0:
                check_cancel(cancel)
            value = values[index]
            need(type(value) in (int, float), 'Float samples cannot contain booleans or objects')
            copied.append(float(value))
    except (OverflowError, IndexError) as error:
        raise ValueError('Float sample or buffer extent changed') from error
    need(all(math.isfinite(value) for value in copied), 'Nonfinite float sample')
    return copied


def _float_hash(values, cancel):
    digest = hashlib.sha256()
    for start in range(0, len(values), 8192):
        check_cancel(cancel)
        block = values[start:start+8192]
        if sys.byteorder != 'little':
            block.byteswap()
        digest.update(block.tobytes())
    return digest.hexdigest()


def float_stem(samples, *, origin_sample, effects=(), complete_tail_declared=True,
               sample_rate=RATE, max_frames=MAX_FRAMES, cancel=lambda: False):
    """Snapshot values as canonical stereo F64LE; declarations are not attestations."""
    _frames_limit(max_frames)
    need(type(sample_rate) is int and sample_rate == RATE
         and integer(origin_sample, 0, 2**53-1-max_frames), 'Absolute 44100 Hz stem clock required')
    need(complete_tail_declared is True, 'Complete-tail declaration required')
    need(type(effects) in (list, tuple) and len(effects) <= 8
         and all(type(item) is str and 0 < len(item) <= 128 for item in effects),
         'Bounded per-instrument effect declarations required')
    values = _copy_samples(samples, max_frames, cancel)
    return dict(version=FLOAT_VERSION, format='stereo_f64le', sample_rate=RATE,
                origin_sample=origin_sample, frames=len(values)//2,
                gain_one_declared=True, complete_tail_declared=True,
                effects=list(effects), sha256=_float_hash(values, cancel), samples=values)


def _metadata(value):
    shape(value, _FLOAT_FIELDS)
    need(value['version'] == FLOAT_VERSION and value['format'] == 'stereo_f64le'
         and type(value['sample_rate']) is int and value['sample_rate'] == RATE
         and integer(value['origin_sample'], 0, 2**53-1-MAX_FRAMES)
         and integer(value['frames'], 1, MAX_FRAMES)
         and value['gain_one_declared'] is True and value['complete_tail_declared'] is True
         and is_sha(value['sha256']), 'Invalid declared float-stem metadata')
    effects = value['effects']
    need(type(effects) is list and len(effects) <= 8
         and all(type(item) is str and 0 < len(item) <= 128 for item in effects),
         'Invalid per-instrument effect declarations')


def _gains(value, names):
    need(type(value) is dict and set(value) == set(names), 'Exactly one balance gain per part required')
    need(all(type(gain) in (int, float) and 0 <= gain <= 4 and math.isfinite(gain)
             for gain in value.values()), 'Balance gains must be finite numbers in zero through four')
    return {name: float(value[name]) for name in names}


def _active(value, names):
    need(type(value) is list and all(type(name) is str for name in value)
         and len(value) == len(set(value)) and set(value) <= set(names), 'Invalid default active part set')
    return [name for name in names if name in value]


def _clock(plan):
    end = max(plan['body_end_sample'], *(p['complete_end_sample'] for p in plan['parts']))
    origin = plan['origin_sample']
    return dict(sample_rate=RATE, origin_sample=origin, body_frames=plan['body_end_sample']-origin,
                tail_frames=end-plan['body_end_sample'], frames=end-origin)


def _headroom(value):
    need(type(value) in (int, float) and -24 <= value <= 0 and math.isfinite(value),
         'Headroom must be finite dBFS in minus24 through zero')
    return min(32767., 32768.*10**(value/20))


def _pcm_bytes(values):
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def _pcm_values(raw):
    values = array.array('h')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return values


def _file(raw, frames):
    return dict(bytes=len(raw), frames=frames, sha256=sha256(raw), format='stereo_s16le')


def _proof(parts, active, frames, cancel):
    positive_peak = negative_peak = 0
    master = array.array('h')
    names = list(parts)
    for start in range(0, frames*2, 8192):
        check_cancel(cancel)
        stop = min(frames*2, start+8192)
        blocks = {name: _pcm_values(parts[name][start*2:stop*2]) for name in names}
        for index in range(stop-start):
            values = [blocks[name][index] for name in names]
            positive = sum(v for v in values if v > 0)
            negative = sum(v for v in values if v < 0)
            need(positive <= 32767 and negative >= -32768, 'A retained mute subset exceeds PCM16')
            positive_peak = max(positive_peak, positive)
            negative_peak = min(negative_peak, negative)
            master.append(sum(blocks[name][index] for name in active))
    return (_pcm_bytes(master), dict(all_mute_subsets_and_gain_reductions_bounded=True,
            positive_peak_pcm=positive_peak, negative_peak_pcm=negative_peak,
            default_sum_exact=True, clipped_samples=0))


def compose(plan, stems, *, balance_gains=None, default_active=None, headroom_db=-3.,
            max_frames=MAX_FRAMES, max_input_bytes=MAX_INPUT_BYTES,
            max_output_bytes=MAX_OUTPUT_BYTES, cancel=lambda: False):
    """Return retained PCM bytes and a path-free receipt, without mutating inputs.

    Balance is supplied, never inferred. Common gain only attenuates; every
    possible mute subset, including initially muted parts, shares its ceiling.
    Effects must already be present in each declared per-instrument input.
    """
    plan = validate_plan(plan, cancel=cancel)
    _frames_limit(max_frames)
    need(integer(max_input_bytes, 16, MAX_INPUT_BYTES)
         and integer(max_output_bytes, 4, MAX_OUTPUT_BYTES), 'Invalid mix byte policy')
    names = [part['id'] for part in plan['parts']]
    need(type(stems) is dict and set(stems) == set(names), 'Exact planned float-stem roster required')
    clock = _clock(plan)
    frames = clock['frames']
    need(frames <= max_frames and (len(names)+1)*frames*4 <= max_output_bytes,
         'Complete padded mix exceeds output policy')
    gains = _gains({name:1. for name in names} if balance_gains is None else balance_gains, names)
    active = _active(names if default_active is None else default_active, names)
    ceiling = _headroom(headroom_db)
    prepared, input_bytes = [], 0
    # Refuse the complete roster's declarations and aggregate budget before
    # taking even the first float-buffer snapshot.
    for part in plan['parts']:
        check_cancel(cancel)
        name = part['id']
        supplied = stems[name]
        shape(supplied, (*_FLOAT_FIELDS, 'samples'))
        metadata = {key:supplied[key] for key in _FLOAT_FIELDS}
        _metadata(metadata)
        metadata['effects'] = list(metadata['effects'])
        need(metadata['origin_sample'] == plan['origin_sample']
             and metadata['origin_sample']+metadata['frames'] == part['complete_end_sample']
             and metadata['effects'] == part['effects'], 'Stem clock, declared tail or effects differ from plan')
        input_bytes += metadata['frames']*16
        samples = supplied['samples']
        need(type(samples) in (list,tuple,array.array)
             and (type(samples) is not array.array or samples.typecode in ('f','d'))
             and len(samples) == metadata['frames']*2, 'Declared float buffer extent differs')
        prepared.append((name, metadata, samples))
    need(input_bytes <= max_input_bytes, 'Float inputs exceed byte policy')
    snapshots, declarations = {}, {}
    for name, metadata, samples in prepared:
        check_cancel(cancel)
        values = _copy_samples(samples, metadata['frames'], cancel)
        need(len(values) == metadata['frames']*2 and _float_hash(values, cancel) == metadata['sha256'],
             'Float-stem bytes changed or extent differs')
        snapshots[name] = values
        declarations[name] = json.loads(canonical(metadata))
    bound = 0.
    for index in range(frames*2):
        if index % 2048 == 0:
            check_cancel(cancel)
        positive = negative = 0.
        for name in names:
            values = snapshots[name]
            value = values[index]*gains[name] if index < len(values) else 0.
            need(math.isfinite(value), 'Balanced float contribution is nonfinite')
            if value >= 0:
                positive += value
            else:
                negative += value
        need(math.isfinite(positive) and math.isfinite(negative), 'Float subset envelope is nonfinite')
        bound = max(bound, positive, -negative)
    # Each independent nearest-even rounding contributes at most half a unit.
    target = ceiling-len(names)*.5
    common = min(1., (target/32768)/bound) if bound else 1.
    need(math.isfinite(common) and common > 0, 'No representable common headroom gain')
    retained = {}
    for name in names:
        values, pcm = snapshots[name], array.array('h')
        for index in range(frames*2):
            if index % 2048 == 0:
                check_cancel(cancel)
            value = values[index]*gains[name]*common if index < len(values) else 0.
            sample = round(value*32768)
            need(-32768 <= sample <= 32767, 'Retained part exceeds PCM16')
            pcm.append(sample)
        retained[name] = _pcm_bytes(pcm)
    master, proof = _proof(retained, active, frames, cancel)
    need(max(proof['positive_peak_pcm'], -proof['negative_peak_pcm']) <= ceiling+1e-9,
         'Retained mix exceeds declared headroom')
    receipt = dict(version=VERSION, plan_sha256=sha256(canonical(plan)), clock=clock,
        musical_role_independence='not_evaluated',
        balance_gains=gains, default_active=active, headroom_db=float(headroom_db), common_gain=common,
        input_declarations=declarations, parts={name:_file(retained[name],frames) for name in names},
        master=_file(master,frames), proof=proof, scope=SCOPE)
    result = dict(version=VERSION, receipt=receipt, receipt_sha256=sha256(canonical(receipt)),
                  parts=retained, master=master)
    verify_mix(plan,result,expected_receipt_sha256=result['receipt_sha256'],
               max_output_bytes=max_output_bytes,cancel=cancel)
    return result


def verify_mix(plan, bundle, *, expected_receipt_sha256,
               max_output_bytes=MAX_OUTPUT_BYTES, cancel=lambda: False):
    """Verify exact committed PCM identity/default sum and all subset bounds."""
    plan = validate_plan(plan,cancel=cancel)
    need(is_sha(expected_receipt_sha256) and integer(max_output_bytes,4,MAX_OUTPUT_BYTES),
         'Trusted receipt identity and bounded output policy required')
    shape(bundle, ('version','receipt','receipt_sha256','parts','master'))
    need(bundle['version'] == VERSION and bundle['receipt_sha256'] == expected_receipt_sha256,
         'Committed mix receipt identity differs')
    receipt = bundle['receipt']
    shape(receipt, ('version','plan_sha256','clock','musical_role_independence','balance_gains','default_active','headroom_db',
                    'common_gain','input_declarations','parts','master','proof','scope'))
    names = [part['id'] for part in plan['parts']]
    need(receipt['version'] == VERSION and receipt['plan_sha256'] == sha256(canonical(plan))
         and canonical(receipt['clock']) == canonical(_clock(plan)) and receipt['scope'] == SCOPE,
         'Plan, clock or declared mix scope differs')
    need(type(receipt['musical_role_independence']) is str
         and receipt['musical_role_independence'] == 'not_evaluated',
         'Musical role independence is not evaluated by the mixer')
    _gains(receipt['balance_gains'],names)
    active = _active(receipt['default_active'],names)
    need(receipt['default_active'] == active, 'Default mix order differs from plan')
    ceiling = _headroom(receipt['headroom_db'])
    need(type(receipt['common_gain']) in (int,float) and 0 < receipt['common_gain'] <= 1
         and math.isfinite(receipt['common_gain']), 'Invalid common gain')
    for rows in (bundle['parts'],receipt['parts'],receipt['input_declarations']):
        need(type(rows) is dict and set(rows) == set(names), 'Saved mix part roster differs')
    frames = _clock(plan)['frames']
    need((len(names)+1)*frames*4 <= max_output_bytes, 'Saved PCM exceeds byte policy')
    for part in plan['parts']:
        check_cancel(cancel)
        name = part['id']
        declaration = receipt['input_declarations'][name]
        _metadata(declaration)
        need(declaration['origin_sample'] == plan['origin_sample']
             and declaration['origin_sample']+declaration['frames'] == part['complete_end_sample']
             and declaration['effects'] == part['effects'], 'Saved input declaration differs')
        raw = bundle['parts'][name]
        need(type(raw) is bytes and len(raw) == frames*4
             and canonical(receipt['parts'][name]) == canonical(_file(raw,frames)), 'Retained part PCM identity differs')
        padding = raw[declaration['frames']*4:]
        need(not any(padding), 'Saved complete-tail padding is not silent')
    raw = bundle['master']
    need(type(raw) is bytes and len(raw) == frames*4 and canonical(receipt['master']) == canonical(_file(raw,frames)),
         'Combined PCM identity differs')
    expected_master, proof = _proof(bundle['parts'],active,frames,cancel)
    need(raw == expected_master and canonical(receipt['proof']) == canonical(proof)
         and max(proof['positive_peak_pcm'],-proof['negative_peak_pcm']) <= ceiling+1e-9,
         'Saved default sum or mute-subset proof differs')
    encoded = canonical(receipt)
    need(len(encoded) <= 65536 and sha256(encoded) == expected_receipt_sha256,
         'Saved mix receipt bytes differ')
    check_cancel(cancel)
    return json.loads(encoded)
