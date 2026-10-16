import tempfile
import unittest
import hashlib
import wave
from pathlib import Path
from audio.performance import Note,Performance,arrange
from audio.render import render

class RenderTests(unittest.TestCase):
    def test_late_passage_has_explicit_source_origin(self):
        origin=48000*180
        a=arrange(Performance('late',48000,[Note('n',origin+4800,origin+9600,64)]))
        a['render_origin_sample']=origin
        with tempfile.TemporaryDirectory() as tmp:
            result=render(a,Path(tmp)/'late.wav')
            self.assertEqual(result['frames'],57600)
            self.assertEqual(result['source_origin_sample'],origin)
            self.assertEqual(a['notes'][0]['start'],origin+4800)
    def test_real_render_repeat_and_cancel(self):
        a=arrange(Performance('render-test',48000,[Note('n',4800,9600,64)]))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            one=render(a,p/'a.wav');two=render(a,p/'b.wav')
            self.assertGreater(one['peak'],0)
            self.assertEqual(one['clipped_samples'],0)
            self.assertEqual(hashlib.sha256((p/'a.wav').read_bytes()).digest(),hashlib.sha256((p/'b.wav').read_bytes()).digest())
            with wave.open(str(p/'a.wav')) as w:
                self.assertEqual(w.getframerate(),48000)
                self.assertEqual(w.getnframes(),57600)
                self.assertEqual(set(w.readframes(4800)),{0})
            with self.assertRaises(InterruptedError):render(a,p/'cancel.wav',cancel=lambda:True)
            self.assertFalse((p/'cancel.wav').exists())
            self.assertFalse((p/'cancel.wav.partial').exists())
            with self.assertRaises(FileExistsError):render(a,p/'a.wav')

if __name__=='__main__':unittest.main()
