"""Availability is a silent presence check, never an integrity or sound pass."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio.electric_preview_runtime import bank_status


class ElectricAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bank = root / 'bank'
        self.bank.mkdir()
        self.sample = self.bank / 'one.wav'
        self.sample.write_bytes(b'not-a-waveform')
        self.engine = root / 'sfizz.dll'
        self.engine.write_bytes(b'not-a-native-library')
        self.dependency = root / 'dependency.dll'
        self.dependency.write_bytes(b'not-a-dependency')
        self.config = dict(bank=self.bank, engine=self.engine,
            members=[{'path': 'one.wav', 'bytes': self.sample.stat().st_size}],
            native={'binaries': [{'path': str(path), 'bytes': path.stat().st_size}
                                for path in (self.engine, self.dependency)],
                    'imports': {str(self.engine): ['dependency.dll', 'api-ms-test.dll'],
                                str(self.dependency): []}})

    def status(self):
        with patch('audio.electric_preview_runtime._configuration', return_value=self.config):
            # Availability must never parse/load these deliberately invalid media
            # and DLL contents. Rendering retains the full integrity obligation.
            return bank_status(self.bank.parent)

    def test_present_files_only_support_unverified_availability(self):
        result = self.status()
        self.assertTrue(result['ready'])
        self.assertFalse(result['integrity_checked'])
        self.assertFalse(result['musical_acceptance'])

    def test_missing_or_wrong_size_sample_and_native_dependency_disable_availability(self):
        for path in (self.sample, self.engine, self.dependency):
            original = path.read_bytes()
            with self.subTest(path=path.name, failure='size'):
                path.write_bytes(b'changed-size')
                self.assertFalse(self.status()['ready'])
                path.write_bytes(original)
            with self.subTest(path=path.name, failure='missing'):
                path.unlink()
                self.assertFalse(self.status()['ready'])
                path.write_bytes(original)

    def test_unpinned_metadata_refuses_availability_without_engine_loading(self):
        with patch('audio.electric_preview_runtime._configuration', side_effect=ValueError('pin changed')):
            result = bank_status(self.bank.parent)
        self.assertFalse(result['ready'])
        self.assertFalse(result['integrity_checked'])


if __name__ == '__main__':
    unittest.main()
