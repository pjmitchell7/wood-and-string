"""Pure planning tests; no sfizz, model, playback or actual sample rendering."""
import struct
import tempfile
from pathlib import Path
import unittest
from audio.research import probe_emilyguitar as probe


class EmilyProbeTests(unittest.TestCase):
    def test_header_geometry_and_truncation(self):
        fmt = struct.pack('<HHIIHH', 1, 1, 44100, 132300, 3, 24)
        chunks = b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', 3) + bytes(4)
        content = b'RIFF' + struct.pack('<I', 4 + len(chunks)) + b'WAVE' + chunks
        with tempfile.TemporaryDirectory(dir=probe.APP / 'work', prefix='emily-header-test-') as folder:
            root = Path(folder)
            self.assertTrue(root.resolve().is_relative_to((probe.APP / 'work').resolve()))
            path = root / 'stub.wav'
            path.write_bytes(content)
            self.assertEqual(probe.wav_header(path)['frames'], 1)
            path.write_bytes(content[:-1])
            with self.assertRaisesRegex(ValueError, 'RIFF length'):
                probe.wav_header(path)

    def test_group_defaults_reset_and_deterministic_ablation(self):
        text = '<group> lokey=59 hikey=61 amp_random=3\n<region> sample=notes/c4.wav lorand=0.2 hirand=0.4\n' \
               '<group> lokey=62 hikey=64 pitch_keycenter=63\n<region> sample=notes/eb4.wav'
        rows = probe.parse_basic(text)
        self.assertEqual(rows[0]['pitch_keycenter'], 60)
        self.assertEqual(rows[1]['pitch_keycenter'], 63)
        self.assertNotIn('amp_random', rows[1]['opcodes'])
        explicit = probe.isolated_map(rows[0])
        omitted = probe.isolated_map(rows[0], explicit_default=False)
        self.assertIn('pitch_keycenter=60', explicit)
        self.assertNotIn('pitch_keycenter', omitted)
        self.assertIn('amp_random=0', explicit)
        self.assertNotIn('lorand', explicit)
        self.assertNotIn('hirand', explicit)

    def test_unsupported_syntax_rejected(self):
        for text in ('<global> lokey=40', '<group> lokey=40 hikey=40 mystery=1\n<region> sample=notes/a.wav',
                     '<group> lokey=40 hikey=40\n<region> sample=../a.wav',
                     '<group> lokey=40 hikey=40\n<region> sample=notes/a.wav lorand=.8 hirand=.2'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                probe.parse_basic(text)

    def test_real_pinned_map_plan_geometry_without_sample_execution(self):
        rows = probe.parse_basic((probe.BANK / 'emily_basic.sfz').read_text())
        plan = probe.build_plan({'regions': rows})
        self.assertEqual(len(plan['source_cells']), 288)
        self.assertEqual(len(plan['dynamic_cells']), 24)
        self.assertEqual(len(plan['implicit_root_pair_regions']), 16)
        self.assertEqual(len(plan['controls']), 10)
        self.assertEqual(plan['budget']['planned_retained_stereo_float_bytes'], 18345600)
        self.assertFalse(plan['candidate_profile']['mode_enabled'])
        self.assertEqual(sum(row['candidate_40_84'] and row['category'] == 'notes' for row in rows), 192)
        for control in plan['controls']:
            for event in control['events']:
                self.assertGreaterEqual(event['start'], probe.ORIGIN)
                self.assertGreater(event['end'], event['start'])
                self.assertEqual(event['pitch'], plan['candidate_profile']['opens'][event['string']] + event['fret'])
                self.assertLessEqual(event['end'] - probe.ORIGIN, control['frames'])


if __name__ == '__main__':
    unittest.main()
