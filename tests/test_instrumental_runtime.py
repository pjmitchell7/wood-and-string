import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from audio import instrumental_runtime as runtime

class Runtime(unittest.TestCase):
    def test_missing_private_install_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(runtime,'ROOT',Path(tmp)),patch.object(runtime.analysis_runtime,'runtime_status',return_value={'ready':True,'runtime_version':'base','manifest':{}}):
            self.assertFalse(runtime.runtime_status()['ready'])
    def test_full_verify_detects_same_size_tamper_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);overlay=root/'research/basic-pitch/runtime';overlay.mkdir(parents=True)
            member=overlay/'module.py';member.write_bytes(b'original')
            inventory=root/'research/basic-pitch/runtime-inventory.json'
            inventory.write_text(json.dumps([{'path':'module.py','bytes':8,'sha256':hashlib.sha256(b'original').hexdigest()}]))
            pins={'research/basic-pitch/runtime-inventory.json':hashlib.sha256(inventory.read_bytes()).hexdigest()}
            base={'ready':True,'reason':None,'runtime_version':'base','manifest':{},'python':'python','modeldir':'models'}
            with patch.object(runtime,'ROOT',root),patch.object(runtime,'PINS',pins),patch.object(runtime.analysis_runtime,'runtime_status',return_value=base),patch.object(runtime.analysis_runtime,'require_runtime') as verify:
                self.assertTrue(runtime.require_runtime()['ready']);verify.assert_called_once()
                with self.assertRaises(InterruptedError):runtime.require_runtime(cancel=lambda:True)
                member.write_bytes(b'changed!');self.assertTrue(runtime.runtime_status()['ready'])
                with self.assertRaises(RuntimeError):runtime.require_runtime()

if __name__=='__main__':unittest.main()
