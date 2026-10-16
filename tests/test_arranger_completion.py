from copy import deepcopy
import json
from pathlib import Path
import unittest

from audio.arranger_baseline import make_baseline
from audio.arranger_completion import WINDOWS_EOG_TRAILER, MAX_STDERR_BYTES, extract_completion, verify_eog
from audio.arranger_evaluation import evaluate_case
from audio.arranger_prompt import MAX_RESPONSE_BYTES, canonical, parse_response


class CompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture=Path(__file__).resolve().parents[1]/'audio/research/arranger_benchmark_v1.json'
        cls.case=json.loads(fixture.read_bytes())['cases'][0]
        cls.context=cls.case['input'];cls.proposal=make_baseline(cls.context)
        cls.body=canonical(cls.proposal)

    def test_valid_entire_json_body_round_trips(self):
        body=extract_completion(self.body+WINDOWS_EOG_TRAILER)
        proposal,resolved=parse_response(self.context,body)
        self.assertEqual(body,self.body);self.assertEqual(proposal,self.proposal)
        self.assertEqual(resolved['status'],'plan')

    def test_all_body_whitespace_is_preserved(self):
        body=b' \r\n\t'+self.body+b'\r\n \t'
        extracted=extract_completion(body+WINDOWS_EOG_TRAILER)
        self.assertEqual(extracted,body)
        self.assertEqual(parse_response(self.context,extracted)[0],self.proposal)

    def test_token_budget_exhaustion_without_eog_frame_rejected(self):
        # Pinned normal loop exit prints only its two final newlines.
        for raw in [self.body,self.body+b'\r\n\r\n']:
            with self.assertRaises(ValueError):extract_completion(raw)

    def test_truncated_frame_rejected(self):
        for count in (1,2,3,4,8):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):extract_completion((self.body+WINDOWS_EOG_TRAILER)[:-count])

    def test_only_pinned_windows_newlines_accepted(self):
        for trailer in [b' [end of text]\n\n\n',b' [end of text]\r\n\r\n',
                        b'[end of text]\r\n\r\n\r\n',b' [end of text] \r\n\r\n\r\n']:
            with self.subTest(trailer=trailer):
                with self.assertRaises(ValueError):extract_completion(self.body+trailer)

    def test_trailing_extra_bytes_rejected(self):
        for extra in [b' ',b'\r\n',b'banner',b'\0']:
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):extract_completion(self.body+WINDOWS_EOG_TRAILER+extra)

    def test_fake_embedded_frame_remains_for_whole_json_rejection(self):
        body=self.body+WINDOWS_EOG_TRAILER+b'extra'
        extracted=extract_completion(body+WINDOWS_EOG_TRAILER)
        self.assertEqual(extracted,body)
        with self.assertRaises(ValueError):parse_response(self.context,extracted)

    def test_duplicate_terminal_frame_removes_only_one_and_rejects_json(self):
        extracted=extract_completion(self.body+WINDOWS_EOG_TRAILER*2)
        self.assertEqual(extracted,self.body+WINDOWS_EOG_TRAILER)
        with self.assertRaises(ValueError):parse_response(self.context,extracted)

    def test_marker_text_inside_valid_json_is_not_globally_removed(self):
        proposal=self.abstain('Observed text contains [end of text] as data.')
        body=canonical(proposal);extracted=extract_completion(body+WINDOWS_EOG_TRAILER)
        self.assertEqual(body,extracted)
        self.assertEqual(parse_response(self.context,extracted)[0],proposal)

    def test_prefix_text_retained_for_strict_parser_to_reject(self):
        for prefix in [b'Here is your JSON:\r\n',b'\xef\xbb\xbf',b'\x1b[32m',b'> ']:
            body=prefix+self.body;extracted=extract_completion(body+WINDOWS_EOG_TRAILER)
            self.assertEqual(extracted,body)
            with self.assertRaises(ValueError):parse_response(self.context,extracted)

    def test_markdown_fence_retained_and_rejected(self):
        body=b'```json\r\n'+self.body+b'\r\n```'
        self.assertEqual(extract_completion(body+WINDOWS_EOG_TRAILER),body)
        with self.assertRaises(ValueError):parse_response(self.context,body)

    def test_second_json_object_is_not_salvaged(self):
        body=self.body+b'\r\n{}'
        with self.assertRaises(ValueError):parse_response(self.context,extract_completion(body+WINDOWS_EOG_TRAILER))

    def test_duplicate_json_key_is_not_repaired(self):
        body=b'{"status":"plan",'+self.body[1:]
        with self.assertRaises(ValueError):parse_response(self.context,extract_completion(body+WINDOWS_EOG_TRAILER))

    def test_truncated_json_even_with_frame_rejected(self):
        with self.assertRaises(ValueError):
            parse_response(self.context,extract_completion(self.body[:-1]+WINDOWS_EOG_TRAILER))

    def test_invalid_utf8_is_not_replaced(self):
        body=self.body+b'\xe2\x82'
        self.assertEqual(extract_completion(body+WINDOWS_EOG_TRAILER),body)
        with self.assertRaises(ValueError):parse_response(self.context,body)

    def test_nonfinite_value_rejected(self):
        body=self.body.replace(b'"octave":0',b'"octave":NaN',1)
        self.assertNotEqual(body,self.body)
        with self.assertRaises(ValueError):parse_response(self.context,extract_completion(body+WINDOWS_EOG_TRAILER))

    def test_unknown_source_id_rejected_after_framing(self):
        proposal=deepcopy(self.proposal);proposal['assignments'][0]['note_id']='invented-source'
        with self.assertRaises(ValueError):parse_response(self.context,extract_completion(canonical(proposal)+WINDOWS_EOG_TRAILER))

    def test_native_trailer_does_not_turn_refusal_text_into_json(self):
        body=b'I cannot produce this arrangement.'
        self.assertEqual(extract_completion(body+WINDOWS_EOG_TRAILER),body)
        with self.assertRaises(ValueError):parse_response(self.context,body)

    def test_valid_abstention_is_not_a_benchmark_plan_pass(self):
        proposal=self.abstain('Bounded test abstention.')
        parsed,_=parse_response(self.context,extract_completion(canonical(proposal)+WINDOWS_EOG_TRAILER))
        report=evaluate_case(self.case,parsed)
        self.assertIsNone(report['hard_violation'])
        self.assertIn('Unexpected plan/abstention status',report['expectation_failures'])

    def test_nonascii_json_preserved(self):
        proposal=self.abstain('caf\u00e9 \u65e5\u672c\u8a9e \U0001f3b8')
        body=json.dumps(proposal,ensure_ascii=False).encode('utf8')
        self.assertEqual(parse_response(self.context,extract_completion(body+WINDOWS_EOG_TRAILER))[0],proposal)

    def test_body_bound_applies_to_entire_response(self):
        body=self.body+b' '*(MAX_RESPONSE_BYTES-len(self.body))
        self.assertEqual(len(extract_completion(body+WINDOWS_EOG_TRAILER)),MAX_RESPONSE_BYTES)
        parse_response(self.context,body)
        with self.assertRaises(ValueError):extract_completion(body+b' '+WINDOWS_EOG_TRAILER)

    def test_invalid_type_empty_or_frame_only_rejected(self):
        for raw in [None,'text',bytearray(self.body+WINDOWS_EOG_TRAILER),b'',WINDOWS_EOG_TRAILER]:
            with self.subTest(raw=type(raw).__name__):
                with self.assertRaises(ValueError):extract_completion(raw)

    @classmethod
    def abstain(cls,reason):
        return {'version':cls.proposal['version'],'status':'abstain','assignments':[],
                'omissions':[],'abstention':reason}


