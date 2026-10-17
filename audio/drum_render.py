"""Fixed basic-kit adapter. Source-clock scheduling, no transcription or app wiring."""
import array, hashlib, math, os, stat, sys, uuid, wave
from contextlib import ExitStack
from pathlib import Path

VERSION = 'virtuosity-basic-kit-render-v1'
RATE, BLOCK, GUARD = 44100, 256, 4410
SUPPORT = {36: 109593, 38: 90913, 42: 34444}
PRELOAD = 587098
MAP_SHA = '2230b2e37efa6faab2a77759e7389f7a335ebed406fd0303b59262323b825fd0'
MAP_BYTES = 11395


def validate(request):
  """Copy ordered commands without changing gates."""
  if type(request) is not dict or set(request) != {'sample_rate', 'render_origin_sample', 'events'}:
    raise ValueError('Exact basic-kit request required')
  sr, origin, events = (request[k] for k in ('sample_rate', 'render_origin_sample', 'events'))
  if type(sr) is not int or sr != RATE or type(origin) is not int or not 0 <= origin <= 2**53-1-30*RATE:
    raise ValueError('Absolute 44100 Hz clock required')
  if type(events) is not list or not 1 <= len(events) <= 4000:
    raise ValueError('Expected 1..4000 events')
  copied, identities, attacks, previous = [], set(), set(), None
  for event in events:
    if type(event) is not dict or set(event) != {'id', 'start', 'end', 'note', 'velocity'}:
      raise ValueError('Exact basic-kit event required')
    row = dict(event)
    if any(type(row[k]) is not int for k in ('start', 'end', 'note', 'velocity')):
      raise ValueError('Integer event values required')
    if row['note'] not in SUPPORT:
      raise ValueError('Only keys36,38,42 supported')
    if not origin <= row['start'] < row['end'] <= origin+30*RATE or not 1 <= row['velocity'] <= 127:
      raise ValueError('Invalid gate or velocity')
    identity = row['id']
    if type(identity) is not str or not 1 <= len(identity) <= 128 or identity in identities:
      raise ValueError('Unique bounded IDs required')
    attack = (row['start'], row['note'])
    order = (*attack, identity)
    if attack in attacks or (previous is not None and order < previous):
      raise ValueError('Unordered events or duplicate attack key')
    previous = order
    identities.add(identity); attacks.add(attack); copied.append(row)
  return {'sample_rate': sr, 'render_origin_sample': origin, 'events': copied}


def extent(request):
  value = validate(request); origin = value['render_origin_sample']
  return max(max(e['end'], e['start']+SUPPORT[e['note']]) for e in value['events'])-origin+GUARD


def commands(request):
  value = validate(request); origin = value['render_origin_sample']; queue = []
  for e in value['events']:
    queue.extend(((e['start']-origin, 1, e['note'], e['id'], e['velocity']),
            (e['end']-origin, 0, e['note'], e['id'], 0)))
  return sorted(queue)  # Offs, ons, key, ID.


def _check(cancel):
  if cancel():
    raise InterruptedError('Drum render cancelled')


def blocks(engine, synth, request, *, cancel=lambda: False):
  """Gain-1 stereo F32 using one persistent synth."""
  value = validate(request); queue = commands(value); frames = extent(value); index = 0; C = engine.C
  buffers = [(C.c_float*BLOCK)(), (C.c_float*BLOCK)()]
  channels = (C.POINTER(C.c_float)*2)(*buffers)
  for start in range(0, frames, BLOCK):
    _check(cancel); count = min(BLOCK, frames-start)
    while index < len(queue) and queue[index][0] < start+count:
      at, on, note, _, velocity = queue[index]
      send = engine.api.sfizz_send_note_on if on else engine.api.sfizz_send_note_off
      send(synth, at-start, note, velocity); index += 1
    engine.api.sfizz_render_block(synth, channels, 2, count)
    values = array.array('f', (buffers[c][i] for i in range(count) for c in (0, 1)))
    if not all(math.isfinite(v) for v in values):
      raise ValueError('Nonfinite native sample')
    _check(cancel)
    yield start, values
  if index != len(queue):
    raise ValueError('Unconsumed command')


