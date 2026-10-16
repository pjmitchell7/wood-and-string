"""Protocol-only checks: no native subprocess, model access or private holdout."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from audio import local_arranger_batch_plan as plan
from audio.arranger_prompt import canonical


class BatchPlanTests(unittest.TestCase):
    def fixture(self):
        inputs={name:{'bytes':10,'sha256':str(i)*64} for i,name in enumerate(
            ('context.json','prompt.txt','proposal.schema.json','chat-template.jinja'),1)}
        captures={'count.stdout':b'\r\n','count.stderr':b'synthetic bounded diagnostics'}
        value={'version':plan.VERSION,'state':'counted','operation':'count',
            'runtime_identity_sha256':plan.RUNTIME_ID,'model_generated_tokens_requested':0,
            'generation_gate_released':False,'fits_prompt_budget':True,'prompt_tokens':3072,
            'context_tokens':4096,'required_output_reserve':1024,'profile':dict(plan.PROFILE),
            'resources':{'profile':dict(plan.PROFILE)},'argv':['fixed','count'],
            'selected_device':{'device':'Vulkan1'},'input_files':inputs,
            'native_metrics':{'returncode':0,'owned_job_empty':True},
            'context_sha256':inputs['context.json']['sha256'],'prompt_sha256':inputs['prompt.txt']['sha256'],
            'schema_sha256':inputs['proposal.schema.json']['sha256'],
            'prompt_audit':{'count':3072,'entire_single_user_template_matches':True,
                'reasoning_template_state':'empty_think_block_prefilled','exact_prompt_bytes':10,
                'exact_prompt_sha256':inputs['prompt.txt']['sha256'],'capture_sha256':plan.sha(captures['count.stderr'])},
            'captures':{name:{'bytes':len(raw),'sha256':plan.sha(raw)} for name,raw in captures.items()}}
        return value,inputs,captures

    def check(self,value,inputs,captures):
        return plan.validate_count(value,inputs,['fixed','count'],{'device':'Vulkan1'},captures)

    def test_exact_boundary_passes_and_changed_runtime_profile_transport_or_case_rejects(self):
        value,inputs,captures=self.fixture()
        self.assertEqual(self.check(value,inputs,captures)['prompt_tokens'],3072)
        mutations={'prompt_tokens':3073,'context_tokens':8192,'required_output_reserve':512,
            'runtime_identity_sha256':'different','profile':{},'argv':['fixed','different'],
            'selected_device':{'device':'Vulkan0'},'input_files':{},'model_generated_tokens_requested':1,
            'generation_gate_released':True,'state':'failed','version':'old'}
        for key,replacement in mutations.items():
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.check({**value,key:replacement},inputs,captures)
        changed=deepcopy(inputs);changed['context.json']['sha256']='a'*64
        with self.assertRaises(ValueError):self.check(value,changed,captures)

    def test_hash_bound_result_and_raw_captures_cannot_be_substituted(self):
        value,inputs,captures=self.fixture();raw=canonical(value)
        receipt={'receipt':'count.result.json','bytes':len(raw),'sha256':plan.sha(raw)}
        self.assertEqual(plan.authenticate_count(raw,receipt),value)
        with self.assertRaises(ValueError):plan.authenticate_count(raw+b' ',receipt)
        with self.assertRaises(ValueError):self.check(value,inputs,{**captures,'count.stderr':b'changed'})
        for key in ('count','exact_prompt_bytes','exact_prompt_sha256','capture_sha256','reasoning_template_state'):
            altered=deepcopy(value);altered['prompt_audit'][key]='changed'
            with self.subTest(key=key),self.assertRaises(ValueError):self.check(altered,inputs,captures)
        altered=deepcopy(value);altered['native_metrics']['owned_job_empty']=False
        with self.assertRaises(ValueError):self.check(altered,inputs,captures)

    def test_boolean_token_count_or_supervisor_size_cannot_authenticate_numeric_identity(self):
        value,inputs,captures=self.fixture();value['prompt_tokens']=1;value['prompt_audit']['count']=True
        with self.assertRaises(ValueError):self.check(value,inputs,captures)
        with self.assertRaises(ValueError):plan.authenticate_count(b'1',{'receipt':'count.result.json','bytes':True,'sha256':plan.sha(b'1')})
        for prior in (None,[],1):
            with self.subTest(prior=prior),self.assertRaises(ValueError):self.check(prior,inputs,captures)

    def test_duplicate_nonfinite_and_oversize_metadata_rejected(self):
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b' '*65537):
            with self.subTest(raw=raw[:20]),self.assertRaises(ValueError):plan.strict_json(raw,65536)

    def test_case1_never_reruns_and_failed_consumed_stages_never_retry(self):
        self.assertEqual(plan.next_action(1),'reuse_preserved_actual_case1')
        with self.assertRaises(ValueError):plan.next_action(1,count_attempted=True)
        for ordinal in range(2,9):
            self.assertEqual(plan.next_action(ordinal),'count')
            self.assertEqual(plan.next_action(ordinal,count_attempted=True),'terminal_count_not_admitted')
            self.assertEqual(plan.next_action(ordinal,count_attempted=True,count_admitted=True),'generate')
            self.assertEqual(plan.next_action(ordinal,count_attempted=True,count_admitted=True,generation_attempted=True),'terminal_no_retry')
        with self.assertRaises(ValueError):plan.next_action(2,generation_attempted=True)

    def test_storage_reserves_temporary_archives_without_new_allowance(self):
        used=plan.CAPTURE_CAP-plan.STAGE_RESERVE
        self.assertFalse(plan.admit_storage(used,0)['new_budget_created'])
        with self.assertRaises(ValueError):plan.admit_storage(used,1)
        with self.assertRaises(ValueError):plan.admit_storage(True,0)

    def test_actual_public_only_manifest_preserves_all_eight_inputs_and_factories(self):
        root=Path(__file__).resolve().parents[1]
        # Saved trial receipts are private development evidence, not fixtures
        # distributed with the source-only public checkout.
        private_receipts=(root/'work/local-arranger-generation-001/attempt-02/frozen.json',
                          root/'work/arranger-baseline-001/frozen.json')
        if not all(path.is_file() for path in private_receipts):
            self.skipTest('Private first-forward and baseline receipts are unavailable')
        public=(root/'audio/research/arranger_benchmark_v1.json').read_bytes()
        forward=private_receipts[0].read_bytes()
        baseline=private_receipts[1].read_bytes()
        required={**json.loads(forward)['inputs'],**json.loads(baseline)['inputs']}
        current={name:plan.sha((root/name).read_bytes()) for name in required}
        result=plan.build_plan(public,forward,baseline,current)
        self.assertEqual(tuple(row['case_id'] for row in result['cases']),plan.PUBLIC_IDS)
        self.assertEqual(len(result['cases']),8)
        self.assertFalse(result['holdout_read']);self.assertFalse(result['native_execution_allowed'])
        self.assertEqual(result['new_generation_attempts_maximum'],7)
        for row in result['cases']:
            self.assertEqual(set(row['input_files']),{'context.json','prompt.txt','proposal.schema.json','chat-template.jinja'})
            self.assertNotIn('expectations',row)
        with self.assertRaises(ValueError):plan.build_plan(public+b' ',forward,baseline,current)
        current['audio/arranger_prompt.py']='0'*64
        with self.assertRaises(ValueError):plan.build_plan(public,forward,baseline,current)


if __name__=='__main__':unittest.main()
