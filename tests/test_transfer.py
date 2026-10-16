from pathlib import Path
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from library import Library
from snapshots import export_snapshot,validate
from transfer import transfer_snapshot,BLOCK,TransferBusyError

def held_writer(source,destination,ready,release,outcome):
    """A real child holds ownership after one durable block has been written."""
    announced=False
    def progress(done,total):
        nonlocal announced
        if not announced:
            announced=True;ready.set()
            if not release.wait(15):raise TimeoutError('Test did not release writer')
    try:outcome.put(('complete',transfer_snapshot(source,destination,progress=progress)))
    except BaseException as error:outcome.put(('error',type(error).__name__,str(error)))

def crash_writer(source,destination):
    def progress(done,total):
        if done>=BLOCK:os._exit(23)  # Deliberate owned test-process crash, no cleanup.
    transfer_snapshot(source,destination,progress=progress)

def fixture(root):
    library=Library(root/'library');staged=library.stage/'track'
    staged.write_bytes(b'A'*BLOCK+b'B'*BLOCK+b'C'*512)
    item=library.publish(staged,'fixture.wav','audio/wav');export_snapshot(library,root/'snapshot')
    return library,item

class TransferTests(unittest.TestCase):
    def test_interrupted_copy_resumes_without_public_partial(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);library=Library(root/'library')
            staged=library.stage/'track';staged.write_bytes(b'A'*BLOCK+b'B'*BLOCK+b'C'*512)
            item=library.publish(staged,'fixture.wav','audio/wav')
            export_snapshot(library,root/'snapshot')
            transferred=[0]
            with self.assertRaises(InterruptedError):
                transfer_snapshot(root/'snapshot',root/'replica',cancel=lambda:transferred[0]>=BLOCK,progress=lambda n,total:transferred.__setitem__(0,n))
            self.assertFalse((root/'replica').exists())
            result=transfer_snapshot(root/'snapshot',root/'replica')
            self.assertGreaterEqual(result['resumed_bytes'],BLOCK)
            self.assertEqual(result['copied_bytes']+result['resumed_bytes'],item['size'])
            self.assertEqual(validate(root/'snapshot'),validate(root/'replica'))
            self.assertTrue(transfer_snapshot(root/'snapshot',root/'replica')['already_complete'])

    def test_concurrent_process_cannot_append_or_publish_same_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);library,item=fixture(root)
            export_snapshot(library,root/'other-generation')
            context=multiprocessing.get_context('spawn');ready=context.Event();release=context.Event();outcome=context.Queue()
            process=context.Process(target=held_writer,args=(str(root/'snapshot'),str(root/'replica'),ready,release,outcome))
            process.start()
            try:
                self.assertTrue(ready.wait(15),'first writer did not reach durable block')
                incoming=list(root.glob('replica.incoming-*'));self.assertEqual(len(incoming),1)
                partial=incoming[0]/'media'/(item['sha256']+'.part')
                before=partial.read_bytes();self.assertEqual(len(before),BLOCK)
                for source in (root/'snapshot',root/'other-generation'):
                    with self.assertRaisesRegex(TransferBusyError,'Another transfer'):
                        transfer_snapshot(source,root/'replica')
                cli=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'transfer.py'),str(root/'snapshot'),str(root/'replica')],capture_output=True,text=True,timeout=5)
                self.assertEqual(cli.returncode,2)
                self.assertEqual(json.loads(cli.stdout)['status'],'busy')
                self.assertEqual(cli.stderr,'','busy is an expected result, not a traceback')
                self.assertEqual(partial.read_bytes(),before,'second writer must not change durable prefix')
                self.assertFalse((root/'replica').exists())
                self.assertEqual(len(list(root.glob('replica.incoming-*'))),1,'different generation must not create parallel incoming state')
            finally:
                release.set();process.join(15)
                if process.is_alive():process.terminate();process.join(5)
            self.assertEqual(process.exitcode,0)
            result=outcome.get(timeout=3);self.assertEqual(result[0],'complete',result)
            outcome.close();outcome.join_thread()
            self.assertEqual(validate(root/'snapshot'),validate(root/'replica'))
            self.assertTrue(transfer_snapshot(root/'snapshot',root/'replica')['already_complete'])
            self.assertTrue((root/'replica.transfer.lock').exists())
            self.assertEqual({p.name for p in (root/'replica').iterdir()},{'manifest.json','media'},'snapshot contains no live DB or lock')

    def test_forced_process_exit_releases_lock_and_resumes_fsynced_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,item=fixture(root)
            process=multiprocessing.get_context('spawn').Process(target=crash_writer,args=(str(root/'snapshot'),str(root/'replica')))
            process.start();process.join(15)
            if process.is_alive():process.terminate();process.join(5);self.fail('crash test child hung')
            self.assertEqual(process.exitcode,23)
            self.assertFalse((root/'replica').exists())
            partial=next(root.glob('replica.incoming-*'))/'media'/(item['sha256']+'.part')
            self.assertEqual(partial.stat().st_size,BLOCK)
            self.assertEqual(hashlib.sha256(partial.read_bytes()).digest(),hashlib.sha256(b'A'*BLOCK).digest())
            result=transfer_snapshot(root/'snapshot',root/'replica')
            self.assertEqual(result['resumed_bytes'],BLOCK)
            self.assertEqual(result['resumed_bytes']+result['copied_bytes'],item['size'])
            self.assertEqual(validate(root/'snapshot'),validate(root/'replica'))

    def test_publication_rename_failure_keeps_verified_generation_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,item=fixture(root)
            with patch('transfer.os.rename',side_effect=PermissionError('Simulated directory in use')):
                with self.assertRaises(PermissionError):transfer_snapshot(root/'snapshot',root/'replica')
            self.assertFalse((root/'replica').exists())
            incoming=next(root.glob('replica.incoming-*'))
            self.assertEqual(validate(incoming),validate(root/'snapshot'))
            result=transfer_snapshot(root/'snapshot',root/'replica')
            self.assertEqual(result['copied_bytes'],0)
            self.assertEqual(result['resumed_bytes'],item['size'])
            self.assertEqual(validate(root/'replica'),validate(root/'snapshot'))

if __name__=='__main__':unittest.main()
