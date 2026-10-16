from pathlib import Path
import tempfile
import unittest
import uuid
from library import Library
from snapshots import export_snapshot,restore_snapshot,validate

class Snapshots(unittest.TestCase):
    def test_roundtrip_and_corruption_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);library=Library(root/'original')
            staged=library.stage/'import.partial';staged.write_bytes(b'original sound')
            original=library.publish(staged,'original.wav','audio/wav')
            staged.write_bytes(b'guitar sound')
            library.publish(staged,'solo.wav','audio/wav','solo',original['id'])
            snapshot=root/'snapshot';export_snapshot(library,snapshot)
            restore_snapshot(snapshot,root/'restored')
            restored=Library(root/'restored')
            self.assertEqual(restored.items(),library.items())
            for item in restored.items(): self.assertEqual((restored.media/item['sha256']).read_bytes(),(library.media/item['sha256']).read_bytes())
            (snapshot/'media'/original['sha256']).write_bytes(b'wrong')
            with self.assertRaises(ValueError): restore_snapshot(snapshot,root/'bad-restore')
            self.assertFalse((root/'bad-restore').exists())
            self.assertEqual((library.media/original['sha256']).read_bytes(),b'original sound')
    def test_restart_scratch_cleanup_preserves_unknown_and_completed_media(self):
        with tempfile.TemporaryDirectory() as root:
            library=Library(root);identity=str(uuid.uuid4())
            for suffix in ('.partial','.wav.partial','.wav','.cancel'):
                (library.stage/(identity+suffix)).write_bytes(b'partial')
            (library.stage/'unknown.wav').write_bytes(b'preserve')
            library.reconcile_staging()
            self.assertEqual([x.name for x in library.stage.iterdir()],['unknown.wav'])

if __name__=='__main__': unittest.main()
