"""Measure dry research renders and prepare separate level-matched copies silently.

Existing portable FFmpeg only. Constant gain preserves the original dynamics;
no limiter, compressor, reverb, source stem, playback or overwrite is used.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import wave

import numpy as np

RESEARCH = Path(__file__).resolve().parent
FFMPEG = RESEARCH / 'decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def levels(path):
    with wave.open(str(path)) as wav:
        frames, rate, channels = wav.getnframes(), wav.getframerate(), wav.getnchannels()
        if wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE' or frames > 30 * rate:
            raise ValueError('Expected bounded PCM16 render')
        x = np.frombuffer(wav.readframes(frames), dtype='<i2').astype(np.float64) / 32768.0
    peak = float(abs(x).max())
    rms = float(np.sqrt(np.mean(x * x)))
    return {'path': str(path.resolve()), 'sha256': sha(path), 'frames': frames,
            'sample_rate': rate, 'channels': channels,
            'sample_peak': peak, 'sample_peak_dbfs': 20 * math.log10(peak),
            'rms': rms, 'rms_dbfs': 20 * math.log10(rms),
            'full_scale_samples': int(np.sum(abs(x) >= 32767 / 32768))}


def loudness(path):
    command = [str(FFMPEG), '-hide_banner', '-nostdin', '-i', str(path), '-af',
               'loudnorm=I=-18:TP=-1:LRA=11:print_format=json', '-f', 'null', '-']
    process = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    # FFmpeg can append its final progress summary after the filter's JSON.
    payload, _ = json.JSONDecoder().raw_decode(process.stderr[process.stderr.rfind('{'):].strip())
    return {key: float(payload[key]) for key in ('input_i', 'input_tp', 'input_lra', 'input_thresh')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(RESEARCH):
        raise ValueError('Use an ignored research result directory')
    evidence_path = directory / 'listening-levels.json'
    if evidence_path.exists():
        raise FileExistsError(evidence_path)
    items = {}
    for label in ('solo', 'duet'):
        source = directory / (label + '.wav')
        destination = directory / (label + '-matched.wav')
        if destination.exists():
            raise FileExistsError(destination)
        items[label] = {'original': levels(source), 'original_loudness': loudness(source)}
    # A single target for both tracks, lowered together if true-peak headroom requires it.
    target = min([-18.0] + [item['original_loudness']['input_i'] - 1.0
                           - item['original_loudness']['input_tp'] for item in items.values()])
    for label, item in items.items():
        source = directory / (label + '.wav')
        destination = directory / (label + '-matched.wav')
        temporary = directory / (label + '-matched.partial.wav')
        if temporary.exists():
            raise FileExistsError(temporary)
        gain = target - item['original_loudness']['input_i']
        command = [str(FFMPEG), '-hide_banner', '-v', 'error', '-nostdin', '-n', '-i',
                   str(source), '-af', f'volume={gain:.6f}dB', '-ar', '44100', '-c:a',
                   'pcm_s16le', str(temporary)]
        subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
        measured = levels(temporary)
        measured_loudness = loudness(temporary)
        if any(measured[key] != item['original'][key] for key in ('frames', 'sample_rate', 'channels')):
            raise AssertionError('Listening preparation changed timing or channels')
        if measured['full_scale_samples'] or measured_loudness['input_tp'] > -0.9:
            raise AssertionError('Insufficient true-peak headroom')
        if abs(measured_loudness['input_i'] - target) > 0.15:
            raise AssertionError('Constant-gain loudness target not reached')
        if sha(source) != item['original']['sha256']:
            raise AssertionError('Original render changed')
        temporary.rename(destination)
        measured['path'] = str(destination)
        item.update({'constant_gain_db': gain, 'matched': measured,
                     'matched_loudness': measured_loudness, 'command': command})
    evidence = {'method': 'FFmpeg loudnorm input EBU R128 loudness/true-peak measurement; separate constant gain PCM16 copies; full-file interleaved sample RMS',
                'ffmpeg': str(FFMPEG), 'ffmpeg_sha256': sha(FFMPEG),
                'target_lufs': target, 'maximum_true_peak_dbtp': -1.0,
                'no_playback': True, 'originals_unchanged': True,
                'no_source_stems_or_effects': True,
                'limitations': ['Short diagnostic excerpt; no listening or naturalness judgment',
                                'Loudness matching does not assess fidelity or articulation'],
                'renders': items, 'script_sha256': sha(Path(__file__))}
    with evidence_path.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, indent=2, allow_nan=False)
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