def stderr_fixture(generated=3,prompt=1888):
    counters=b''.join(f'n_remain: {value}\r\n'.encode() for value in range(1023,1023-generated,-1))
    return (b'other pinned native diagnostics\r\n'+counters+b'found an EOG token\r\n'
            +f'common_perf_print:    samplers time =       5.00 ms / {prompt+generated:5d} tokens\r\n'.encode())


class EogTests(unittest.TestCase):
    def test_accepted_generation_and_total_prompt_counter_match(self):
        raw=stderr_fixture();result=verify_eog(raw,prompt_count=1888)
        self.assertEqual(result['accepted_generated_tokens_including_eog'],3)
        self.assertEqual(result['sampler_total_accepted_tokens'],1891)
        self.assertEqual(result['remaining_output_budget'],1021)
        self.assertEqual(result['capture_sha256'],__import__('hashlib').sha256(raw).hexdigest())
        self.assertLess(len(json.dumps(result)),1024)

    def test_eog_at_budget_boundary_is_valid_when_independently_diagnosed(self):
        result=verify_eog(stderr_fixture(1024),prompt_count=1888)
        self.assertEqual(result['accepted_generated_tokens_including_eog'],1024)
        self.assertEqual(result['remaining_output_budget'],0)

    def test_one_accepted_eog_is_counted_but_does_not_claim_valid_json(self):
        self.assertEqual(verify_eog(stderr_fixture(1),prompt_count=1888)['accepted_generated_tokens_including_eog'],1)

    def test_missing_duplicate_or_embedded_only_eog_rejected(self):
        raw=stderr_fixture()
        for changed in [raw.replace(b'found an EOG token\r\n',b''),raw+b'found an EOG token\r\n',
                        raw.replace(b'found an EOG token',b'text says found an EOG token')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):verify_eog(changed,prompt_count=1888)

    def test_eog_before_final_counter_rejected(self):
        raw=stderr_fixture().replace(b'found an EOG token\r\n',b'')
        raw=b'found an EOG token\r\n'+raw
        with self.assertRaises(ValueError):verify_eog(raw,prompt_count=1888)

    def test_skipped_duplicated_out_of_order_and_noncanonical_remaining_rejected(self):
        raw=stderr_fixture()
        for changed in [raw.replace(b'n_remain: 1023\r\n',b''),
                        raw.replace(b'n_remain: 1022\r\n',b'n_remain: 1023\r\n'),
                        raw.replace(b'n_remain: 1022\r\n',b'n_remain: 1020\r\n'),
                        raw.replace(b'n_remain: 1023\r\n',b'n_remain: 01023\r\n')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):verify_eog(changed,prompt_count=1888)

    def test_more_than_budget_or_no_generation_rejected(self):
        for count in (0,1025):
            with self.assertRaises(ValueError):verify_eog(stderr_fixture(count),prompt_count=1888)

    def test_negative_final_counter_rejected(self):
        raw=stderr_fixture().replace(b'n_remain: 1021',b'n_remain: -1')
        with self.assertRaises(ValueError):verify_eog(raw,prompt_count=1888)

    def test_sampler_counter_must_include_prompt_accepts(self):
        raw=stderr_fixture().replace(b' 1891 tokens',b'    3 tokens')
        with self.assertRaises(ValueError):verify_eog(raw,prompt_count=1888)

    def test_sampler_counter_off_by_one_rejected(self):
        raw=stderr_fixture().replace(b' 1891 tokens',b' 1892 tokens')
        with self.assertRaises(ValueError):verify_eog(raw,prompt_count=1888)

    def test_grammar_resampling_does_not_add_accept_counter_entries(self):
        raw=stderr_fixture().replace(b'n_remain: 1022',b'grammar diagnostic about resampling\r\nn_remain: 1022')
        self.assertEqual(verify_eog(raw,prompt_count=1888)['sampler_total_accepted_tokens'],1891)

    def test_missing_duplicate_or_premature_perf_summary_rejected(self):
        raw=stderr_fixture();head,perf=raw.split(b'common_perf_print:',1)
        for changed in [head,raw+b'common_perf_print:'+perf,b'common_perf_print:'+perf+head]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):verify_eog(changed,prompt_count=1888)

    def test_unexpected_log_prefix_or_lf_format_rejected(self):
        raw=stderr_fixture()
        for changed in [raw.replace(b'found an EOG token',b'0.03.750.131 I found an EOG token'),
                        raw.replace(b'\r\n',b'\n')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):verify_eog(changed,prompt_count=1888)

    def test_stderr_and_fixed_argument_bounds(self):
        for raw,prompt,budget in [(b'',1888,1024),('text',1888,1024),(b'x'*(MAX_STDERR_BYTES+1),1888,1024),
                                  (stderr_fixture(),True,1024),(stderr_fixture(),0,1024),
                                  (stderr_fixture(),3073,1024),(stderr_fixture(),1888,1023),
                                  (stderr_fixture(),1888,True)]:
            with self.subTest(prompt=prompt,budget=budget):
                with self.assertRaises(ValueError):verify_eog(raw,prompt_count=prompt,token_budget=budget)


if __name__=='__main__':unittest.main()