def _native(cancel):
  # Validation precedes map/native loading.
  from .drum_probe import Engine, ROOT
  _check(cancel)
  with (ROOT/'canonical.sfz').open('rb') as source: raw = source.read(MAP_BYTES+1)
  if len(raw) != MAP_BYTES or hashlib.sha256(raw).hexdigest() != MAP_SHA:
    raise ValueError('Canonical map differs')
  return Engine(lambda: _check(cancel), PRELOAD), raw.decode('utf-8')


def _same(path, handle, links=1):
  left, right = path.lstat(), os.fstat(handle.fileno())
  if (not stat.S_ISREG(left.st_mode) or getattr(left, 'st_file_attributes', 0) & 0x400
      or (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino) or left.st_nlink != links):
    raise ValueError('Temporary file association differs')


def _remove(path, identity):
  try:
    current = path.lstat()
  except FileNotFoundError:
    return
  if (stat.S_ISREG(current.st_mode) and not getattr(current, 'st_file_attributes', 0) & 0x400
      and (current.st_dev, current.st_ino) == identity):
    path.unlink()


def render(request, output, *, cancel=lambda: False, progress=lambda _: None, level_policy='peak'):
  """Caller owns admission, bank/runtime closure and job commit."""
  value = validate(request); frames = extent(value)
  if level_policy not in ('peak', 'legacy_fixed'):
    raise ValueError('Unknown level policy')
  _check(cancel)
  output = Path(output)
  if os.name != 'nt':
    raise RuntimeError('Windows held-file writer required')
  if os.path.lexists(output):
    raise FileExistsError('Refusing to overwrite render')
  if not output.parent.is_dir() or getattr(output.parent.lstat(), 'st_file_attributes', 0) & 0x400:
    raise ValueError('Ordinary output directory required')
  token = uuid.uuid4().hex
  spool = output.with_name(output.name+'.'+token+'.f32.partial')
  partial = output.with_name(output.name+'.'+token+'.wav.partial')
  owned = []; peak = energy = tail_energy = 0.
  try:
    # Keep original CRT objects open, denying Windows deletion/replacement.
    with ExitStack() as stack:
      streams = []
      for path in (spool, partial):
        handle = stack.enter_context(path.open('x+b')); streams.append(handle)
        info = os.fstat(handle.fileno()); owned.append((path, (info.st_dev, info.st_ino)))
        _same(path, handle)
      floats, pcm = streams
      engine, mapping = _native(cancel)
      with engine.load(mapping) as synth:
        count = 0
        for start, values in blocks(engine, synth, value, cancel=cancel):
          if start != count:
            raise ValueError('Noncontiguous output')
          peak = max(peak, max(map(abs, values)))
          energy += sum(float(v)*v for v in values)
          tail = values[max(0, (frames-GUARD-start)*2):]
          tail_energy += sum(float(v)*v for v in tail)
          if sys.byteorder != 'little': values.byteswap()
          floats.write(values.tobytes()); count += len(values)//2
          progress(.8*count/frames)
        if count != frames:
          raise ValueError('Incomplete output')
      floats.flush(); os.fsync(floats.fileno()); _same(spool, floats); _same(partial, pcm)
      from .render import _write_pcm16
      levels = _write_pcm16(spool, partial, RATE, peak, level_policy, cancel, progress)
      pcm.flush(); os.fsync(pcm.fileno()); _same(spool, floats); _same(partial, pcm)
      if levels['frames'] != frames or os.fstat(pcm.fileno()).st_size != 44+4*frames:
        raise ValueError('PCM extent differs')
      with wave.open(str(partial), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes(), wav.getcomptype()) != (2, 2, RATE, frames, 'NONE'):
          raise ValueError('PCM header differs')
      progress(1.); _check(cancel); _same(partial, pcm)
      os.link(partial, output)  # Atomic create-new publication.
    return {**levels, 'finite': True, 'sample_rate': RATE, 'source_origin_sample': value['render_origin_sample'],
        'renderer_version': VERSION, 'map_sha256': MAP_SHA,
        'support_ceiling_frames_by_key': dict(SUPPORT), 'guard_frames': GUARD,
        'raw_rms': math.sqrt(energy/(frames*2)), 'final_guard_rms': math.sqrt(tail_energy/(GUARD*2)),
        'gain': 10**(-3/20)/peak if level_policy == 'peak' else .25, 'native_loads': engine.loads}
  finally:
    for path, identity in owned:
      _remove(path, identity)
