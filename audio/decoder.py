"""Offline, local-file decoder. Canonical coordinates describe decoded audio.

Call inside the managed worker: cooperative deadlines cannot interrupt a native
decoder call. No PyAV import occurs until a validated local file is opened.
"""
import hashlib
from fractions import Fraction
import os
from pathlib import Path
import re
import time
import wave

RATE = 44100
DECODER_VERSION = 'pyav19-canonical-v2'
CODECS = {'mp3', 'mp3float', 'aac', 'flac', 'opus', 'vorbis', 'pcm_s16le',
          'pcm_s24le', 'pcm_s32le', 'pcm_f32le', 'pcm_f64le', 'pcm_u8'}


def _local_path(value):
    text = os.fspath(value)
    if '://' in text or text.startswith(('\\\\', '//')):
        raise ValueError('Decoder accepts local regular files only')
    path = Path(text).resolve(strict=True)
    if str(path).startswith(('\\\\', '//')) or not path.is_file():
        raise ValueError('Decoder accepts local regular files only')
    # Reject Windows alternate streams and URL-like scheme paths.
    if ':' in str(path)[2:]:
        raise ValueError('Alternate streams and protocol paths are unsupported')
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) not in (2, 3, 6):
            raise ValueError('Network and unknown drive types are unsupported')
    return path


def _format(header):
    if header[:4] == b'RIFF' and header[8:12] == b'WAVE': return 'wav'
    if header[:4] == b'fLaC': return 'flac'
    if header[4:8] == b'ftyp': return 'mov'
    if header[:4] == b'\x1aE\xdf\xa3': return 'matroska'
    if len(header) >= 2 and header[0] == 255 and header[1] & 246 == 240:
        return 'aac'  # ADTS sync + zero layer bits; distinct from MPEG audio.
    if header[:3] == b'ID3' or (len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224):
        return 'mp3'
    raise ValueError('Unsupported container; playlists and external references are not accepted')


