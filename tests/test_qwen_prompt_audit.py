import hashlib
import io
import json
import struct
import unittest
from unittest.mock import patch

from audio import qwen_prompt_audit as q


def capture(pieces, ids=None, prefix=False, newline=b'\n', before=b''):
    ids=list(range(len(pieces))) if ids is None else ids
    log=b'0.03.750.131 I ' if prefix else b''
    result=before+log+b'llama_completion: number of tokens in prompt = '+str(len(ids)).encode()+newline
    for token in ids:
        piece=pieces[token]
        if newline==b'\r\n':piece=piece.replace(b'\n',b'\r\n')
        result+=log+f'{token:6d} -> \x27'.encode()+piece+b"'"+newline
    return result+log+newline+b'other diagnostics'+newline


def envelope(user,suffix=q.EMPTY_THINK_SUFFIX):
    return b'<|im_start|>user\n'+user+b'<|im_end|>\n'+q.ASSISTANT_PREFIX+suffix


def string(raw):return struct.pack('<Q',len(raw))+raw


def gguf(entries):
    return b'GGUF'+struct.pack('<IQQ',3,398,len(entries))+b''.join(
        string(key)+struct.pack('<I',kind)+value for key,kind,value in entries)


def fake_meta():
    tokens=[b'[PAD]']*q.VOCABULARY_SIZE
    kinds=[5]*q.VOCABULARY_SIZE
    for name,token,kind in [('im_start',b'<|im_start|>',3),('im_end',b'<|im_end|>',3),
            ('think',b'<think>',4),('end_think',b'</think>',4),('assistant',b'assistant',1),
            ('lf','\u010a'.encode(),1)]:
        tokens[q.MARKER_IDS[name]]=token;kinds[q.MARKER_IDS[name]]=kind
    return {'general.architecture':b'qwen3','tokenizer.ggml.model':b'gpt2','tokenizer.ggml.pre':b'qwen2',
            'tokenizer.ggml.tokens':tokens,'tokenizer.ggml.token_type':kinds}


