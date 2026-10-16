from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio import local_arranger_gpu_trial as trial
from audio.arranger_prompt import canonical
from tests.test_arranger_contract import context

GiB=1024**3
LISTING=b'Available devices:\n  Vulkan0: Intel(R) UHD Graphics 630 (8192 MiB, 7000 MiB free)\n  Vulkan1: NVIDIA GeForce GTX 1660 Ti (6144 MiB, 5966 MiB free)\n'


class FakeGpu:
    identity={'name':'NVIDIA GeForce GTX 1660 Ti','nvml_index':0}
    def __init__(self):self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def snapshot(self):return {'total_bytes':6*GiB,'free_bytes':int(5.8*GiB),'reported_used_bytes':int(.2*GiB),'unreported_reserved_bytes':0}


class GpuTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'gpu-trial';self.root.mkdir();self.directory=self.root/'case';self.directory.mkdir()
        self.addCleanup(patch.stopall);patch.object(trial,'TRIAL_ROOT',self.root).start()
        self.device_path=self.root/'metadata/devices/devices.result.json';self.device_path.parent.mkdir(parents=True)
        patch.object(trial,'DEVICE_RESULT',self.device_path).start()
        raw=canonical(context());(self.directory/'context.json').write_bytes(raw)
        self.request={'operation':'count','work_dir':str(self.directory),'context_sha256':hashlib.sha256(raw).hexdigest()}
        capture=self.device_path.parent/'devices.stdout';capture.write_bytes(LISTING)
        self.device_receipt={'state':'checked','operation':'devices','runtime_identity_sha256':'a'*64,
            'selected_device':trial.parse_devices(LISTING),
            'captures':{'devices.stdout':{'bytes':len(LISTING),'sha256':hashlib.sha256(LISTING).hexdigest()}}}
        self.save_device();self.protected=False;self.calls=[];self.tokens=1000;self.gpu=FakeGpu()
        @contextmanager
        def guard():
            self.protected=True
            try:yield {'identity_sha256':'a'*64,'executable':str(self.root/'completion.exe'),'model':str(self.root/'model.gguf')}
            finally:self.protected=False
        patch.object(trial,'runtime_guard',guard).start()
        patch.object(trial,'available_ram',return_value=int(6.5*GiB)).start()
        patch.object(trial,'NvidiaMemory',return_value=self.gpu).start()
        patch.object(trial,'run_native',side_effect=self.native).start()

    def save_device(self):
        self.device_path.write_bytes(canonical(self.device_receipt))
        self.request['device_receipt_sha256']=trial.digest(self.device_path)

    def native(self,argv,**kwargs):
        self.assertTrue(self.protected);self.calls.append((argv,kwargs))
        if kwargs['before_resume']:
            kwargs['before_resume']();self.assertFalse(kwargs['cancel']())
        kwargs['stdout_path'].write_bytes(LISTING if '--list-devices' in argv else b'')
        kwargs['stderr_path'].write_bytes((f'llama_prepare_model_devices: using device Vulkan1 (NVIDIA GeForce GTX 1660 Ti) (fake-device-id) - 5966 MiB free\nload_tensors: offloaded 37/37 layers to GPU\nllama_completion: number of tokens in prompt = {self.tokens}\n').encode())
        return {'native_pid':123,'returncode':0,'owned_job_empty':True}

    def result(self):
        receipt=trial.run(self.request,lambda event:None);path=self.directory/receipt['receipt']
        self.assertEqual(trial.digest(path),receipt['sha256']);self.assertFalse(self.protected)
        return json.loads(path.read_text())

    def test_explicit_nvidia_device_and_fixed_zero_generation_profile(self):
        result=self.result();self.assertEqual(result['state'],'counted',result)
        self.assertFalse(result['generation_gate_released']);self.assertTrue(result['fits_prompt_budget'])
        self.assertEqual(result['selected_device']['device'],'Vulkan1')
        argv,kwargs=self.calls[0]
        for flag,value in [('--device','Vulkan1'),('--n-predict','0'),('--gpu-layers','all'),('--fit','off'),('--ctx-size','4096'),('--split-mode','none')]:
            self.assertEqual(argv[argv.index(flag)+1],value)
        self.assertIn('--offline',argv);self.assertIn('--no-warmup',argv)
        self.assertEqual(kwargs['memory_bytes'],int(3.5*GiB))
        self.assertEqual(result['resources']['fresh_pre_resume_samples'],1)
        self.assertTrue(self.gpu.closed)

    def test_all_symbolic_and_native_device_evidence_are_protected(self):
        original=self.native
        def attempted_edit(argv,**kwargs):
            for path in [self.directory/name for name in ('context.json','prompt.txt','proposal.schema.json')]+[self.device_path,self.device_path.parent/'devices.stdout']:
                before=path.read_bytes()
                with self.assertRaises(PermissionError):path.write_bytes(b'changed')
                self.assertEqual(path.read_bytes(),before)
            return original(argv,**kwargs)
        with patch.object(trial,'run_native',side_effect=attempted_edit):result=self.result()
        self.assertEqual(result['state'],'counted',result)

    def test_device_receipt_runtime_and_capture_tamper_fail_before_native(self):
        for mode in ('runtime','capture','hash'):
            with self.subTest(mode=mode):
                if mode=='runtime':self.device_receipt['runtime_identity_sha256']='b'*64;self.save_device()
                elif mode=='capture':
                    self.device_receipt['runtime_identity_sha256']='a'*64;self.save_device()
                    (self.device_path.parent/'devices.stdout').write_bytes(b'changed')
                else:self.request['device_receipt_sha256']='c'*64
                if mode!='runtime':
                    # Fresh attempts preserve every failure result in prior folder.
                    self.directory=self.root/mode;self.directory.mkdir()
                    (self.directory/'context.json').write_bytes(canonical(context()))
                    self.request['work_dir']=str(self.directory)
                result=self.result();self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])

    def test_host_admission_failure_does_not_launch(self):
        with patch.object(trial,'available_ram',return_value=6*GiB-1):result=self.result()
        self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])
        self.assertIn('6 GiB',result['error']);self.assertTrue(self.gpu.closed)

    def test_complete_offload_is_required(self):
        device=trial.parse_devices(LISTING)
        binding=b'llama_prepare_model_devices: using device Vulkan1 (NVIDIA GeForce GTX 1660 Ti) (fake-id) - 5966 MiB free\n'
        for raw in (b'load_tensors: offloaded 36/37 layers to GPU\n',b'',b'load_tensors: offloaded 37/37 layers to GPU\nwarning: no usable GPU found\n'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):trial.offload_evidence(binding+raw,device)
        good=binding+b'load_tensors: offloaded 37/37 layers to GPU\n'
        self.assertEqual(trial.offload_evidence(good,device)['current_device'],'Vulkan1')
        for raw in (good.replace(b'Vulkan1',b'Vulkan0'),good.replace(b'NVIDIA GeForce GTX 1660 Ti',b'Intel UHD 630'),good+binding):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):trial.offload_evidence(raw,device)

    def test_device_parser_rejects_missing_ambiguous_or_wrong_gpu(self):
        self.assertEqual(trial.parse_devices(LISTING)['device'],'Vulkan1')
        self.assertEqual(trial.parse_devices(LISTING.replace(b'6144',b'6180'))['listed_total_mib'],6180)
        for raw in (b'',LISTING.replace(b'1660 Ti',b'1650 Ti'),LISTING+LISTING.splitlines(keepends=True)[-1],LISTING.replace(b'6144',b'0')):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):trial.parse_devices(raw)

    def test_metadata_never_passes_model_or_nvml(self):
        for op in ('help','version','devices'):
            directory=self.root/op;directory.mkdir()
            receipt=trial.run({'operation':op,'work_dir':str(directory)},lambda event:None)
            self.assertEqual(receipt['state'],'checked')
            argv,kwargs=self.calls[-1];self.assertEqual(len(argv),2);self.assertIsNone(kwargs['before_resume'])
        self.assertFalse(self.gpu.closed)

    def test_unknown_request_keys_or_outside_directory_rejected(self):
        for changes in ({'operation':'generate'},{'argv':['unwanted']},{'work_dir':str(self.root.parent)},{'device_receipt_sha256':'invalid'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):trial.validate_request({**self.request,**changes})

    def test_prompt_overflow_retains_full_inputs_and_no_generation_gate(self):
        self.tokens=3073;result=self.result()
        self.assertEqual(result['state'],'rejected_prompt_budget');self.assertFalse(result['fits_prompt_budget'])
        self.assertFalse(result['generation_gate_released']);self.assertIn('n1',(self.directory/'prompt.txt').read_text())

    def test_corrected_transport_protects_template_and_audits_exact_prompt(self):
        from audio import qwen_prompt_audit as audit
        model=self.root/'model.gguf';model.write_bytes(b'guarded fake model')
        original=self.native
        def native(argv,**kwargs):
            template=self.directory/'chat-template.jinja'
            self.assertEqual(template.read_bytes(),audit.NONTHINK_TEMPLATE)
            with self.assertRaises(PermissionError):template.write_bytes(b'changed')
            return original(argv,**kwargs)
        def check(raw,stream,prompt):
            self.assertTrue(self.protected)
            self.assertEqual(stream.read(),b'guarded fake model')
            self.assertEqual(prompt,(self.directory/'prompt.txt').read_bytes())
            self.assertTrue(prompt.endswith(b'\n'))
            with self.assertRaises(PermissionError):model.write_bytes(b'changed')
            return {'count':1000,'entire_single_user_template_matches':True,'reasoning_template_state':'empty_think_block_prefilled'}
        with patch.object(trial,'run_native',side_effect=native),patch.object(audit,'audit_capture',side_effect=check):
            receipt=trial.run_probe(self.request,lambda event:None,transport_template=audit.NONTHINK_TEMPLATE)
        result=json.loads((self.directory/receipt['receipt']).read_text());self.assertEqual(result['state'],'counted',result)
        self.assertFalse(result['generation_gate_released']);argv,_=self.calls[0]
        self.assertNotIn('--file',argv)
        for flag,value in [('--binary-file',str(self.directory/'prompt.txt')),('--chat-template-file',str(self.directory/'chat-template.jinja')),('--verbosity','4'),('--log-colors','off')]:
            self.assertEqual(argv[argv.index(flag)+1],value)
        for flag in ('--no-escape','--no-log-prefix','--no-log-timestamps','--no-log-jsonl'):self.assertIn(flag,argv)
        self.assertEqual(result['input_files']['chat-template.jinja']['sha256'],audit.NONTHINK_TEMPLATE_SHA256)

    def test_corrected_transport_never_accepts_audit_mismatch(self):
        from audio import qwen_prompt_audit as audit
        (self.root/'model.gguf').write_bytes(b'fake')
        with patch.object(audit,'audit_capture',return_value={'count':1000,'entire_single_user_template_matches':False,
                'reasoning_template_state':'reasoning_off_not_established'}):
            receipt=trial.run_probe(self.request,lambda event:None,transport_template=audit.NONTHINK_TEMPLATE)
        result=json.loads((self.directory/receipt['receipt']).read_text())
        self.assertEqual(result['state'],'failed');self.assertFalse(result['generation_gate_released'])
        self.assertIn('Actual tokenized prompt',result['error'])


if __name__=='__main__':unittest.main()