def decode_passage(source, output, start_sample, frame_count, *, cancel=lambda: False,
                   timeout_seconds=120, expected_sha256=None):
    """Write exactly the requested canonical stereo PCM16 passage, or fail.

    Zero is the first decoder-emitted sample after codec delay/skip processing,
    resampled to 44.1 kHz. PTS are recorded, never used to guess original PCM
    origin. Sequential decode preserves resampler state across passage edges.
    """
    if type(start_sample) is not int or not 0 <= start_sample <= RATE*7200:
        raise ValueError('Start must be a canonical sample within two hours')
    if type(frame_count) is not int or not 1 <= frame_count <= RATE*30:
        raise ValueError('Passage must contain between one sample and 30 seconds')
    if not 0 < timeout_seconds <= 300:
        raise ValueError('Decoder deadline must be positive and at most 300 seconds')
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_sha256)):
        raise ValueError('Invalid expected original hash')
    began = time.monotonic()
    def check():
        if cancel(): raise InterruptedError('Decode cancelled')
        if time.monotonic()-began > timeout_seconds: raise TimeoutError('Decode deadline exceeded')
    check()
    source = _local_path(source)
    output = Path(output)
    partial = output.with_suffix(output.suffix+'.decode.partial')
    if source == output.resolve() or output.exists() or partial.exists():
        raise FileExistsError('Refusing to overwrite source or decoded output')
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open('rb') as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > 2*1024**3: raise ValueError('Source exceeds 2 GiB decoder limit')
            fmt = _format(handle.read(32)); handle.seek(0)
            digest = hashlib.sha256()
            while data := handle.read(1024*1024):
                check(); digest.update(data)
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise ValueError('Original audio failed verification before decoding')
            handle.seek(0)
            import av
            if av.__version__ != '19.0.1': raise RuntimeError('Pinned PyAV 19.0.1 is required')
            # File-like I/O supplies bytes; no filename/URL is handed to FFmpeg.
            # Empty protocol whitelist blocks auxiliary URL opens. MOV external
            # data references and absolute sidecar paths stay disabled explicitly.
            with av.open(handle, mode='r', format=fmt, options={
                'protocol_whitelist': '', 'enable_drefs': '0', 'use_absolute_path': '0',
                'codec_whitelist': ','.join(sorted(CODECS)),
                'probesize': '1048576', 'analyzeduration': '5000000',
            }) as container:
                if not container.streams.audio: raise ValueError('Source contains no audio stream')
                stream = container.streams.audio[0]
                codec = stream.codec_context
                if codec.name not in CODECS: raise ValueError('Unsupported audio codec')
                codec.thread_count = 1
                if codec.sample_rate < 8000 or codec.sample_rate > 192000:
                    raise ValueError('Unsupported source sample rate')
                if len(codec.layout.channels) not in (1, 2):
                    raise ValueError('Only mono or stereo input is supported')
                metadata = {'decoder_version': DECODER_VERSION, 'pyav_version': av.__version__,
                    'libraries': av.library_versions, 'original_sha256': digest.hexdigest(),
                    'original_bytes': before.st_size, 'container': container.format.name,
                    'codec': codec.name, 'source_sample_rate': codec.sample_rate,
                    'source_layout': codec.layout.name, 'stream_index': stream.index,
                    'stream_time_base': str(stream.time_base), 'stream_start_time': stream.start_time,
                    'container_start_time_us': container.start_time,
                    'canonical_origin_sample': start_sample, 'sample_rate': RATE, 'channels': 2,
                    'sample_format': 'PCM16 analysis copy; not original-quality master',
                    'timebase_policy': 'first_decoded_sample_zero_after_codec_skip; sequential_resample',
                    'original_pcm_alignment': 'not established', 'first_decoded_pts': None,
                    'first_decoded_time_base': None, 'decoded_source_format': None}
                resampler = av.AudioResampler(format='s16', layout='stereo', rate=RATE)
                count = written = 0
                previous_end = None
                def emit(frames, wav):
                    nonlocal count, written
                    for frame in frames:
                        check()
                        lo = max(0, start_sample-count)
                        hi = min(frame.samples, start_sample+frame_count-count)
                        if hi > lo:
                            wav.writeframesraw(bytes(frame.planes[0])[lo*4:hi*4])
                            written += hi-lo
                        count += frame.samples
                with partial.open('xb') as stage:
                    created = True
                    with wave.open(stage, 'wb') as wav:
                        wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(RATE)
                        for frame in container.decode(stream):
                            check()
                            if frame.sample_rate != codec.sample_rate or len(frame.layout.channels) not in (1, 2):
                                raise ValueError('Changing audio format is unsupported')
                            if metadata['decoded_source_format'] is None:
                                metadata.update(first_decoded_pts=frame.pts,
                                    first_decoded_time_base=str(frame.time_base), decoded_source_format=frame.format.name)
                            if frame.pts is None or frame.time_base is None:
                                raise ValueError('Decoded timestamps are required')
                            timestamp = frame.pts * frame.time_base
                            # Coarse WebM timestamps round to milliseconds. Permit
                            # at most one container tick plus one source sample.
                            tolerance = max(frame.time_base, Fraction(1, frame.sample_rate)) + Fraction(1, frame.sample_rate)
                            if previous_end is not None and abs(timestamp-previous_end) > tolerance:
                                raise ValueError('Discontinuous decoded timestamps are unsupported')
                            previous_end = timestamp + Fraction(frame.samples, frame.sample_rate)
                            frame.pts = None  # canonical contiguous decoder order is authoritative
                            emit(resampler.resample(frame), wav)
                            if written == frame_count: break
                        else:
                            emit(resampler.resample(None), wav)
                if written != frame_count:
                    raise ValueError('Source is shorter than the requested canonical passage')
                metadata.update(frames=written, decoded_canonical_frames=count,
                                elapsed_seconds=time.monotonic()-began)
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Source changed during decode')
        check()
        os.replace(partial, output)
        return metadata
    finally:
        if created and partial.exists(): partial.unlink()