class RecordTests(unittest.TestCase):
    def test_unprefixed_full_single_user_empty_block(self):
        user=b'original\n';pieces=[envelope(user)]
        result=q._audit_records(capture(pieces),pieces,user)
        self.assertTrue(result['entire_single_user_template_matches'])
        self.assertEqual(result['reasoning_template_state'],'empty_think_block_prefilled')
        self.assertEqual(result['logger_prefix_mode'],'none')

    def test_prefixed_crlf_bare_actual_prefix_fails_reasoning_off(self):
        user=b'original';pieces=[envelope(user,b'')]
        result=q._audit_records(capture(pieces,prefix=True,newline=b'\r\n'),pieces,user)
        self.assertTrue(result['entire_single_user_template_matches'])
        self.assertEqual(result['reasoning_template_state'],'reasoning_off_not_established')
        self.assertEqual(result['logger_prefix_mode'],'timestamp-info')

    def test_good_example_does_not_pass_bad_actual_prefix(self):
        pieces=[envelope(b'x',b'')]
        example=b'llama_completion: chat template example:\n'+envelope(b'example')+b'\n'
        self.assertEqual(q._audit_records(capture(pieces,before=example),pieces,b'x')['reasoning_template_state'],
                         'reasoning_off_not_established')

    def test_open_think_block_fails_off(self):
        pieces=[envelope(b'x',b'<think>\n')]
        self.assertEqual(q._audit_records(capture(pieces),pieces,b'x')['reasoning_template_state'],
                         'reasoning_off_not_established')

    def test_split_markers_utf8_and_json_escapes_preserved(self):
        user='caf\u00e9 \u65e5\u672c\u8a9e \U0001f3b8 \\n \\" \\\\ \n'.encode()
        full=envelope(user);pieces=[bytes([x]) for x in full]
        result=q._audit_records(capture(pieces,prefix=True,newline=b'\r\n'),pieces,user)
        self.assertTrue(result['entire_single_user_template_matches'])
        self.assertEqual(result['reconstructed_sha256'],hashlib.sha256(full).hexdigest())
        self.assertEqual(result['reasoning_template_state'],'empty_think_block_prefilled')

    def test_embedded_quote_newline_and_fake_headers_remain_piece_content(self):
        user=b"'\n     1 -> 'fake'\n0.03.750.131 I      4 -> 'fake'\n"
        pieces=[envelope(user)]
        self.assertTrue(q._audit_records(capture(pieces,prefix=True),pieces,user)['entire_single_user_template_matches'])

    def test_crlf_translation_preserves_original_cr(self):
        user=b'\r\n\n';pieces=[envelope(user)]
        self.assertTrue(q._audit_records(capture(pieces,newline=b'\r\n'),pieces,user)['entire_single_user_template_matches'])

    def test_user_marker_text_cannot_substitute_for_final_prefix(self):
        user=q.ASSISTANT_PREFIX+q.EMPTY_THINK_SUFFIX
        pieces=[envelope(user,b'')]
        self.assertEqual(q._audit_records(capture(pieces),pieces,user)['reasoning_template_state'],
                         'reasoning_off_not_established')

    def test_removed_final_lf_fails_exact_binding(self):
        pieces=[envelope(b'original')]
        self.assertFalse(q._audit_records(capture(pieces),pieces,b'original\n')['entire_single_user_template_matches'])

    def test_unescaped_json_quote_and_newline_fail_exact_binding(self):
        user=b'{"x":"quoted\\\"word\\nnext"}\n'
        changed=user.replace(b'\\"',b'"').replace(b'\\n',b'\n');pieces=[envelope(changed)]
        self.assertFalse(q._audit_records(capture(pieces),pieces,user)['entire_single_user_template_matches'])

    def test_wrong_user_or_extra_system_content_fails_binding(self):
        for full in [envelope(b'wrong'),b'<|im_start|>system\nextra<|im_end|>\n'+envelope(b'x')]:
            with self.subTest(full=full):
                self.assertFalse(q._audit_records(capture([full]),[full],b'x')['entire_single_user_template_matches'])

    def test_extra_suffix_is_not_pass(self):
        pieces=[envelope(b'x')+b'extra']
        self.assertEqual(q._audit_records(capture(pieces),pieces,b'x')['reasoning_template_state'],
                         'unrecognized_assistant_prefix')

    def test_missing_duplicate_and_count_zero_anchors_rejected(self):
        pieces=[b'a'];raw=capture(pieces)
        for changed in [b'',raw+raw,raw.replace(b'prompt = 1',b'prompt = 0')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):q._audit_records(changed,pieces,b'x')

    def test_noncanonical_count_padding_rejected(self):
        pieces=[b'a'];raw=capture(pieces).replace(b'prompt = 1',b'prompt = 01')
        with self.assertRaises(ValueError):q._audit_records(raw,pieces,b'x')

    def test_truncated_extra_and_corrupt_records_rejected(self):
        pieces=[b'a',b'b'];raw=capture(pieces)
        for changed in [raw[:75],raw.replace(b'prompt = 2',b'prompt = 1'),raw.replace(b"'a'",b"'c'"),
                        raw.replace(b'     0 ->',b'0 ->'),raw.replace(b'     0 ->',b'     9 ->')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):q._audit_records(changed,pieces,b'x')

    def test_nul_truncation_rejected(self):
        pieces=[b'a\0b']
        with self.assertRaises(ValueError):q._audit_records(capture(pieces),pieces,b'x')

    def test_missing_final_blank_line_rejected(self):
        pieces=[b'a'];raw=capture(pieces).replace(b"'\n\nother",b"'\nother")
        with self.assertRaises(ValueError):q._audit_records(raw,pieces,b'x')

    def test_nonmonotonic_or_invalid_prefix_rejected(self):
        pieces=[b'a'];raw=capture(pieces,prefix=True)
        for changed in [raw.replace(b'0.03.750.131 I      0',b'0.03.750.130 I      0'),
                        raw.replace(b'0.03.750.131 I      0',b'0.63.750.131 I      0'),
                        raw.replace(b'0.03.750.131 I      0',b'     0')]:
            with self.subTest(raw=changed):
                with self.assertRaises(ValueError):q._audit_records(changed,pieces,b'x')

    def test_token_id_digest_preserves_tokenization_even_with_same_text(self):
        a=q._audit_records(capture([b'ab']),[b'ab'],b'x')
        b=q._audit_records(capture([b'a',b'b']),[b'a',b'b'],b'x')
        self.assertEqual(a['reconstructed_sha256'],b['reconstructed_sha256'])
        self.assertNotEqual(a['token_ids_u32le_sha256'],b['token_ids_u32le_sha256'])


