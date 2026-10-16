import hashlib
import json
import time
import uuid
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from library import Library
from server import create_app
from fastapi.testclient import TestClient

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.library=Library(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def staged(self,data=b'RIFF-test-fixture'):
        path=self.library.stage/'test.partial';path.write_bytes(data);return path
    def test_idempotent_original_preserves_bytes(self):
        a=self.library.publish(self.staged(),'first.wav','audio/wav')
        b=self.library.publish(self.staged(),'renamed.wav','audio/wav')
        self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.library.items()),1)
        self.assertEqual((self.library.media/a['sha256']).read_bytes(),b'RIFF-test-fixture')
    def test_variants_have_distinct_identity_even_identical_bytes(self):
        a=self.library.publish(self.staged(),'first.wav','audio/wav')
        b=self.library.publish(self.staged(),'solo.wav','audio/wav','solo',a['id'])
        self.assertNotEqual(a['id'],b['id']);self.assertEqual(a['sha256'],b['sha256'])
        self.assertEqual(a['source_id'],b['source_id'])
    def test_low_space_preserves_store(self):
        with patch('library.shutil.disk_usage') as usage:
            usage.return_value.free=100
            with self.assertRaises(ValueError): self.library.space(1000)
        self.assertEqual(self.library.items(),[])
    def test_retry_repairs_corrupt_blob_and_retains_bad_copy(self):
        a=self.library.publish(self.staged(),'first.wav','audio/wav')
        (self.library.media/a['sha256']).write_bytes(b'corrupt')
        b=self.library.publish(self.staged(),'retry.wav','audio/wav')
        self.assertEqual(a['id'],b['id'])
        self.assertEqual((self.library.media/a['sha256']).read_bytes(),b'RIFF-test-fixture')
        self.assertEqual(len(list(self.library.stage.glob('*.corrupt'))),1)
    def test_variant_retry_repairs_without_duplicate_identity(self):
        first=self.library.publish(self.staged(),'solo.wav','audio/wav','solo','source-test')
        (self.library.media/first['sha256']).write_bytes(b'corrupt')
        retry=self.library.publish(self.staged(),'solo.wav','audio/wav','solo','source-test')
        self.assertEqual(first['id'],retry['id']);self.assertTrue(self.library.verify(first['id']))

    def test_reconcile_removes_owned_decode_scratch_only(self):
        identity=str(uuid.uuid4())
        owned=[self.library.stage/(identity+suffix) for suffix in
               ('.analysis.decoded.wav','.analysis.decoded.wav.decode.partial')]
        for path in owned:path.write_bytes(b'owned')
        unrelated=self.library.stage/'my-recording.wav';unrelated.write_bytes(b'preserve')
        self.assertEqual(self.library.reconcile_staging(),10)
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertEqual(unrelated.read_bytes(),b'preserve')

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(self.temp.name)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765')
    def tearDown(self): self.client.close();self.temp.cleanup()
    def test_session_boundary(self):
        self.assertEqual(self.client.get('/api/library').status_code,401)
        self.assertEqual(self.client.get('/',headers={'Host':'other.invalid'}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
        self.assertEqual(self.client.get('/session',headers={'Origin':'https://other.invalid'}).status_code,403)
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/api/library').status_code,200)
    def test_upload_range_and_duplicate(self):
        self.client.get('/')
        payload=b'RIFF'+bytes(range(256))*10
        a=self.client.post('/api/import',headers={'X-Filename':'test.wav'},content=payload)
        self.assertEqual(a.status_code,200,a.text)
        item=a.json();self.assertEqual(item['sha256'],hashlib.sha256(payload).hexdigest())
        b=self.client.post('/api/import',headers={'X-Filename':'new.wav'},content=payload)
        self.assertEqual(b.json()['id'],item['id'])
        part=self.client.get('/api/media/'+item['id'],headers={'Range':'bytes=2-7'})
        self.assertEqual(part.status_code,206);self.assertEqual(part.content,payload[2:8])
    def test_failed_empty_upload_leaves_no_asset(self):
        self.client.get('/')
        self.assertEqual(self.client.post('/api/import',headers={'X-Filename':'empty.wav'},content=b'').status_code,400)
        self.assertEqual(self.app.state.library.items(),[])
        self.assertEqual(list(self.app.state.library.stage.iterdir()),[])
    def test_request_payload_limits(self):
        self.client.get('/session')
        self.assertEqual(self.client.post('/api/diagnostic',content=b'x'*1025).status_code,413)
        self.assertEqual(self.client.post('/api/extract',content=b'x'*4097).status_code,413)
    def test_completed_job_missing_media_is_not_success(self):
        self.client.get('/session');identity=str(uuid.uuid4())
        result={'asset':{'id':'missing-id'}}
        with self.app.state.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(identity,'complete',1,'{}',json.dumps(result),None,time.time()))
        job=self.client.get('/api/jobs/'+identity).json()
        self.assertEqual(job['state'],'interrupted')
        self.assertIn('verification',job['error'])

if __name__=='__main__': unittest.main()
