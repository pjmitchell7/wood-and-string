"""Owned pure-CPU tone stage. No synthesis, analysis, external executable or playback."""
import array
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import uuid

from . import electric_tone_stream as stream
from .electric_tone import ToneOverload
from .native_trial_process import _assert_owned_worker
from render_evidence import _held, _plain, validate_reference, _sha

VERSION = 'electric-tone-stage-v1'
MAX_RECEIPT = 65536
MAX_RAW = stream.MAX_OUTPUT_FRAMES*16
MAX_WAV = 44+stream.MAX_OUTPUT_FRAMES*4
FILES = ('processed.f64','processed.wav','receipt.json')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def header(frames):
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF',36+frames*4,b'WAVE',b'fmt ',16,
                       1,2,44100,176400,4,16,b'data',frames*4)


def work_path(root, identity):
    if type(identity) is not str or str(uuid.UUID(identity)) != identity: raise ValueError('Invalid tone job identity')
    stage = _plain(Path(root)/'staging', directory=True)
    path = stage/(identity+'.tone')
    if path.exists(): _plain(path, directory=True)
    if path.is_symlink(): raise ValueError('Linked tone work directory')
    return path


def read_pcm(stack, path, expected, cancel=lambda: False):
    if (type(expected) is not dict or set(expected) != {'sha256','bytes'} or not _sha(expected['sha256'])
            or type(expected['bytes']) is not int or not 48 <= expected['bytes'] <= 44+stream.MAX_FRAMES*4
            or (expected['bytes']-44)%4): raise ValueError('Invalid dry WAV identity')
    handle = _held(stack, path, 44+stream.MAX_FRAMES*4)
    if os.fstat(handle.fileno()).st_size != expected['bytes']: raise ValueError('Dry WAV size differs')
    first = handle.read(44); frames = (expected['bytes']-44)//4
    if first != header(frames): raise ValueError('Expected exact dry stereo PCM16 WAV')
    checksum = hashlib.sha256(first); samples = array.array('d')
    while data := handle.read(65536):
        if cancel(): raise InterruptedError('Tone cancelled')
        checksum.update(data)
        pcm = array.array('h'); pcm.frombytes(data)
        if sys.byteorder != 'little': pcm.byteswap()
        samples.extend(value/32768 for value in pcm)
    if checksum.hexdigest() != expected['sha256']: raise ValueError('Dry WAV hash differs')
    return samples


def write_outputs(folder, samples, metadata, binding, cancel):
    raw_hash, wav_hash = hashlib.sha256(), hashlib.sha256()
    frames = len(samples)//2; peak = 0; residual = 0.
    with (folder/'processed.f64').open('xb') as raw, (folder/'processed.wav').open('xb') as wav:
        first = header(frames); wav.write(first); wav_hash.update(first)
        for start in range(0, len(samples), 8192):
            if cancel(): raise InterruptedError('Tone cancelled')
            values = array.array('d', samples[start:start+8192])
            pcm = array.array('h')
            for value in values:
                number = round(value*32768)
                if not -32767 <= number <= 32767: raise ValueError('Tone PCM overload; no clipping')
                residual = max(residual, abs(number-value*32768)); peak = max(peak,abs(number)); pcm.append(number)
            if sys.byteorder != 'little': values.byteswap(); pcm.byteswap()
            data=values.tobytes();raw.write(data);raw_hash.update(data)
            data=pcm.tobytes();wav.write(data);wav_hash.update(data)
        for handle in (raw,wav): handle.flush();os.fsync(handle.fileno())
    receipt=dict(version=VERSION, binding=binding, processing=metadata,
        raw={'sha256':raw_hash.hexdigest(),'bytes':frames*16},
        wav={'sha256':wav_hash.hexdigest(),'bytes':44+frames*4},
        pcm_peak=peak/32768, maximum_quantization_residual_lsb=residual,
        clipped_samples=0, musical_acceptance=False)
    data=canonical(receipt)
    if len(data)>MAX_RECEIPT: raise ValueError('Tone receipt exceeds bound')
    with (folder/'receipt.json').open('xb') as handle: handle.write(data);handle.flush();os.fsync(handle.fileno())
    return {'state':'processed','receipt':{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}}


def run(request, emit):
    _assert_owned_worker()
    if type(request) is not dict or set(request) != {'root','job_id','input','settings','source_origin_sample','dry_evidence','source_commands_sha256'}:
        raise ValueError('Unexpected tone worker request')
    validate_reference(request['dry_evidence'])
    if not _sha(request['source_commands_sha256']): raise ValueError('Invalid dry commands digest')
    folder=work_path(request['root'],request['job_id'])
    if folder.exists(): raise FileExistsError('Preserve occupied tone attempt')
    folder.mkdir()
    began=time.monotonic()
    cancel=lambda:time.monotonic()-began>55 or (folder.parent/(request['job_id']+'.cancel')).exists()
    binding={key:request[key] for key in ('input','settings','source_origin_sample','dry_evidence','source_commands_sha256')}
    with ExitStack() as stack:
        samples=read_pcm(stack,folder.parent/(request['job_id']+'.wav'),request['input'],cancel)
        emit({'progress':.1})
        try:
            result=stream.process(samples,settings=request['settings'],source_origin_sample=request['source_origin_sample'],cancel=cancel)
        except ToneOverload as error:
            return {'state':'refused','refusal':error.metadata}
        emit({'progress':.85})
        return write_outputs(folder,result['samples'],result['metadata'],binding,cancel)