class MetadataTests(unittest.TestCase):
    def test_read_metadata_only_and_restore_position(self):
        raw=gguf([(b'general.architecture',8,string(b'qwen3'))]);stream=io.BytesIO(raw+b'NO_TENSOR_READ')
        stream.seek(4);meta,size,digest=q._metadata(stream)
        self.assertEqual(stream.tell(),4)
        self.assertEqual(meta['general.architecture'],b'qwen3')
        self.assertEqual(size,len(raw));self.assertEqual(digest,hashlib.sha256(raw).hexdigest())

    def test_duplicate_unknown_truncated_and_oversized_metadata_rejected(self):
        raws=[gguf([(b'a',8,string(b'a')),(b'a',8,string(b'b'))]),gguf([(b'a',999,b'')]),
              gguf([(b'a',8,struct.pack('<Q',2**32))]),gguf([(b'a',9,struct.pack('<IQ',9,0))]),
              gguf([(b'a',9,struct.pack('<IQ',999,0))]),gguf([(b'a',9,struct.pack('<IQ',8,2_000_001))]),
              gguf([(b'a',8,string(b'x'))])[:-1],b'GGUF'+struct.pack('<IQQ',3,398,129)]
        for raw in raws:
            with self.subTest(raw=raw):
                stream=io.BytesIO(raw);stream.seek(3)
                with self.assertRaises((ValueError,UnicodeError)):q._metadata(stream)
                self.assertEqual(stream.tell(),3)

    def test_read_error_restores_position(self):
        class Broken(io.BytesIO):
            def read(self,size):
                if self.tell()>=24:raise OSError('fixture read failure')
                return super().read(size)
        stream=Broken(gguf([(b'a',8,string(b'x'))]));stream.seek(2)
        with self.assertRaises(OSError):q._metadata(stream)
        self.assertEqual(stream.tell(),2)

    def test_exact_vocabulary_and_markers(self):
        meta=fake_meta();pieces=q._pieces(meta)
        self.assertEqual(len(pieces),151936)
        self.assertEqual(pieces[151667],b'<think>')
        self.assertEqual(pieces[198],b'\n')
        self.assertEqual(pieces[0],b'')

    def test_wrong_size_encoding_marker_or_kind_rejected(self):
        mutations=[lambda m:m['tokenizer.ggml.tokens'].pop(),lambda m:m.update({'tokenizer.ggml.pre':b'other'}),
                   lambda m:m['tokenizer.ggml.tokens'].__setitem__(151667,b'wrong'),
                   lambda m:m['tokenizer.ggml.token_type'].__setitem__(151667,1),
                   lambda m:m['tokenizer.ggml.token_type'].__setitem__(0,999),
                   lambda m:m.update({'tokenizer.ggml.escape_whitespaces':True})]
        for mutate in mutations:
            meta=fake_meta();mutate(meta)
            with self.assertRaises(ValueError):q._pieces(meta)


class PublicApiTests(unittest.TestCase):
    def test_input_bounds_checked_before_metadata(self):
        for raw,prompt in [(b'',b'x'),(b'x'*(q.MAX_CAPTURE_BYTES+1),b'x'),('text',b'x'),
                           (b'x',b''),(b'x',b'x'*(q.MAX_PROMPT_BYTES+1)),(b'x','text')]:
            with patch.object(q,'_metadata') as read:
                with self.assertRaises(ValueError):q.audit_capture(raw,io.BytesIO(),prompt)
                read.assert_not_called()

    def test_pinned_header_identity_required(self):
        for size,digest in [(q.METADATA_BYTES,'0'*64),(q.METADATA_BYTES-1,q.METADATA_SHA256)]:
            with patch.object(q,'_metadata',return_value=({},size,digest)),patch.object(q,'_pieces') as pieces:
                with self.assertRaises(ValueError):q.audit_capture(b'x',io.BytesIO(),b'x')
                pieces.assert_not_called()

    def test_small_receipt_has_no_per_token_bytes(self):
        user=b'full\n';pieces=[envelope(user)];raw=capture(pieces,prefix=True)
        with patch.object(q,'_metadata',return_value=({},q.METADATA_BYTES,q.METADATA_SHA256)), \
                patch.object(q,'_pieces',return_value=pieces):
            result=q.audit_capture(raw,io.BytesIO(),user)
        self.assertTrue(result['entire_single_user_template_matches'])
        self.assertEqual(result['capture_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['required_model_sha256'],q.REQUIRED_MODEL_SHA256)
        self.assertNotIn('records',result);self.assertNotIn('reconstructed_hex',result)
        self.assertLess(len(json.dumps(result)),2048)

    def test_template_exact_pinned_bytes(self):
        self.assertEqual(len(q.NONTHINK_TEMPLATE),237)
        self.assertNotIn(b'\r',q.NONTHINK_TEMPLATE)
        self.assertEqual(hashlib.sha256(q.NONTHINK_TEMPLATE).hexdigest(),q.NONTHINK_TEMPLATE_SHA256)

    def test_reference_jinja_supports_example_roles_and_exact_user_bytes(self):
        try:from jinja2 import Environment,StrictUndefined
        except ImportError:self.skipTest('Optional existing Jinja2 reference renderer unavailable')
        template=Environment(undefined=StrictUndefined,autoescape=False).from_string(q.NONTHINK_TEMPLATE.decode('ascii'))
        user='caf\u00e9 \u65e5\u672c\u8a9e \\n \\" \\\\ \r\n\n'
        self.assertEqual(template.render(messages=[{'role':'user','content':user}],add_generation_prompt=True).encode(),
                         envelope(user.encode()))
        messages=[{'role':'system','content':'You are a helpful assistant'},{'role':'user','content':'Hello'},
                  {'role':'assistant','content':'Hi there'},{'role':'user','content':'How are you?'}]
        expected=b''.join(b'<|im_start|>'+m['role'].encode()+b'\n'+m['content'].encode()+b'<|im_end|>\n' for m in messages)
        self.assertEqual(template.render(messages=messages,add_generation_prompt=True).encode(),
                         expected+q.ASSISTANT_PREFIX+q.EMPTY_THINK_SUFFIX)
        self.assertEqual(template.render(messages=messages,add_generation_prompt=False).encode(),expected)


if __name__=='__main__':unittest.main()
