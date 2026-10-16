"""Frozen, silent, gain-only comparison of the rejected and repaired renders.

Uses the exact saved arrangements and existing portable FFmpeg. No analysis,
playback, source-stem mixing, new dependency or change to production settings.
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import wave

APP = Path(__file__).resolve().parents[2]
ROOT = APP / 'audio/research'
OLD = ROOT / 'app-preview-003'
FFMPEG = ROOT / 'decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
RATE = 44100
PASSAGE = 264600


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def pcm(path):
    with wave.open(str(path)) as wav:
        if (wav.getcomptype() != 'NONE' or wav.getsampwidth() != 2
                or wav.getframerate() != RATE or wav.getnchannels() != 2
                or not PASSAGE <= wav.getnframes() <= 30 * RATE):
            raise ValueError('Expected bounded stereo PCM16 at 44100 Hz')
        frames = wav.getnframes()
        data = array.array('h', wav.readframes(frames))
    if sys.byteorder != 'little':
        data.byteswap()
    if len(data) != frames * 2:
        raise ValueError('Truncated WAV')
    return data, dict(frames=frames, sample_rate=RATE, channels=2)


def measure(path, passage=False):
    prefix = f'atrim=end_sample={PASSAGE},' if passage else ''
    command = [str(FFMPEG), '-hide_banner', '-nostdin', '-threads', '1',
               '-i', str(path), '-af', prefix +
               'loudnorm=I=-18:TP=-1:LRA=11:print_format=json', '-f', 'null', '-']
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    report, _ = json.JSONDecoder().raw_decode(result.stderr[result.stderr.rfind('{'):].strip())
    values = {k: float(report[k]) for k in ('input_i', 'input_tp', 'input_lra', 'input_thresh')}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('Nonfinite loudness or silent input')
    return values


def run(request, emit):
    from audio.render import render, RENDER_VERSION
    out = Path(request['output'])
    if sha(out / 'frozen-plan.json') != request['plan_sha256']:
        raise RuntimeError('Frozen plan changed')
    plan = json.loads((out / 'frozen-plan.json').read_text())
    for item in plan['inputs']:
        if sha(APP / item['path']) != item['sha256']:
            raise RuntimeError('Frozen input changed: ' + item['path'])
    if RENDER_VERSION != plan['renderer_version']:
        raise RuntimeError('Renderer version changed')
    began = time.monotonic()
    old = json.loads((OLD / 'evidence.json').read_text())
    originals = {'original': OLD / 'original-passage.wav',
                 'old-solo': OLD / 'solo.wav', 'old-duet': OLD / 'duet.wav'}
    renders = {}
    # Exact saved commands, including adaptations and original source evidence.
    for performers, label in ((1, 'solo'), (2, 'duet')):
        job = next(j for j in old['cases'] if j['state'] == 'complete'
                   and j['request'].get('performers') == performers)
        arrangement = job['result']['arrangement']
        assert arrangement['render_origin_sample'] == 11025
        assert len(arrangement['notes']) == (36 if performers == 1 else 37)
        assert sha(OLD / (label + '.wav')) == job['result']['asset']['sha256']
        before = json.dumps(arrangement, sort_keys=True)
        save(out / (label + '-arrangement.json'), arrangement)
        path = out / ('new-' + label + '.wav')
        metrics = render(arrangement, path)
        assert json.dumps(arrangement, sort_keys=True) == before
        assert metrics['processing_mode'] == 'freewheeling'
        assert metrics['renderer_version'] == plan['renderer_version']
        new_pcm, new_geometry = pcm(path)
        old_pcm, old_geometry = pcm(OLD / (label + '.wav'))
        assert new_geometry == old_geometry
        originals['new-' + label] = path
        renders[label] = dict(metrics=metrics, sha256=sha(path), notes=len(arrangement['notes']),
                             old_new_exact_pcm=new_pcm == old_pcm,
                             differing_pcm_samples=sum(a != b for a, b in zip(new_pcm, old_pcm)),
                             maximum_pcm_difference=max(abs(a - b) for a, b in zip(new_pcm, old_pcm)))
        emit(dict(stage='rendered', label=label))
    # Independently retain the original's exact sample-origin relation.
    with wave.open(str(ROOT / 'mixed/mixed.wav')) as source:
        source.setpos(11025)
        expected = source.readframes(PASSAGE)
    with wave.open(str(originals['original'])) as reference:
        assert reference.getnframes() == PASSAGE
        assert reference.readframes(PASSAGE) == expected
    entries = {}
    for label, path in originals.items():
        data, geometry = pcm(path)
        entries[label] = dict(source_path=str(path.relative_to(APP)), source_sha256=sha(path),
                             geometry=geometry, passage_loudness=measure(path, True),
                             whole_loudness=measure(path), sample_peak=max(map(abs, data)) / 32768,
                             rms=math.sqrt(sum(x * x for x in data) / len(data)) / 32768)
    target = min([plan['maximum_target_lufs']] + [
        x['passage_loudness']['input_i'] + plan['design_true_peak_dbtp'] -
        x['whole_loudness']['input_tp'] for x in entries.values()])
    for label, item in entries.items():
        path = originals[label]
        destination = out / (label + '-matched.wav')
        gain_db = target - item['passage_loudness']['input_i']
        # The formatted value is also the one used by the gain-only verifier.
        gain_text = f'{gain_db:.9f}'
        command = [str(FFMPEG), '-hide_banner', '-v', 'error', '-nostdin', '-n',
                   '-threads', '1', '-i', str(path), '-map_metadata', '-1', '-af',
                   f'volume={gain_text}dB:precision=double:eval=once:replaygain=drop',
                   '-c:a', 'pcm_s16le', str(destination)]
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        before, geometry = pcm(path)
        after, matched_geometry = pcm(destination)
        assert geometry == matched_geometry
        gain = 10 ** (float(gain_text) / 20)
        residual = max(abs(b - a * gain) for a, b in zip(before, after))
        assert residual <= plan['maximum_gain_residual_lsb']
        assert max(map(abs, after)) < 32767
        passage = measure(destination, True)
        whole = measure(destination)
        assert abs(passage['input_i'] - target) <= plan['maximum_lufs_error']
        assert whole['input_tp'] <= plan['verified_true_peak_ceiling_dbtp']
        assert sha(path) == item['source_sha256']
        item.update(constant_gain_db=float(gain_text), matched_file=destination.name,
                    matched_sha256=sha(destination), matched_geometry=matched_geometry,
                    matched_passage_loudness=passage, matched_whole_loudness=whole,
                    gain_residual_max_lsb=residual, clipped_samples=0,
                    matched_sample_peak=max(map(abs, after)) / 32768,
                    matched_rms=math.sqrt(sum(x * x for x in after) / len(after)) / 32768)
        emit(dict(stage='level-matched', label=label))
    for item in plan['inputs']:
        assert sha(APP / item['path']) == item['sha256']
    evidence = dict(target_lufs=target, entries=entries, renders=renders,
                    all_frozen_inputs_unchanged=True, original_slice_exact=True,
                    audio_played=False, elapsed_seconds=time.monotonic() - began,
                    musical_acceptance='Not assessed; earlier user rejection remains in force',
                    limits=plan['limits'])
    save(out / 'evidence.json', evidence)
    return dict(output=str(out), target_lufs=target, files=5, no_playback=True)


def main():
    from audio.render import ASSETS, RENDER_VERSION
    from audio.managed_process import run_worker
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='renderer-listening-001')
    name = parser.parse_args().name
    if not re.fullmatch(r'renderer-listening-\d{3}', name):
        raise ValueError('Fresh numbered comparison required')
    allowance = 32 * 1024 ** 2
    used = sum(p.stat().st_size for tree in (ROOT, APP / 'audio/vendor/analysis')
               for p in tree.rglob('*') if p.is_file())
    free = shutil.disk_usage(ROOT).free
    if used + allowance > 4 * 1024 ** 3 or free < 8 * 1024 ** 3 + allowance:
        raise RuntimeError('Research budget or disk reserve exceeded')
    inputs = [Path(__file__), APP / 'audio/render.py', APP / 'audio/managed_process.py',
              APP / 'audio/vendor/sfizz/bin/Release/sfizz.dll', ASSETS / 'meta-steel-guitar.sfz',
              FFMPEG, ROOT / 'mixed/mixed.wav']
    inputs += [OLD / name for name in ('evidence.json', 'original-passage.wav', 'solo.wav', 'duet.wav')]
    inputs += sorted((ASSETS / 'ogg').rglob('*.ogg'))
    out = ROOT / name
    out.mkdir(exist_ok=False)
    plan = dict(frozen_before_render=True, renderer_version=RENDER_VERSION,
                inputs=[dict(path=str(p.relative_to(APP)), sha256=sha(p)) for p in inputs],
                source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=APP, text=True).strip(),
                source_origin=11025, passage_frames=PASSAGE, sample_rate=RATE,
                fixed_arrangements='app-preview-003 exact saved solo/duet, 36/37 notes',
                maximum_target_lufs=-18.0, design_true_peak_dbtp=-1.2,
                verified_true_peak_ceiling_dbtp=-1.0, maximum_lufs_error=.15,
                maximum_gain_residual_lsb=1.0,
                measurement='FFmpeg loudnorm input readings only; six-second LUFS and full-file true peak',
                processing='One constant gain per complete file; no timing/effects/limiting change',
                budget_bytes=allowance, research_bytes_before=used, free_disk_before=free,
                wall_seconds=120, sampled_memory_bytes=512 * 1024 ** 2,
                memory_scope='Sampled managed Python worker only; excludes FFmpeg descendant working sets',
                limits=['Short-excerpt gated loudness is not a perceptual equality guarantee',
                        'Original mix and acoustic arrangement have different instrumentation',
                        'Original is six seconds; guitar files retain the complete release tail',
                        'No note truth, transcription improvement or musical acceptance is inferred'])
    for source in inputs[:3]:
        (out / ('frozen-' + source.name)).write_bytes(source.read_bytes())
    save(out / 'frozen-plan.json', plan)
    try:
        result = run_worker('audio.research.prepare_renderer_listening',
                            dict(output=str(out), plan_sha256=sha(out / 'frozen-plan.json')),
                            wall_seconds=plan['wall_seconds'], memory_bytes=plan['sampled_memory_bytes'],
                            progress=lambda event: print(json.dumps(event), flush=True))
        save(out / 'run.json', result)
        print(json.dumps(result), flush=True)
    except Exception as error:
        save(out / 'failed.json', dict(type=type(error).__name__, error=str(error), retained=True))
        raise


if __name__ == '__main__':
    main()
