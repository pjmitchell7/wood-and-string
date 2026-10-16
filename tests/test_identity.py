import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from identity import library_identity
from library import Library
from pairing import Pairing


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stable_identity_and_missing_file_requires_recovery(self):
        first = library_identity(self.root)
        self.assertEqual(library_identity(self.root), first)
        path = self.root / 'identity.json'
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Restore'):
            library_identity(self.root)
        self.assertFalse(path.exists())
        path.write_bytes(original)
        self.assertEqual(library_identity(self.root), first)

    def test_existing_device_without_identity_cannot_get_new_origin(self):
        pairing = Pairing(Library(self.root))
        request = pairing.request('Established phone')
        pairing.approve_local(request['id'])
        device = pairing.claim(request['id'], request['secret'])
        with self.assertRaisesRegex(ValueError, 'Restore'):
            library_identity(self.root)
        self.assertFalse((self.root / 'identity.json').exists())
        self.assertIsNotNone(pairing.authenticate(device['token']))

    def test_changed_or_corrupt_identity_is_preserved_and_rejected(self):
        original = library_identity(self.root)
        path = self.root / 'identity.json'
        other = dict(original, hostname='different.local')
        path.write_text(json.dumps(other))
        with self.assertRaises(ValueError):
            library_identity(self.root)
        self.assertEqual(json.loads(path.read_text()), other)
        path.write_text('{')
        with self.assertRaises(ValueError):
            library_identity(self.root)
        self.assertEqual(path.read_text(), '{')

    def test_failed_atomic_publication_does_not_leave_partial_identity(self):
        with patch('identity.os.replace', side_effect=PermissionError('simulated file hold')):
            with self.assertRaises(PermissionError):
                library_identity(self.root)
        self.assertFalse((self.root / 'identity.json').exists())
        self.assertEqual(list(self.root.glob('identity-*.tmp')), [])
        self.assertEqual(library_identity(self.root), library_identity(self.root))


if __name__ == '__main__':
    unittest.main()
