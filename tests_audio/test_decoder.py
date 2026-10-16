"""Silent decoder tests; optional research FFmpeg generates original fixtures only."""
import array
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave

from audio.decoder import decode_passage, _format
from audio.mixed_analysis import read_input_passage

FFMPEG = Path(os.environ.get('DECODER_TEST_FFMPEG',
    'audio/research/decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'))


def pcm(path):
    with wave.open(str(path)) as wav:
        return wav.getparams(), wav.readframes(wav.getnframes())


class ValidationTests(unittest.TestCase):
    def test_adts_is_distinct_from_mpeg_audio(self):
        self.assertEqual(_format(bytes.fromhex('fff1508001fffc')), 'aac')
        self.assertEqual(_format(bytes.fromhex('fff9508001fffc')), 'aac')
        self.assertEqual(_format(bytes.fromhex('fffb906400')), 'mp3')
    def test_reject_network_and_bad_bounds_before_import(self):
        for source in ('https://example.invalid/a.mp3', '//server/a.wav', r'\\server\a.wav'):
            with self.assertRaises(ValueError): decode_passage(source, 'unused.wav', 0, 1)
        with self.assertRaises(ValueError): decode_passage('unused', 'unused.wav', 0, 44100*31)
        with self.assertRaises(InterruptedError):
            decode_passage('unused', 'unused.wav', 0, 1, cancel=lambda: True)

    def test_playlist_disguised_as_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'fake.mp3'
            source.write_text('#EXTM3U\nhttps://example.invalid/track.mp3\n')
            with self.assertRaises(ValueError): decode_passage(source, root/'out.wav', 0, 1)
            self.assertFalse((root/'out.wav').exists())


