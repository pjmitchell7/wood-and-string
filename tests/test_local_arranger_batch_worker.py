"""Owned synthetic files and fake native boundary only; no model or holdout read."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from audio import local_arranger_batch_worker as worker
from audio import local_arranger_gpu_trial as shared
from audio import local_arranger_batch_plan as plan
from audio import qwen_prompt_audit as prompt_audit
from audio import capture_archive as real_archive
from audio.arranger_baseline import make_baseline
from audio.arranger_completion import WINDOWS_EOG_TRAILER
from audio.arranger_prompt import build_prompt, canonical
from audio.gpu_trial_monitor import PROFILE
from tests.test_arranger_contract import context


def sha(raw):return hashlib.sha256(raw).hexdigest()


class FakeGpu:
    identity={'name':'NVIDIA GeForce GTX 1660 Ti','nvml_index':0}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def snapshot(self):
        return {'total_bytes':6*1024**3,'free_bytes':int(5.8*1024**3),
                'reported_used_bytes':int(.2*1024**3),'unreported_reserved_bytes':0}


class BatchWorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'public8';self.root.mkdir()
        self.ordinal=2;self.count_dir=self.root/'development-02/count';self.gen_dir=self.root/'development-02/generate'
        self.count_dir.mkdir(parents=True);self.gen_dir.mkdir()
        self.sources=[];rows=[]
        for ordinal,case_id in enumerate(plan.PUBLIC_IDS,1):
            c=context();c['case_id']=case_id;self.sources.append(c)
            rows.append({'ordinal':ordinal,'case_id':case_id,'input_files':plan.input_rows(c),
                         'action':'reuse_preserved_actual_case1' if ordinal==1 else 'count_then_at_most_one_generation',
                         'context_tokens':4096,'prompt_token_limit':3072,'output_token_limit':1024,
                         'token_count':1888 if ordinal==1 else None})
        self.source=self.sources[1];self.artifact=build_prompt(self.source)
        self.frozen={'version':plan.VERSION,'device_receipt_sha256':'d'*64,'plan':{
            'version':plan.VERSION,'public_benchmark_sha256':plan.PUBLIC_SHA,
            'first_forward_freeze_sha256':plan.FORWARD_FREEZE_SHA,'baseline_freeze_sha256':plan.BASELINE_FREEZE_SHA,
            'runtime_identity_sha256':plan.RUNTIME_ID,'gpu_profile':dict(PROFILE),'cases':rows}}
        self.freeze_raw=canonical(self.frozen);(self.root/'frozen.json').write_bytes(self.freeze_raw)
        for path in (self.count_dir,self.gen_dir):(path/'context.json').write_bytes(canonical(self.source))
        self.request={'operation':'count','ordinal':2,'context_sha256':sha(canonical(self.source)),
                      'device_receipt_sha256':'d'*64,'freeze_sha256':sha(self.freeze_raw)}
        self.runtime={'identity_sha256':plan.RUNTIME_ID,'executable':str(self.root/'never-executed.exe'),
                      'model':str(self.root/'synthetic-never-loaded.gguf')}
        Path(self.runtime['model']).write_bytes(b'not a model; fake audit boundary only')
        self.selected={'device':'Vulkan1','name':'NVIDIA GeForce GTX 1660 Ti'}
        self.calls=[];self.archive_reads=[];self.audit_calls=[];self.guard_open=False
        self.generated_count=2000;self.body=canonical(make_baseline(self.source));self.eog=True
        self.addCleanup(patch.stopall)
        patch.object(worker,'TRIAL_ROOT',self.root).start()
        patch.object(worker,'_assert_owned_worker').start()
        patch.object(shared,'device_evidence',return_value=self.selected).start()
        patch.object(shared,'NvidiaMemory',return_value=FakeGpu()).start()
        patch.object(shared,'available_ram',return_value=7*1024**3).start()
        patch.object(shared,'run_native',side_effect=self.native).start()
        patch.object(prompt_audit,'audit_capture',side_effect=self.audit).start()
        @contextmanager
        def runtime_guard():
            self.guard_open=True
            try:yield self.runtime
            finally:self.guard_open=False
        patch.object(shared,'runtime_guard',runtime_guard).start()
        # Archive compression/lifecycle is independently tested by its owner.
        # This stub tests wrapper integration and keeps exact archive bytes locked.
        archive=types.ModuleType('audio.capture_archive');archive.read_capture=self.read_capture
        patch.dict(sys.modules,{'audio.capture_archive':archive}).start()

    def audit(self,raw,model,prompt):
        self.audit_calls.append((raw,prompt));tokens=shared.parse_prompt_count(raw)
        return {'count':tokens,'entire_single_user_template_matches':True,'reasoning_template_state':'empty_think_block_prefilled',
                'capture_sha256':sha(raw),'exact_prompt_sha256':sha(prompt),'exact_prompt_bytes':len(prompt),
                'reconstructed_sha256':sha(b'synthetic-template'+prompt),'reconstructed_bytes':len(prompt)+18,
                'token_ids_u32le_sha256':sha(str(tokens).encode()+prompt),'metadata_sha256':'a'*64,
                'metadata_bytes':123,'required_model_sha256':'b'*64}

    def native(self,argv,**kwargs):
        self.calls.append((argv,kwargs));self.assertTrue(self.guard_open)
        kwargs['before_resume']();self.assertFalse(kwargs['cancel']())
        generating=argv[argv.index('--n-predict')+1]=='1024'
        tokens=self.generated_count if generating else 2000
        for path in [self.root/'frozen.json',kwargs['cwd']/'context.json']:
            with self.assertRaises(PermissionError):path.write_bytes(b'attempted mutation')
        if generating:
            for path in [self.count_dir/'supervisor.json',self.count_dir/'captures.json',self.count_dir/'prompt.txt',
                         self.count_dir/'count.stderr.gz',self.count_dir/'count.stdout.gz']:
                with self.assertRaises(PermissionError):path.write_bytes(b'attempted mutation')
        stdout=self.body+WINDOWS_EOG_TRAILER if generating else b'\r\n'
        stderr=(b'llama_prepare_model_devices: using device Vulkan1 (NVIDIA GeForce GTX 1660 Ti) (synthetic-id) - 5966 MiB free\r\n'
                b'load_tensors: offloaded 37/37 layers to GPU\r\n'+
                f'llama_completion: number of tokens in prompt = {tokens}\r\n'.encode())
        if generating:
            stderr+=b'n_remain: 1023\r\n'+(b'found an EOG token\r\n' if self.eog else b'')
            stderr+=f'common_perf_print:    samplers time =       0.00 ms /  {tokens+1} tokens\r\n'.encode()
        kwargs['stdout_path'].write_bytes(stdout);kwargs['stderr_path'].write_bytes(stderr)
        return {'returncode':0,'reason':'exit','owned_job_empty':True,'native_pid':123,
                'primary_thread_resumed':True,'primary_thread_handle_closed':True,
                'stdout_bytes':len(stdout),'stderr_bytes':len(stderr)}

    def read_capture(self,stack,folder,name,*,expected,binding,freeze_sha256,cancel=lambda:False):
        self.archive_reads.append((folder,name,expected,binding,freeze_sha256))
        if (freeze_sha256!=sha(self.freeze_raw) or binding['raw']!={'name':name,**expected}
            or binding['ownership']!={'freeze_sha256':freeze_sha256,'ordinal':2,'stage':'count'}):
            raise ValueError('Synthetic archive binding differs')
        archive=worker._read(stack,folder/(name+'.gz'),1024**2,binding['archive']['sha256'])
        raw=gzip.decompress(archive)
        if len(raw)!=expected['bytes'] or sha(raw)!=expected['sha256']:raise ValueError('Synthetic archive raw mismatch')
        return raw

    def counted(self):
        returned=worker.run(self.request,lambda event:None)
        self.assertEqual(returned['state'],'counted')
        self.prior=json.loads((self.count_dir/'count.result.json').read_bytes())
        (self.count_dir/'supervisor.json').write_bytes(canonical({'result':returned}))
        bindings={}
        for name in ('count.stdout','count.stderr'):
            path=self.count_dir/name;raw=path.read_bytes();compressed=gzip.compress(raw,mtime=0)
            (self.count_dir/(name+'.gz')).write_bytes(compressed);path.unlink()
            bindings[name]={'raw':{'name':name,**self.prior['captures'][name]},
                'archive':{'name':name+'.gz','bytes':len(compressed),'sha256':sha(compressed)},
                'ownership':{'freeze_sha256':sha(self.freeze_raw),'ordinal':2,'stage':'count'}}
        (self.count_dir/'captures.json').write_bytes(canonical({'version':'capture-archive-v1','captures':bindings}))
        self.gen_request=self.request|{'operation':'generate',
            'count_supervisor_sha256':sha((self.count_dir/'supervisor.json').read_bytes()),
            'capture_manifest_sha256':sha((self.count_dir/'captures.json').read_bytes())}
        return returned

    def save_prior(self):
        raw=canonical(self.prior);(self.count_dir/'count.result.json').write_bytes(raw)
        supervisor=canonical({'result':{'state':'counted','receipt':'count.result.json','bytes':len(raw),'sha256':sha(raw)}})
        (self.count_dir/'supervisor.json').write_bytes(supervisor);self.gen_request['count_supervisor_sha256']=sha(supervisor)

    def counted_with_real_archives(self):
        """Actual Windows archive transaction over fake-native count output."""
        returned=worker.run(self.request,lambda event:None)
        self.assertEqual(returned['state'],'counted')
        self.prior=json.loads((self.count_dir/'count.result.json').read_bytes())
        (self.count_dir/'supervisor.json').write_bytes(canonical({'result':returned}))
        captured={name:(self.count_dir/name).read_bytes() for name in ('count.stdout','count.stderr')}
        bindings={name:real_archive.archive_capture(self.count_dir,name,
            expected=self.prior['captures'][name],freeze_sha256=sha(self.freeze_raw)) for name in captured}
        (self.count_dir/'captures.json').write_bytes(canonical({'version':real_archive.VERSION,'captures':bindings}))
        self.gen_request=self.request|{'operation':'generate',
            'count_supervisor_sha256':sha((self.count_dir/'supervisor.json').read_bytes()),
            'capture_manifest_sha256':sha((self.count_dir/'captures.json').read_bytes())}
        return captured

    def preflight(self):
        _,delegated=worker.validate_request(self.gen_request)
        with ExitStack() as stack:
            return worker._preflight(stack,self.gen_request,self.frozen['plan']['cases'][1],delegated,
                                     self.runtime,self.artifact,prompt_audit.NONTHINK_TEMPLATE)

    def test_count_delegates_only_fixed_existing_flags_and_never_expectations(self):
        returned=worker.run(self.request,lambda event:None)
        self.assertEqual(returned['state'],'counted');self.assertEqual(len(self.calls),1)
        argv,kwargs=self.calls[0]
        self.assertEqual(argv,shared.count_argv(self.runtime,self.count_dir,'Vulkan1',self.count_dir/'chat-template.jinja'))
        self.assertEqual(kwargs['wall_seconds'],120);self.assertEqual(kwargs['memory_bytes'],PROFILE['native_working_set_stop_bytes'])
        self.assertEqual((self.count_dir/'prompt.txt').read_bytes(),self.artifact['prompt'].encode())
        self.assertEqual(self.archive_reads,[]);self.assertFalse((self.gen_dir/'generate.attempt.json').exists())

    def test_fixed_ordinals_fields_and_hashes_reject_before_delegate(self):
        for altered in [self.request|{'ordinal':v} for v in (1,9,True,'2')]+[
                self.request|{'operation':'help'},self.request|{'work_dir':str(self.count_dir)},
                self.request|{'context_sha256':'wrong'},self.request|{'expectations':{}},
                self.request|{'operation':'generate'}]:
            with self.subTest(request=altered),self.assertRaises(ValueError):worker.run(altered,lambda event:None)
        self.assertEqual(self.calls,[]);self.assertFalse((self.count_dir/'count.attempt.json').exists())

    def test_cross_case_or_changed_frozen_context_rejects_before_delegate(self):
        for path in (self.root/'frozen.json',self.count_dir/'context.json'):
            original=path.read_bytes();path.write_bytes(original+b' ')
            with self.subTest(path=path),self.assertRaises(ValueError):worker.run(self.request,lambda event:None)
            path.write_bytes(original)
        third=self.root/'development-03/count';third.mkdir(parents=True)
        (third/'context.json').write_bytes(canonical(self.source))
        with self.assertRaises(ValueError):worker.run(self.request|{'ordinal':3},lambda event:None)
        self.assertEqual(self.calls,[])

    def test_generation_reaudits_archives_holds_inputs_and_uses_unchanged_completion(self):
        self.counted();before=[p.read_bytes() for p in (self.count_dir/'supervisor.json',self.count_dir/'captures.json')]
        returned=worker.run(self.gen_request,lambda event:None)
        result=json.loads((self.gen_dir/'generate.result.json').read_bytes())
        self.assertEqual(returned['state'],'proposed',result);self.assertEqual(len(self.calls),2)
        self.assertEqual(len(self.archive_reads),2);self.assertEqual(len(self.audit_calls),3)
        self.assertFalse((self.count_dir/'count.stderr').exists());self.assertFalse(self.guard_open)
        self.assertEqual(before,[p.read_bytes() for p in (self.count_dir/'supervisor.json',self.count_dir/'captures.json')])
        admission=result['prior_count_admission']
        for key in ('count_supervisor_sha256','capture_manifest_sha256','freeze_sha256'):
            self.assertEqual(admission[key],self.gen_request[key])
        self.assertEqual(admission['public_ordinal'],2);self.assertTrue(admission['archived_captures_reaudited'])
        argv=self.calls[-1][0]
        self.assertEqual(argv,shared.count_argv(self.runtime,self.gen_dir,'Vulkan1',self.gen_dir/'chat-template.jinja',1024))
        self.assertEqual((self.gen_dir/'completion.raw').read_bytes(),self.body)
        for name,pin in result['output_files'].items():
            raw=(self.gen_dir/name).read_bytes();self.assertEqual(pin,{'bytes':len(raw),'sha256':sha(raw)})
        self.assertFalse(result['musical_quality_assessed'])

    def test_each_counted_input_or_archive_change_is_rejected(self):
        self.counted()
        for name in (*worker.INPUT_NAMES,'count.result.json','supervisor.json','captures.json','count.stdout.gz','count.stderr.gz'):
            path=self.count_dir/name;raw=path.read_bytes();path.write_bytes(raw+b'changed')
            with self.subTest(name=name),self.assertRaises((ValueError,OSError,EOFError)):self.preflight()
            path.write_bytes(raw)
        self.assertEqual(len(self.calls),1)

    def test_profile_device_argv_count_and_native_ownership_fail_closed(self):
        self.counted();good=deepcopy(self.prior)
        changes=[('runtime_identity_sha256','c'*64),('prompt_tokens',3073),('context_tokens',8192),
                 ('input_files',{}),('profile',{}),('selected_device',{'device':'Vulkan0'}),
                 ('argv',self.prior['argv']+['--new-flag']),('model_generated_tokens_requested',1)]
        for field,value in changes:
            self.prior=deepcopy(good);self.prior[field]=value;self.save_prior()
            with self.subTest(field=field),self.assertRaises(ValueError):self.preflight()
        for field,value in [('returncode',1),('owned_job_empty',False),('reason','timeout'),
                            ('primary_thread_resumed',False),('primary_thread_handle_closed',False),
                            ('cleanup_errors',['failed']),('stdout_bytes',999),('native_pid',True)]:
            self.prior=deepcopy(good);self.prior['native_metrics'][field]=value;self.save_prior()
            with self.subTest(field=field),self.assertRaises(ValueError):self.preflight()
        self.assertEqual(len(self.calls),1)

    def test_stored_audit_flags_are_not_a_substitute_for_reaudit(self):
        self.counted();self.prior['prompt_audit']['token_ids_u32le_sha256']='f'*64;self.save_prior()
        with self.assertRaisesRegex(ValueError,'Recomputed'):self.preflight()
        self.assertEqual(len(self.calls),1)

    def test_different_actual_generation_prompt_count_is_preserved_but_rejected(self):
        self.counted();self.generated_count=2001
        with self.assertRaisesRegex(ValueError,'Generated prompt differs'):worker.run(self.gen_request,lambda event:None)
        result=json.loads((self.gen_dir/'generate.result.json').read_bytes())
        self.assertEqual(result['state'],'proposed');self.assertEqual(result['prompt_tokens'],2001)
        self.assertEqual(len(self.calls),2)
        with self.assertRaises(FileExistsError):worker.run(self.gen_request,lambda event:None)
        self.assertEqual(len(self.calls),2)

    def test_missing_eog_is_failure_with_no_proposal_or_automatic_retry(self):
        self.counted();self.eog=False
        returned=worker.run(self.gen_request,lambda event:None)
        self.assertEqual(returned['state'],'failed');self.assertFalse((self.gen_dir/'proposal.json').exists())
        self.assertTrue((self.gen_dir/'generate.stdout').exists())
        with self.assertRaises(FileExistsError):worker.run(self.gen_request,lambda event:None)
        self.assertEqual(len(self.calls),2)

    def test_strict_whole_json_completion_is_unchanged(self):
        self.counted();self.body+=b' trailing prose'
        returned=worker.run(self.gen_request,lambda event:None)
        self.assertEqual(returned['state'],'failed');self.assertTrue((self.gen_dir/'completion.raw').exists())
        self.assertFalse((self.gen_dir/'proposal.json').exists());self.assertEqual(len(self.calls),2)

    def test_prelaunch_ram_failure_consumes_shared_stage_without_native_retry(self):
        with patch.object(shared,'available_ram',return_value=5*1024**3):
            returned=worker.run(self.request,lambda event:None)
        self.assertEqual(returned['state'],'failed');self.assertEqual(self.calls,[])
        self.assertTrue((self.count_dir/'count.attempt.json').exists())
        with self.assertRaises(FileExistsError):worker.run(self.request,lambda event:None)
        self.assertEqual(self.calls,[])

    @unittest.skipUnless(os.name=='nt','Actual Windows archive sharing and deletion')
    def test_real_archive_preflight_reads_protected_gzip_without_recreating_raw(self):
        with patch.object(real_archive,'ROOT',self.root),patch.dict(sys.modules,{'audio.capture_archive':real_archive}):
            captured=self.counted_with_real_archives()
            before={p.name:p.read_bytes() for p in self.count_dir.iterdir()}
            held=[]
            def native_with_archive_checks(argv,**kwargs):
                for name in captured:
                    self.assertFalse((self.count_dir/name).exists())
                    for suffix in ('.gz','.archive.json'):
                        path=self.count_dir/(name+suffix)
                        for operation in (lambda p=path:p.write_bytes(b'changed'),lambda p=path:p.unlink(),
                                          lambda p=path:p.rename(p.with_name(p.name+'.moved'))):
                            with self.assertRaises(OSError):operation()
                        held.append(path.name)
                return self.native(argv,**kwargs)
            with patch.object(shared,'run_native',side_effect=native_with_archive_checks):
                returned=worker.run(self.gen_request,lambda event:None)
            self.assertEqual(returned['state'],'proposed');self.assertEqual(len(self.calls),2)
            self.assertEqual(self.archive_reads,[],'The fake archive reader must not service this integration.')
            self.assertEqual(self.audit_calls[1][0],captured['count.stderr'])
            self.assertEqual(set(held),{name+suffix for name in captured for suffix in ('.gz','.archive.json')})
            self.assertEqual(before,{p.name:p.read_bytes() for p in self.count_dir.iterdir()})
            self.assertTrue(all(not(self.count_dir/name).exists() for name in captured))
            self.assertFalse(any(p.name.endswith('.pending') for p in self.count_dir.iterdir()))

    @unittest.skipUnless(os.name=='nt','Actual Windows archive sharing and deletion')
    def test_real_archive_wrong_ownership_blocks_generation_and_preserves_failure(self):
        with patch.object(real_archive,'ROOT',self.root),patch.dict(sys.modules,{'audio.capture_archive':real_archive}):
            captured=self.counted_with_real_archives()
            path=self.count_dir/'captures.json';manifest=json.loads(path.read_bytes())
            manifest['captures']['count.stderr']['ownership']['ordinal']=3
            path.write_bytes(canonical(manifest));self.gen_request['capture_manifest_sha256']=sha(path.read_bytes())
            returned=worker.run(self.gen_request,lambda event:None)
            result=json.loads((self.gen_dir/'generate.result.json').read_bytes())
            self.assertEqual(returned['state'],'failed');self.assertIn('ownership',result['error'])
            self.assertEqual(len(self.calls),1);self.assertFalse((self.gen_dir/'generate.stdout').exists())
            self.assertTrue(all(not(self.count_dir/name).exists() for name in captured))
            self.assertTrue((self.gen_dir/'generate.attempt.json').exists())
            with self.assertRaises(FileExistsError):worker.run(self.gen_request,lambda event:None)
            self.assertEqual(len(self.calls),1)

    @unittest.skipUnless(os.name=='nt','Actual Windows archive sharing and deletion')
    def test_real_archive_receipt_tamper_blocks_generation_without_raw_fallback(self):
        with patch.object(real_archive,'ROOT',self.root),patch.dict(sys.modules,{'audio.capture_archive':real_archive}):
            captured=self.counted_with_real_archives()
            path=self.count_dir/'count.stderr.archive.json';path.write_bytes(path.read_bytes()+b' ')
            before={p.name:p.read_bytes() for p in self.count_dir.iterdir()}
            returned=worker.run(self.gen_request,lambda event:None)
            result=json.loads((self.gen_dir/'generate.result.json').read_bytes())
            self.assertEqual(returned['state'],'failed');self.assertIn('receipt changed',result['error'])
            self.assertEqual(len(self.calls),1);self.assertFalse((self.gen_dir/'proposal.json').exists())
            self.assertTrue(all(not(self.count_dir/name).exists() for name in captured))
            self.assertEqual(before,{p.name:p.read_bytes() for p in self.count_dir.iterdir()})


if __name__=='__main__':unittest.main()
