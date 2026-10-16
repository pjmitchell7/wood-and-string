"""Silent real PyAV streaming/EOF tests. No model or audio-device calls."""
import hashlib
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import av
import numpy as np
from audio.full_decode import stream_decode
from audio.decoder import decode_passage,RATE

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

class FullDecode(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def wav(self,seconds=1,rate=48000,channels=2):
        p=self.root/'source.wav';n=round(seconds*rate)
        # Deterministic bounded sawtooth, never played.
        x=((np.arange(n,dtype=np.int64)%1000)-500).astype('<i2')
        if channels==2:x=np.column_stack((x,-x))
        with wave.open(str(p),'wb') as w:
            w.setnchannels(channels);w.setsampwidth(2);w.setframerate(rate);w.writeframes(x.tobytes())
        return p
    def collect(self,p,**kw):
        chunks=[]
        receipt=stream_decode(p,lambda start,data:chunks.append((start,data)),expected_sha256=sha(p),**kw)
        at=0
        for start,data in chunks:self.assertEqual(start,at);self.assertEqual(len(data)%4,0);at+=len(data)//4
        self.assertEqual(at,receipt['frames']);return b''.join(data for _,data in chunks),receipt,chunks
    def test_over_thirty_seconds_exact_pcm_and_callback_bounds(self):
        p=self.wav(31.125,44100);data,receipt,chunks=self.collect(p,chunk_frames=127)
        with wave.open(str(p)) as w:self.assertEqual(data,w.readframes(w.getnframes()))
        self.assertGreater(receipt['frames'],30*RATE);self.assertTrue(receipt['resampler_flushed'])
        self.assertTrue(receipt['source_unchanged_verified']);self.assertTrue(all(0<len(d)<=127*4 for _,d in chunks))
    def test_compressed_eof_flush_matches_canonical_passage_slices(self):
        p=self.wav(32.125,48000,1);flac=self.root/'source.flac'
        with av.open(str(p)) as inp,av.open(str(flac),'w',format='flac') as out:
            stream=out.add_stream('flac',rate=48000);stream.layout='mono'
            for frame in inp.decode(audio=0):
                for packet in stream.encode(frame):out.mux(packet)
            for packet in stream.encode(None):out.mux(packet)
        data,receipt,_=self.collect(flac,chunk_frames=1000)
        self.assertEqual(receipt['frames'],(1542000*RATE+47999)//48000);self.assertEqual(receipt['source_channels'],1)
        for start,count in [(0,1001),(29*RATE,3*RATE),(receipt['frames']-1201,1201)]:
            output=self.root/f'slice-{start}.wav';decode_passage(flac,output,start,count,expected_sha256=sha(flac))
            with wave.open(str(output)) as w:self.assertEqual(data[start*4:(start+count)*4],w.readframes(count))
        # The last resampled samples are included, not dropped at compressed EOF.
        self.assertEqual(len(data),receipt['frames']*4)
    def test_duration_rejects_instead_of_truncating(self):
        p=self.wav(1.2);received=[]
        with self.assertRaisesRegex(ValueError,'exceeds maximum duration'):
            stream_decode(p,lambda s,d:received.append(d),expected_sha256=sha(p),max_seconds=1)
        self.assertTrue(received);self.assertLessEqual(sum(map(len,received)),RATE*4)
    def test_cancel_callback_error_and_deadline_close_handles(self):
        p=self.wav();cancelled=[False]
        def sink(s,d):cancelled[0]=True
        with self.assertRaises(InterruptedError):stream_decode(p,sink,expected_sha256=sha(p),cancel=lambda:cancelled[0])
        class Stop(Exception):pass
        with self.assertRaises(Stop):stream_decode(p,lambda s,d:(_ for _ in ()).throw(Stop()),expected_sha256=sha(p))
        clock=[0.]
        with patch('audio.full_decode.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(TimeoutError):
                stream_decode(p,lambda s,d:clock.__setitem__(0,2.),expected_sha256=sha(p),timeout_seconds=1)
        p.rename(self.root/'closed.wav')
    def test_bad_hash_size_urls_and_configuration(self):
        p=self.wav();calls=[]
        with self.assertRaisesRegex(ValueError,'verification'):stream_decode(p,lambda *a:calls.append(a),expected_sha256='0'*64)
        self.assertEqual(calls,[])
        with patch('audio.full_decode.MAX_SOURCE_BYTES',1):
            with self.assertRaisesRegex(ValueError,'250 MiB'):self.collect(p)
        with self.assertRaises(ValueError):stream_decode('https://example.com/audio.wav',lambda *a:None,expected_sha256='0'*64)
        for kw in [{'chunk_frames':True},{'max_seconds':float('nan')},{'timeout_seconds':float('inf')},{'max_seconds':901}]:
            with self.assertRaises(ValueError):self.collect(p,**kw)
    def test_preexisting_writer_fails_closed_without_callbacks(self):
        p=self.wav();original=sha(p);calls=[]
        with p.open('r+b'):
            with self.assertRaises(OSError):stream_decode(p,lambda *a:calls.append(a),expected_sha256=original)
        self.assertEqual(calls,[]);self.assertEqual(sha(p),original)
    def test_transient_mutation_and_delete_denied_through_final_verification(self):
        p=self.wav(32,44100);original=sha(p);attempts=[];data=[]
        def try_mutation(label):
            with self.assertRaises(OSError):
                with p.open('r+b') as f:f.seek(20*RATE*4+44);f.write(b'X'*RATE*8)
            with self.assertRaises(OSError):p.unlink()
            attempts.append(label)
        def sink(start,pcm):
            if not attempts:try_mutation('first callback')
            if start>=25*RATE and len(attempts)==1:try_mutation('after25seconds')
            data.append(pcm)
        # Probe at final fstat, after the final hash but before protected close.
        real_fstat=os.fstat;calls=[0]
        def checked_fstat(fd):
            calls[0]+=1
            if calls[0]==2:try_mutation('final verification')
            return real_fstat(fd)
        with patch('audio.full_decode.os.fstat',side_effect=checked_fstat):
            result=stream_decode(p,sink,expected_sha256=original)
        self.assertEqual(attempts,['first callback','after25seconds','final verification'])
        self.assertEqual(sha(p),original)
        with wave.open(str(p)) as w:self.assertEqual(b''.join(data),w.readframes(w.getnframes()))
        self.assertIn('FILE_SHARE_READ',result['source_protection'])
        # The protection is temporary, not an attribute/permission change.
        with p.open('r+b') as f:self.assertEqual(f.read(4),b'RIFF')
        p.rename(self.root/'released.wav')
    def test_other_platform_fails_closed(self):
        from audio.full_decode import _protected_reader
        with patch('audio.full_decode.os.name','posix'):
            with self.assertRaisesRegex(RuntimeError,'Windows'):_protected_reader('unused')
    def test_five_compressed_codecs_eof_slices_with_existing_encoder(self):
        ffmpeg=Path(__file__).resolve().parents[1]/'audio/research/decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
        if not ffmpeg.is_file():self.skipTest('Existing private FFmpeg encoder not available; never download')
        p=self.wav(32.125,48000,2)
        for suffix,codec in [('mp3','libmp3lame'),('m4a','aac'),('webm','libopus'),('aac','aac'),('flac','flac')]:
            with self.subTest(format=suffix):
                encoded=self.root/('encoded.'+suffix)
                subprocess.run([str(ffmpeg),'-nostdin','-v','error','-i',str(p),'-c:a',codec,str(encoded)],
                    check=True,timeout=30,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                data,receipt,chunks=self.collect(encoded,chunk_frames=4096)
                self.assertGreater(receipt['frames'],30*RATE);self.assertTrue(receipt['resampler_flushed'])
                self.assertTrue(all(len(chunk)<=4096*4 for _,chunk in chunks))
                for label,start,count in [('first',0,1001),('middle',15*RATE,2101),('end',receipt['frames']-1201,1201)]:
                    output=self.root/f'{suffix}-{label}.wav'
                    decode_passage(encoded,output,start,count,expected_sha256=sha(encoded))
                    with wave.open(str(output)) as w:self.assertEqual(data[start*4:(start+count)*4],w.readframes(count))
    def test_discontinuous_pts_rejected_with_real_resampler(self):
        p=self.wav();real=av.open
        class Wrapper:
            def __init__(self,*a,**kw):self.c=real(*a,**kw)
            def __enter__(self):return self
            def __exit__(self,*args):self.c.close()
            def __getattr__(self,name):return getattr(self.c,name)
            def decode(self,stream):
                for i,f in enumerate(self.c.decode(stream)):
                    if i==1:f.pts+=48000
                    yield f
        with patch('av.open',Wrapper):
            with self.assertRaisesRegex(ValueError,'Discontinuous'):self.collect(p)

if __name__=='__main__':unittest.main()
