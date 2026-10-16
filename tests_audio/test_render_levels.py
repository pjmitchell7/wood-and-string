import array
import math
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

from audio.render import _write_pcm16, render
from audio.performance import Note, Performance, arrange


class RenderLevelTests(unittest.TestCase):
    def convert(self, root, values, policy='peak', cancel=lambda: False):
        data = array.array('f', values)
        if sys.byteorder != 'little': data.byteswap()
        spool = root / 'mix.float'
        spool.write_bytes(data.tobytes())
        result = _write_pcm16(spool, root/'out.wav', 44100,
                             max(abs(x) for x in values), policy, cancel, lambda _: None)
        with wave.open(str(root/'out.wav')) as wav:
            pcm = array.array('h', wav.readframes(wav.getnframes()))
        if sys.byteorder != 'little': pcm.byteswap()
        return result, pcm

    def test_float_gain_precedes_quantization_and_preserves_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Smallest values would round to zero if PCM16 came first.
            values = [0., 0., 1e-5, -1e-5, 5e-6, -5e-6]
            result, pcm = self.convert(Path(tmp), values)
            target = 10**(-3/20)
            self.assertEqual(list(pcm[:2]), [0, 0])
            for value, actual in zip(values, pcm):
                self.assertLessEqual(abs(actual/32768 - value/1e-5*target), 1/32768)
            self.assertAlmostEqual(result['peak'], target, delta=1/32768)
            self.assertEqual(result['clipped_samples'], 0)
            self.assertTrue(all(abs(x) < 32767 for x in pcm))

    def test_over_unity_float_mix_is_attenuated_before_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, pcm = self.convert(Path(tmp), [4., -4., 2., -2.])
            self.assertLess(result['gain_db'], 0)
            self.assertEqual(pcm[0], -pcm[1])
            self.assertAlmostEqual(pcm[2]/pcm[0], .5, delta=1/32768)

    def test_silence_dust_and_nonfinite_rejected(self):
        for values in ([0., 0.], [1e-9, -1e-9], [math.inf, 0.], [math.nan, 0.]):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError): self.convert(Path(tmp), values)

    def test_actual_guitar_and_cleanup_during_second_pass(self):
        a = arrange(Performance('levels', 44100, [Note('n', 4410, 13230, 64)]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = render(a, root/'guitar.wav')
            self.assertAlmostEqual(20*math.log10(result['peak']), -3, delta=.001)
            self.assertGreater(result['raw_peak'], 1e-8)
            self.assertFalse(list(root.glob('*.partial')))
            with wave.open(str(root/'guitar.wav')) as wav:
                self.assertEqual(set(wav.readframes(4410)), {0})
            cancel = [False]
            def progress(value):
                if value >= .8: cancel[0] = True
            with self.assertRaises(InterruptedError):
                render(a, root/'cancel.wav', cancel=lambda: cancel[0], progress=progress)
            self.assertFalse((root/'cancel.wav').exists())
            self.assertFalse(list(root.glob('*.partial')))
            with patch('audio.render._write_pcm16', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError): render(a, root/'fail.wav')
            self.assertFalse((root/'fail.wav').exists())
            self.assertFalse(list(root.glob('*.partial')))


if __name__ == '__main__': unittest.main()
