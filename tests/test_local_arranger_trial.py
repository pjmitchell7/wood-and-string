from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio import local_arranger_trial as trial
from audio.arranger_prompt import canonical
from tests.test_arranger_contract import context


class RuntimeTrial(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'trial';self.root.mkdir();self.directory=self.root/'case';self.directory.mkdir()
        self.addCleanup(patch.stopall);patch.object(trial,'TRIAL_ROOT',self.root).start()
        self.request={'operation':'count','work_dir':str(self.directory)}
        raw=canonical(context());(self.directory/'context.json').write_bytes(raw)
        self.request['context_sha256']=hashlib.sha256(raw).hexdigest()
        self.protected=False;self.token_count=1000;self.calls=[]
        @contextmanager
        def guard():
            self.protected=True
            try:yield {'identity_sha256':'a'*64,'executable':str(self.root/'completion.exe'),
                       'tokenize':str(self.root/'tokenize.exe'),'model':str(self.root/'model.gguf')}
            finally:self.protected=False
        patch.object(trial,'protect_runtime',guard).start()
        patch.object(trial,'available_ram',return_value=9*1024**3).start()
        patch.object(trial,'run_native',side_effect=self.native).start()

    def native(self,argv,**kwargs):
        self.assertTrue(self.protected)
        self.calls.append((argv,kwargs))
        kwargs['stdout_path'].write_bytes(b'')
        kwargs['stderr_path'].write_bytes(f'llama_completion: number of tokens in prompt = {self.token_count}\n'.encode())
        return {'returncode':0,'native_pid':123,'elapsed_seconds':.1,'peak_working_set_bytes':1024}

    def result(self):
        receipt=trial.run(self.request,lambda event:None)
        path=self.directory/receipt['receipt'];self.assertEqual(trial.digest(path),receipt['sha256'])
        self.assertFalse(self.protected)
        return json.loads(path.read_text())

    def test_count_is_zero_generation_fixed_cpu_offline_and_keeps_evidence(self):
        result=self.result();self.assertEqual(result['state'],'counted');self.assertTrue(result['admitted_for_generation'])
        self.assertEqual(result['prompt_tokens'],1000);self.assertEqual(result['model_generated_tokens_requested'],0)
        argv,kwargs=self.calls[0]
        for flag,value in [('--n-predict','0'),('--ctx-size','4096'),('--gpu-layers','0'),('--reasoning','off')]:
            self.assertEqual(argv[argv.index(flag)+1],value)
        self.assertIn('--offline',argv);self.assertIn('--no-context-shift',argv)
        self.assertEqual(kwargs['wall_seconds'],120)
        self.assertEqual(set(result['captures']),{'count.stdout','count.stderr'})
        self.assertEqual(result['context_sha256'],self.request['context_sha256'])
        self.assertEqual(json.loads((self.directory/'context.json').read_text()),context())

    def test_overflow_is_rejected_without_evidence_truncation(self):
        self.token_count=3073;result=self.result()
        self.assertEqual(result['state'],'rejected_prompt_budget');self.assertFalse(result['admitted_for_generation'])
        self.assertEqual(len(self.calls),1);self.assertIn('n1',(self.directory/'prompt.txt').read_text())

    def test_memory_and_context_failures_happen_before_native(self):
        with patch.object(trial,'available_ram',return_value=8*1024**3-1):result=self.result()
        self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])
        self.assertIn('8 GiB',result['error'])

    def test_memory_is_rechecked_after_package_verification(self):
        with patch.object(trial,'available_ram',side_effect=[9*1024**3,8*1024**3-1]):result=self.result()
        self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])
        self.assertIn('immediately',result['error'])

    def test_exact_context_prompt_and_schema_are_write_protected_during_native(self):
        original=self.native
        def attempted_edit(argv,**kwargs):
            for name in ('context.json','prompt.txt','proposal.schema.json'):
                path=self.directory/name
                before=path.read_bytes()
                with self.assertRaises(PermissionError):path.write_bytes(b'changed evidence')
                self.assertEqual(path.read_bytes(),before)
            return original(argv,**kwargs)
        with patch.object(trial,'run_native',side_effect=attempted_edit):result=self.result()
        self.assertEqual(result['state'],'counted')
        for name,receipt in result['input_files'].items():
            self.assertEqual(trial.digest(self.directory/name),receipt['sha256'])
        (self.directory/'prompt.txt').write_bytes(b'Protection released after the run')

    def test_hash_tamper_and_occupied_attempt_preserve_files(self):
        (self.directory/'context.json').write_bytes(b'{}')
        result=self.result();self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])
        before=(self.directory/'count.result.json').read_bytes()
        with self.assertRaises(FileExistsError):trial.run(self.request,lambda event:None)
        self.assertEqual((self.directory/'count.result.json').read_bytes(),before)

    def test_no_arbitrary_operation_directory_or_flags(self):
        for changes in [{'operation':'generate'},{'argv':['unwanted']},{'work_dir':str(self.root.parent)},
                        {'operation':'help'}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):trial.validate_request({**self.request,**changes})
        self.assertEqual(self.calls,[])

    def test_metadata_probes_never_load_a_model(self):
        for number,op in enumerate(('help','version','tokenize-help')):
            directory=self.root/f'probe-{number}';directory.mkdir()
            receipt=trial.run({'operation':op,'work_dir':str(directory)},lambda event:None)
            self.assertEqual(receipt['state'],'checked')
            argv,_=self.calls[-1]
            self.assertEqual(argv[-1],'--help' if op=='tokenize-help' else '--'+op)
            self.assertEqual(len(argv),2)

    def test_prompt_count_requires_one_unambiguous_native_diagnostic(self):
        self.assertEqual(trial.parse_prompt_count(b'llama_completion: number of tokens in prompt = 3072\r\n'),3072)
        for raw in [b'1000',b'llama_completion: number of tokens in prompt = 0\n',
                    b'main: number of tokens in prompt = 2\n',
                    b'llama_completion: number of tokens in prompt = 1\nllama_completion: number of tokens in prompt = 2\n']:
            with self.assertRaises(ValueError):trial.parse_prompt_count(raw)


if __name__=='__main__':unittest.main()