@unittest.skipUnless(importlib.util.find_spec('av'), 'isolated pinned PyAV probe required')
class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root/'source.wav'
        data = array.array('h')
        for i in range(96000):
            t = i/48000
            envelope = sum(math.exp(-((t-center)/.008)**2) for center in (.5, 1.5))
            value = round(18000*envelope*(.6*math.sin(2*math.pi*997*t)+.4*math.sin(2*math.pi*1513*t)))
            data.extend((value, -value))
        with wave.open(str(self.source), 'wb') as wav:
            wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed')); wav.writeframes(data.tobytes())
    def tearDown(self): self.temp.cleanup()

    def test_wav_origin_slice_hash_short_failure_and_cancel(self):
        result = decode_passage(self.source, self.root/'full.wav', 0, 83790)
        self.assertEqual(result['original_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(result['first_decoded_pts'], 0)
        decode_passage(self.source, self.root/'slice.wav', 17777, 20000)
        self.assertEqual(pcm(self.root/'slice.wav')[1], pcm(self.root/'full.wav')[1][17777*4:37777*4])
        with self.assertRaises(ValueError): decode_passage(self.source, self.root/'short.wav', 80000, 20000)
        self.assertFalse((self.root/'short.wav.decode.partial').exists())
        ticks = [0]
        def cancel():
            ticks[0] += 1
            return ticks[0] >= 5
        with self.assertRaises(InterruptedError): decode_passage(self.source, self.root/'cancel.wav', 0, 80000, cancel=cancel)
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_analysis_wrapper_canonical_slice_integrity_and_cleanup(self):
        digest=hashlib.sha256(self.source.read_bytes()).hexdigest()
        reference=self.root/'reference.wav'
        decode_passage(self.source, reference, 11025, 22050, expected_sha256=digest)
        scratch=self.root/'analysis.decoded.wav'
        raw,info=read_input_passage(self.source,digest,.25,.75,scratch)
        self.assertEqual(raw,pcm(reference)[1])
        self.assertEqual((info['origin_sample'],info['end_sample']),(11025,33075))
        self.assertTrue(info['decoded'])
        self.assertEqual(info['decoder']['original_sha256'],digest)
        self.assertFalse(scratch.exists())
        with self.assertRaisesRegex(ValueError,'verification'):
            read_input_passage(self.source,'0'*64,.25,.75,scratch)
        self.assertFalse(scratch.exists())
        scratch.write_bytes(b'existing file')
        with self.assertRaises(FileExistsError):
            read_input_passage(self.source,digest,.25,.75,scratch)
        self.assertEqual(scratch.read_bytes(),b'existing file')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),digest)

    @unittest.skipUnless(FFMPEG.is_file(), 'research-only fixture encoder unavailable')
    def test_nonzero_container_start_and_discontinuous_pts(self):
        shifted = self.root/'shifted.m4a'
        subprocess.run([str(FFMPEG.resolve()), '-v', 'error', '-i', str(self.source),
            '-c:a', 'aac', '-output_ts_offset', '3', str(shifted)],
            check=True, timeout=20, capture_output=True)
        report = decode_passage(shifted, self.root/'shifted.wav', 0, 44100)
        self.assertGreater(report['first_decoded_pts'], 0)
        self.assertGreater(report['container_start_time_us'], 2000000)
        self.assertEqual(report['canonical_origin_sample'], 0)
        gap = self.root/'gap.mkv'
        subprocess.run([str(FFMPEG.resolve()), '-v', 'error', '-i', str(self.source),
            '-af', r'asetpts=PTS+gte(T\,1)*0.5/TB', '-c:a', 'flac', str(gap)],
            check=True, timeout=20, capture_output=True)
        with self.assertRaisesRegex(ValueError, 'Discontinuous'):
            decode_passage(gap, self.root/'gap.wav', 0, 83790)
        self.assertFalse((self.root/'gap.wav').exists())
        self.assertFalse(list(self.root.glob('*.partial')))

    @unittest.skipUnless(FFMPEG.is_file(), 'research-only fixture encoder unavailable')
    def test_compressed_formats_slice_and_controlled_marker_timing(self):
        decode_passage(self.source, self.root/'reference.wav', 0, 83790)
        reference = array.array('h'); reference.frombytes(pcm(self.root/'reference.wav')[1])
        for extension, codec in (('flac', 'flac'), ('mp3', 'libmp3lame'), ('m4a', 'aac'), ('webm', 'libopus')):
            with self.subTest(extension=extension):
                encoded = self.root/f'encoded.{extension}'
                subprocess.run([str(FFMPEG.resolve()), '-v', 'error', '-i', str(self.source),
                    '-c:a', codec, str(encoded)], check=True, timeout=20, capture_output=True)
                full = self.root/f'{extension}-full.wav'; part = self.root/f'{extension}-part.wav'
                report = decode_passage(encoded, full, 0, 83790)
                decode_passage(encoded, part, 17777, 20000)
                self.assertEqual(pcm(part)[1], pcm(full)[1][17777*4:37777*4])
                raw,info=read_input_passage(encoded,hashlib.sha256(encoded.read_bytes()).hexdigest(),
                                           17777/44100,37777/44100,self.root/'staging.wav')
                self.assertEqual(raw,pcm(part)[1]);self.assertTrue(info['decoded'])
                actual = array.array('h'); actual.frombytes(pcm(full)[1])
                if extension == 'flac': self.assertEqual(actual, reference)
                lags = []
                for center in (22050, 66150):
                    scores = [(sum(reference[2*i]*actual[2*(i+lag)] for i in range(center-500, center+500)), lag)
                              for lag in range(-80, 81)]
                    lag = max(scores)[1]; lags.append(lag)
                    self.assertLessEqual(abs(lag), 2, f'{extension}: controlled marker lag {lag}')
                print({'format': extension, 'marker_lags_samples': lags,
                       'first_pts': report['first_decoded_pts'], 'time_base': report['first_decoded_time_base']})

    @unittest.skipUnless(FFMPEG.is_file(), 'research-only fixture encoder unavailable')
    def test_adts_aac_decodes_without_claiming_original_pcm_alignment(self):
        encoded=self.root/'encoded.aac'
        subprocess.run([str(FFMPEG.resolve()),'-v','error','-i',str(self.source),
                        '-c:a','aac',str(encoded)],check=True,timeout=20,capture_output=True)
        result=decode_passage(encoded,self.root/'adts.wav',0,44100)
        self.assertEqual(result['codec'],'aac')
        self.assertEqual(result['original_pcm_alignment'],'not established')


if __name__ == '__main__': unittest.main()
