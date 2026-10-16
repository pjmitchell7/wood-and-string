import tempfile
import unittest
from library import Library
from pairing import Pairing

class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000
        self.library=Library(self.temp.name);self.pairing=Pairing(self.library,clock=lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def test_approval_claim_once_and_revoke(self):
        request=self.pairing.request('Test phone')
        self.assertEqual(self.pairing.claim(request['id'],request['secret'])['state'],'pending')
        with self.assertRaises(PermissionError):self.pairing.claim(request['id'],'wrong')
        self.pairing.approve_local(request['id'])
        device=self.pairing.claim(request['id'],request['secret'])
        self.assertEqual(self.pairing.authenticate(device['token'])['id'],device['device_id'])
        retry=self.pairing.claim(request['id'],request['secret'])
        self.assertEqual(device,retry);self.assertEqual(len(self.pairing.devices()),1)
        with self.library.connect() as db:
            self.assertNotEqual(db.execute('SELECT token_hash FROM devices').fetchone()[0],device['token'])
        self.pairing.revoke_local(device['device_id'])
        self.assertIsNone(self.pairing.authenticate(device['token']))
        with self.assertRaises(PermissionError):self.pairing.claim(request['id'],request['secret'])
    def test_expiry_and_bounded_pending(self):
        first=self.pairing.request('phone')
        with self.assertRaises(ValueError):self.pairing.request('too soon')
        for i in range(4):self.now+=5;self.pairing.request('test'+str(i))
        self.now+=5
        with self.assertRaises(ValueError):self.pairing.request('over limit')
        self.now+=301
        with self.assertRaises(PermissionError):self.pairing.claim(first['id'],first['secret'])
        self.assertEqual(self.pairing.pending(),[])

if __name__=='__main__':unittest.main()
