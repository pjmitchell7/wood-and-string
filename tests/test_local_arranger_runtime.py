"""Tiny owned package fixtures only; no acquired model/native execution."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib
from audio import local_arranger_runtime as runtime


def sha(data):return hashlib.sha256(data).hexdigest()


class RuntimeGuard(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'package';self.root.mkdir()
        self.native=self.root/'runtime';self.native.mkdir()
        self.notices=self.root/'notices';self.notices.mkdir()
        self.system=Path(self.tmp.name)/'system32';self.system.mkdir()
        self.system_file=self.system/'base.dll';self.system_file.write_bytes(b'system fixture, never executed')
        self.model={'file':'tiny.gguf','bytes':32,'sha256':sha(b'm'*32),'revision':'test-revision'}
        (self.root/self.model['file']).write_bytes(b'm'*32)
        self.files={'llama-completion.exe':b'completion stub bytes','llama-tokenize.exe':b'tokenize stub bytes',
                    'ggml.dll':b'library stub bytes','LICENSE-LLVM-OpenMP':b'fixture license bytes'}
        archive=self.root/'tiny.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for name,data in self.files.items():z.writestr(name,data);(self.native/name).write_bytes(data)
        self.pins={'file':'tiny.zip','bytes':archive.stat().st_size,'sha256':sha(archive.read_bytes()),
                   'expanded_bytes':sum(map(len,self.files.values())),'members':len(self.files),'tag':'test-tag','commit':'test-commit',
                   'executable':'llama-completion.exe','tokenize':'llama-tokenize.exe','metadata':{}}
        inventory=[{'path':name,'bytes':len(data),'sha256':sha(data),'zip_crc32':f'{zlib.crc32(data):08x}'} for name,data in self.files.items()]
        self.write('runtime-inventory.json',inventory)
        binaries=[{'path':str(self.native/name),'bytes':len(data),'sha256':sha(data)} for name,data in self.files.items() if name.endswith(('.exe','.dll'))]
        binaries.append({'path':str(self.system_file),'bytes':self.system_file.stat().st_size,'sha256':sha(self.system_file.read_bytes())})
        deps={'binaries':binaries,'imports':{b['path']:([] if b['path']==str(self.system_file) else ['base.dll','api-ms-fixture.dll']) for b in binaries},
              'windows_api_set_contracts':['api-ms-fixture.dll'],'unresolved':[],'limitations':'Fixture normal imports only; no dynamic resolution claim.'}
        self.write('static-dependencies.json',deps)
        notice_data={'qwen-LICENSE':b'fixture model license','llama-LICENSE':b'fixture runtime license','LICENSE-LLVM-OpenMP':self.files['LICENSE-LLVM-OpenMP']}
        notices={}
        for name,data in notice_data.items():
            (self.notices/name).write_bytes(data);notices[name]={'bytes':len(data),'sha256':sha(data)}
        self.write('frozen.json',{'model':deepcopy(self.model),'runtime':{k:v for k,v in self.pins.items() if k!='metadata'}})
        self.write('acquisition.json',{'state':'complete','native_executed':False,'notices':notices,'artifacts':[
            {'file':p['file'],'expected_bytes':p['bytes'],'transferred_bytes':p['bytes'],'expected_sha256':p['sha256'],'sha256':p['sha256'],'state':'verified'} for p in (self.model,self.pins)]})
        self.repin()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name,value in [('ROOT',self.root),('MODEL',self.model),('RUNTIME',self.pins),('SYSTEM32',self.system)]:self.stack.enter_context(patch.object(runtime,name,value))

    def write(self,name,value):
        (self.root/name).write_text(json.dumps(value,indent=2),encoding='utf-8')

    def load(self,name):return json.loads((self.root/name).read_text(encoding='utf-8'))

    def repin(self):
        files=[*self.root.glob('*.json'),*self.notices.iterdir()]
        self.pins['metadata']={p.relative_to(self.root).as_posix():(p.stat().st_size,sha(p.read_bytes())) for p in files}

    def test_status_is_cheap_and_full_receipt_is_json_safe_stable(self):
        with patch.object(runtime,'_digest',side_effect=AssertionError('Cheap status hashed a binary')):
            status=runtime.runtime_status();self.assertTrue(status['ready']);self.assertFalse(status['full_verified'])
        one=runtime.require_runtime();two=runtime.require_runtime()
        self.assertTrue(one['full_verified']);self.assertFalse(one['protection_active'])
        self.assertEqual(one,two);json.dumps(one,allow_nan=False)
        self.assertEqual(one['identity_sha256'],status['identity_sha256'])
        self.assertEqual(one['executable'],str(self.native/'llama-completion.exe'))
        self.assertEqual(len(one['native_inputs']),len(self.files)+1)

    def test_same_size_model_and_runtime_tampering_rejected(self):
        for p in (self.root/self.model['file'],self.native/'ggml.dll'):
            with self.subTest(file=p.name):
                original=p.read_bytes();p.write_bytes(b'x'*len(original))
                self.assertTrue(runtime.runtime_status()['ready'])
                with self.assertRaises(ValueError):runtime.require_runtime()
                p.write_bytes(original)

    def test_inventory_is_not_an_independent_trust_root(self):
        target=self.native/'ggml.dll';target.write_bytes(b'x'*len(self.files['ggml.dll']))
        digest=sha(target.read_bytes());rows=self.load('runtime-inventory.json')
        for row in rows:
            if row['path']=='ggml.dll':row['sha256']=digest
        self.write('runtime-inventory.json',rows)
        deps=self.load('static-dependencies.json')
        for b in deps['binaries']:
            if b['path']==str(target):b['sha256']=digest
        self.write('static-dependencies.json',deps);self.repin()
        self.assertTrue(runtime.runtime_status()['ready'])
        with self.assertRaisesRegex(ValueError,'authenticated ZIP'):runtime.require_runtime()

    def test_archive_same_size_tamper_rejected(self):
        target=self.root/self.pins['file'];data=bytearray(target.read_bytes());data[8]^=1;target.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'archive'):runtime.require_runtime()

    def test_extra_binary_nested_directory_and_root_file_rejected(self):
        for path,directory in ((self.native/'unexpected.dll',False),(self.native/'nested',True),(self.root/'unexpected.exe',False)):
            with self.subTest(entry=path.name):
                path.mkdir() if directory else path.write_bytes(b'owned harmless fixture')
                self.assertFalse(runtime.runtime_status()['ready'])
                with self.assertRaises(ValueError):runtime.require_runtime()
                path.rmdir() if directory else path.unlink()

    def test_unsafe_and_colliding_inventory_names_rejected(self):
        original=self.load('runtime-inventory.json')
        for name in ('../outside.dll','folder/file.dll','C:/absolute.dll','C:\\absolute.dll','colon:stream.dll','NUL.dll','trailing.','LLAMA-TOKENIZE.EXE'):
            with self.subTest(name=name):
                rows=deepcopy(original);rows[0]['path']=name;self.write('runtime-inventory.json',rows);self.repin()
                self.assertFalse(runtime.runtime_status()['ready'])
        self.write('runtime-inventory.json',original);self.repin()

    def test_hardlinked_owned_file_rejected(self):
        target=self.native/'ggml.dll';outside=Path(self.tmp.name)/'linked-data';outside.write_bytes(target.read_bytes())
        target.unlink();os.link(outside,target)
        self.assertFalse(runtime.runtime_status()['ready'])
        with self.assertRaises(ValueError):runtime.require_runtime()

    def test_symlinked_owned_file_rejected_when_supported(self):
        target=self.native/'ggml.dll';outside=Path(self.tmp.name)/'symlink-data';outside.write_bytes(target.read_bytes());target.unlink()
        try:target.symlink_to(outside)
        except OSError as error:
            target.write_bytes(outside.read_bytes());self.skipTest('Creating owned symlink unavailable: '+str(error))
        self.assertFalse(runtime.runtime_status()['ready'])

    def test_stale_receipt_and_frozen_pin_rejected_even_if_repinned(self):
        receipt=self.load('acquisition.json')
        for state in ('started','failed'):
            changed=deepcopy(receipt);changed['state']=state;self.write('acquisition.json',changed);self.repin()
            self.assertFalse(runtime.runtime_status()['ready'])
        changed=deepcopy(receipt);changed['artifacts'][0]['transferred_bytes']-=1
        self.write('acquisition.json',changed);self.repin();self.assertFalse(runtime.runtime_status()['ready'])
        self.write('acquisition.json',receipt)
        frozen=self.load('frozen.json');frozen['model']['revision']='other';self.write('frozen.json',frozen);self.repin()
        self.assertFalse(runtime.runtime_status()['ready'])

    def test_metadata_tampering_and_notice_loss_fail_closed(self):
        path=self.root/'acquisition.json';data=path.read_bytes();path.write_bytes(data.replace(b'complete',b'xxxxxxxx'))
        self.assertFalse(runtime.runtime_status()['ready']);path.write_bytes(data)
        (self.notices/'qwen-LICENSE').unlink();self.assertFalse(runtime.runtime_status()['ready'])

    def test_bad_crc_metadata_rejected_against_archive(self):
        rows=self.load('runtime-inventory.json');rows[0]['zip_crc32']='00000000';self.write('runtime-inventory.json',rows);self.repin()
        with self.assertRaisesRegex(ValueError,'inventory differ'):runtime.require_runtime()

    def test_static_closure_and_system_tampering(self):
        deps=self.load('static-dependencies.json');deps['unresolved']=['absent.dll'];self.write('static-dependencies.json',deps);self.repin()
        self.assertFalse(runtime.runtime_status()['ready'])
        deps['unresolved']=[];self.write('static-dependencies.json',deps);self.repin()
        self.system_file.write_bytes(b'x'*self.system_file.stat().st_size)
        with self.assertRaisesRegex(ValueError,'System32'):runtime.require_runtime()

    def test_observed_system_hardlinks_are_allowed_without_package_exemption(self):
        # Normal Windows System32 files can also have WinSxS hard links.
        os.link(self.system_file,Path(self.tmp.name)/'system-copy-link')
        self.assertTrue(runtime.require_runtime()['full_verified'])

    def test_cancel_before_and_during_hash_releases_owned_handles(self):
        with self.assertRaises(InterruptedError):runtime.require_runtime(cancel=lambda:True)
        cancelled=[False];real=runtime._digest
        def digest(stream,cancel):
            result=real(stream,cancel);cancelled[0]=True;return result
        with patch.object(runtime,'_digest',side_effect=digest):
            with self.assertRaises(InterruptedError):runtime.require_runtime(cancel=lambda:cancelled[0])
        with (self.root/self.model['file']).open('r+b') as stream:stream.write(b'm')

    @unittest.skipUnless(os.name=='nt','Windows share-mode semantics')
    def test_protected_interval_blocks_owned_writes_and_releases_after_exit(self):
        targets=[self.root/self.model['file'],self.native/'ggml.dll',self.root/'runtime-inventory.json']
        with runtime.protect_runtime() as receipt:
            self.assertTrue(receipt['protection_active']);json.dumps(receipt)
            for target in targets:
                with self.assertRaises(PermissionError):
                    with target.open('r+b'):pass
                with self.assertRaises(PermissionError):target.rename(target.with_suffix('.renamed'))
            # Owned guard must not lock operating-system dependencies.
            with self.system_file.open('r+b'):pass
        for target in targets:
            with target.open('r+b'):pass

    def test_new_entry_during_context_is_detected_at_exit(self):
        with self.assertRaisesRegex(ValueError,'extra entries'):
            with runtime.protect_runtime():(self.native/'late.dll').write_bytes(b'owned harmless fixture')
        with (self.native/'ggml.dll').open('r+b'):pass


if __name__=='__main__':unittest.main()
