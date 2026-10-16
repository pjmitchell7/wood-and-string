"""Silent sustained-note regression against independently fully loaded samples."""
import array
import ctypes as C
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio import render as module


class OfflineRender(unittest.TestCase):
    def test_long_held_strings_match_complete_preload_without_changing_gates(self):
        # Native recorded regions on independent strings; no pitch extraction.
        notes = [dict(player=1, string=3, start=4410, end=48510, pitch=73, velocity=80),
                 dict(player=1, string=0, start=8820, end=52920, pitch=40, velocity=80)]
        arrangement = dict(sample_rate=44100, notes=notes)
        original_configure = module._configure_offline
        def complete_preload(api, synth):
            settings = original_configure(api, synth)
            fn=api.sfizz_set_preload_size; fn.argtypes=[C.c_void_p,C.c_uint];fn.restype=None
            fn(synth, 1048576)
            return settings
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result=module.render(arrangement,root/'offline.wav')
            with patch('audio.render._configure_offline',complete_preload):
                module.render(arrangement,root/'preloaded.wav')
            self.assertEqual(hashlib.sha256((root/'offline.wav').read_bytes()).digest(),
                             hashlib.sha256((root/'preloaded.wav').read_bytes()).digest())
            self.assertEqual(result['processing_mode'],'freewheeling')
            self.assertEqual(result['renderer_version'],module.RENDER_VERSION)
            self.assertEqual(result['frames'],97020)
            self.assertEqual(len(result['quality_by_instance']),2)
            with wave.open(str(root/'offline.wav')) as wav:
                pcm=array.array('h',wav.readframes(wav.getnframes()))
            self.assertEqual(set(pcm[:4410*2]),{0})
            self.assertGreater(max(abs(x) for x in pcm[22050*2:26460*2]),100)
            self.assertEqual(set(pcm[-4410*2:]),{0})
            self.assertFalse(list(root.glob('*.partial')))
        self.assertEqual([n['end'] for n in notes],[48510,52920])


if __name__=='__main__':unittest.main()
