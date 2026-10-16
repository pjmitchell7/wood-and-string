import array
import io
import math
from pathlib import Path
import tempfile
import time
import unittest
import wave
import subprocess
import sys
import json
import uuid
from library import Library
from fastapi.testclient import TestClient
from server import create_app
from audio.render import RENDER_VERSION

class Pipeline(unittest.TestCase):
    def test_real_worker_exits_with_parent_pipe_still_open_after_terminal_result(self):
        # Missing source fails before extraction/rendering; exercise the actual
        # entry point, watcher shutdown and worker lock with no audio work.
        with tempfile.TemporaryDirectory() as root:
            library=Library(root)
            for unused in range(2):
                identity=str(uuid.uuid4())
                with library.connect() as db:
                    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                        (identity,'queued',0,json.dumps({'kind':'extract','asset_id':'missing','renderer_version':RENDER_VERSION}),None,None,time.time()))
                child=subprocess.Popen([sys.executable,'worker.py',root,identity],stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                try:
                    self.assertEqual(child.wait(timeout=10),0)
                    self.assertFalse(child.stdin.closed)
                    self.assertEqual(library.job(identity)['state'],'failed')
                    with library.exclusive('worker.lock'): pass
                finally:
                    if child.poll() is None: child.kill();child.wait(timeout=5)
                    child.stdin.close()

    def test_isolated_phrase_import_extract_render_and_idempotence(self):
        with tempfile.TemporaryDirectory() as root:
            app=create_app(root)
            with TestClient(app,base_url='http://127.0.0.1:8765') as client:
                client.get('/session')
                buffer=io.BytesIO()
                with wave.open(buffer,'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
                    wav.writeframes(array.array('h',(int(8000*math.sin(2*math.pi*440*i/8000)) for i in range(8000))).tobytes())
                asset=client.post('/api/import',headers={'X-Filename':'isolated-test.wav'},content=buffer.getvalue()).json()
                spec={'asset_id':asset['id'],'start_seconds':0,'end_seconds':1,'isolated_melody':True,'performers':1}
                response=client.post('/api/extract',json=spec)
                self.assertEqual(response.status_code,200,response.text)
                identity=response.json()['id'];deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    job=client.get('/api/jobs/'+identity).json()
                    if job['state'] not in ('queued','running'): break
                    time.sleep(.1)
                self.assertEqual(job['state'],'complete',job)
                result=job['result'];self.assertEqual(result['asset']['source_id'],asset['source_id'])
                self.assertEqual(result['arrangement']['source']['notes'][0]['pitch'],69)
                self.assertTrue(result['metrics']['peak']>0)
                retry=client.post('/api/extract',json=spec).json()
                self.assertEqual(retry['id'],identity);self.assertEqual(retry['state'],'complete')
                self.assertEqual(len(client.get('/api/library').json()['items']),2)
                self.assertEqual(client.post('/api/extract',json={**spec,'performers':2}).status_code,400)
                self.assertEqual(client.post('/api/extract',json={**spec,'isolated_melody':False}).status_code,400)
    def test_authority_lock_prevents_second_owner(self):
        with tempfile.TemporaryDirectory() as root:
            app=create_app(root)
            with app.state.library.exclusive('authority.lock'):
                with self.assertRaises(OSError):
                    with app.state.library.exclusive('authority.lock'): pass
    def test_worker_cancels_after_parent_process_is_killed(self):
        with tempfile.TemporaryDirectory() as root:
            library=Library(root);identity=str(uuid.uuid4())
            with library.connect() as db:
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',(identity,'queued',0,json.dumps({'kind':'diagnostic','performers':2,'renderer_version':RENDER_VERSION}),None,None,time.time()))
            # Owned test parent keeps the worker pipe open; forceful parent exit
            # must close it and trigger cooperative cancellation in the worker.
            code="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'worker.py',sys.argv[1],sys.argv[2]],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True); time.sleep(60)"
            parent=subprocess.Popen([sys.executable,'-c',code,root,identity],stdout=subprocess.PIPE,text=True)
            try:
                worker_pid=int(parent.stdout.readline().strip())
                deadline=time.monotonic()+10
                while library.job(identity)['state']=='queued' and time.monotonic()<deadline: time.sleep(.02)
                self.assertEqual(library.job(identity)['state'],'running')
                parent.terminate();parent.wait(timeout=5)
                deadline=time.monotonic()+10
                while library.job(identity)['state']=='running' and time.monotonic()<deadline: time.sleep(.05)
                self.assertEqual(library.job(identity)['state'],'cancelled')
                deadline=time.monotonic()+5
                while True:
                    try:
                        with library.exclusive('worker.lock'): break
                    except OSError:
                        if time.monotonic()>deadline: raise
                        time.sleep(.05)
                self.assertEqual(list(library.stage.glob('*.partial')),[])
                self.assertEqual(library.items(),[])
            finally:
                if parent.poll() is None: parent.terminate();parent.wait(timeout=5)
                parent.stdout.close()

if __name__=='__main__': unittest.main()
