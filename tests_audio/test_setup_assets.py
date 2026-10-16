import tempfile
import unittest
import zipfile
from pathlib import Path
from audio.setup_assets import safe_extract

class SetupTests(unittest.TestCase):
    def test_traversal_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            with zipfile.ZipFile(p/'bad.zip','w') as z:
                z.writestr('ok.txt','safe');z.writestr('../escape.txt','unsafe')
            with self.assertRaises(ValueError):safe_extract(p/'bad.zip',p/'out')
            self.assertFalse((p/'escape.txt').exists());self.assertFalse((p/'out/ok.txt').exists())

if __name__=='__main__':unittest.main()
