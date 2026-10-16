"""Bounded local full-song decode; callback data is provisional until return.

Run under process supervision: cooperative hooks cannot interrupt native decode
or a blocked callback. The caller owns downstream staging and must discard it
on any exception. No complete decoded file or song-sized PCM buffer is created.
"""
from fractions import Fraction
import hashlib
import math
import os
import re
import stat
import time
from .decoder import RATE, DECODER_VERSION, CODECS, _local_path, _format

VERSION='pyav19-full-stream-v2'
MAX_SOURCE_BYTES=250*1024**2

def _protected_reader(path):
    """Own a Windows handle denying concurrent write/delete through close.

    Read sharing is permitted; an existing writer or delete-access handle makes
    CreateFile fail. This protects decoder bytes, unlike before/after hashes
    alone, which cannot detect a temporary edit restored before verification.
    No attributes, ACLs or privileges are changed. Other platforms fail closed.
    """
    if os.name!='nt':raise RuntimeError('Full decode requires Windows protected file sharing')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                                ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    # The CRT descriptor now exclusively owns the Win32 handle.
    try:return os.fdopen(fd,'rb')
    except BaseException:
        os.close(fd)
        raise

def stream_decode(source,on_chunk,*,expected_sha256,max_seconds=900,chunk_frames=RATE,
                  cancel=lambda:False,timeout_seconds=300):
    """Call on_chunk(start_sample, packed stereo PCM16 bytes), then return receipt.

    Canonical zero is the first emitted resampled sample, after codec skip. Never
    infer original PCM alignment from timestamps. Reject excess duration, do not
    truncate it. No callback can commit until the final source rehash succeeds.
    """
    if not callable(on_chunk):raise TypeError('Expected PCM chunk callback')
    if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):raise ValueError('Expected original SHA256 required')
    if type(max_seconds) not in (int,float) or not math.isfinite(max_seconds) or not 0<max_seconds<=900:raise ValueError('Maximum duration must be positive and at most 900 seconds')
    if type(chunk_frames) is not int or not 1<=chunk_frames<=RATE:raise ValueError('Chunk size must be 1 to 44100 frames')
    if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=3600:raise ValueError('Invalid decode deadline')
    maximum=int(max_seconds*RATE)
    if maximum<1:raise ValueError('Maximum duration must contain at least one sample')
    began=time.monotonic()
    def check():
        if cancel():raise InterruptedError('Full decode cancelled')
        if time.monotonic()-began>timeout_seconds:raise TimeoutError('Full decode deadline exceeded')
    def digest(handle):
        handle.seek(0);h=hashlib.sha256()
        while data:=handle.read(1024**2):check();h.update(data)
        return h.hexdigest()
    check();source=_local_path(source)
    with _protected_reader(source) as handle:
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_SOURCE_BYTES:raise ValueError('Expected local regular source at most 250 MiB')
        fmt=_format(handle.read(32))
        if digest(handle)!=expected_sha256:raise ValueError('Original audio failed verification before decoding')
        handle.seek(0)
        import av
        if av.__version__!='19.0.1':raise RuntimeError('Pinned PyAV 19.0.1 is required')
        with av.open(handle,mode='r',format=fmt,options={'protocol_whitelist':'','enable_drefs':'0','use_absolute_path':'0',
                'codec_whitelist':','.join(sorted(CODECS)),'probesize':'1048576','analyzeduration':'5000000'}) as container:
            if not container.streams.audio:raise ValueError('Source contains no audio stream')
            stream=container.streams.audio[0];codec=stream.codec_context
            if codec.name not in CODECS:raise ValueError('Unsupported audio codec')
            codec.thread_count=1
            channels=len(codec.layout.channels)
            if not 8000<=codec.sample_rate<=192000 or channels not in (1,2):raise ValueError('Only mono/stereo input at 8–192 kHz is supported')
            metadata={'decoder_version':DECODER_VERSION,'full_decoder_version':VERSION,'pyav_version':av.__version__,
                'libraries':av.library_versions,'original_sha256':expected_sha256,'original_bytes':before.st_size,
                'container':container.format.name,'codec':codec.name,'source_sample_rate':codec.sample_rate,
                'source_layout':codec.layout.name,'source_channels':channels,'stream_index':stream.index,
                'stream_time_base':str(stream.time_base),'stream_start_time':stream.start_time,'container_start_time_us':container.start_time,
                'canonical_origin_sample':0,'sample_rate':RATE,'channels':2,'sample_format':'PCM16 analysis copy; not original-quality master',
                'timebase_policy':'first_decoded_sample_zero_after_codec_skip; sequential_resample','original_pcm_alignment':'not established',
                'first_decoded_pts':None,'first_decoded_time_base':None,'decoded_source_format':None}
            resampler=av.AudioResampler(format='s16',layout='stereo',rate=RATE)
            count=source_frames=chunks=0;previous_end=None
            def emit(frames):
                nonlocal count,chunks
                for frame in frames:
                    check()
                    if count+frame.samples>maximum:raise ValueError('Decoded source exceeds maximum duration; no truncated result accepted')
                    plane=memoryview(frame.planes[0])
                    if len(plane)<frame.samples*4:raise ValueError('Invalid canonical PCM plane')
                    for lo in range(0,frame.samples,chunk_frames):
                        check();n=min(chunk_frames,frame.samples-lo)
                        on_chunk(count,bytes(plane[lo*4:(lo+n)*4]));check()
                        count+=n;chunks+=1
            for frame in container.decode(stream):
                check()
                if frame.sample_rate!=codec.sample_rate or len(frame.layout.channels)!=channels:raise ValueError('Changing audio format is unsupported')
                if metadata['decoded_source_format'] is None:
                    metadata.update(first_decoded_pts=frame.pts,first_decoded_time_base=str(frame.time_base),decoded_source_format=frame.format.name)
                elif frame.format.name!=metadata['decoded_source_format']:raise ValueError('Changing audio format is unsupported')
                if frame.pts is None or frame.time_base is None:raise ValueError('Decoded timestamps are required')
                timestamp=frame.pts*frame.time_base
                tolerance=max(frame.time_base,Fraction(1,frame.sample_rate))+Fraction(1,frame.sample_rate)
                if previous_end is not None and abs(timestamp-previous_end)>tolerance:raise ValueError('Discontinuous decoded timestamps are unsupported')
                previous_end=timestamp+Fraction(frame.samples,frame.sample_rate);source_frames+=frame.samples
                frame.pts=None;emit(resampler.resample(frame))
            emit(resampler.resample(None));check()
            if not count:raise ValueError('Source contains no decoded audio samples')
            metadata.update(frames=count,decoded_canonical_frames=count,decoded_source_frames=source_frames,
                            chunks=chunks,resampler_flushed=True,last_decoded_end_time=str(previous_end))
        # Same open handle binds both hashes to the exact bytes supplied to PyAV.
        if digest(handle)!=expected_sha256:raise ValueError('Source changed during decode')
        after=os.fstat(handle.fileno());current=source.stat()
        signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if signature(before)!=signature(after) or signature(after)!=signature(current):raise ValueError('Source changed during decode')
    check();metadata.update(source_unchanged_verified=True,source_protection='Windows GENERIC_READ with FILE_SHARE_READ only, held through both hashes and decode',elapsed_seconds=time.monotonic()-began)
    return metadata
