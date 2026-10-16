"""Bounded byte-preserving audit of the pinned Qwen3 verbose prompt diagnostics.

The caller MUST hold local_arranger_gpu_runtime.protect_runtime (or the CPU
equivalent) across this call, binding its full SHA-verified GGUF to the supplied
protected read stream. This header-only audit does not verify tensor bytes and
does not initialize a tokenizer, model, native library or GPU context.
"""
import hashlib
import re
import struct

VERSION = 'pinned-qwen3-verbose-prompt-audit-v1'
REQUIRED_MODEL_SHA256 = '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
METADATA_SHA256 = 'f60c4ad45829e67964683dfacc939c9adccf640f62db14e6de7cb63be7b7b23a'
METADATA_BYTES = 5_932_859
VOCABULARY_SIZE = 151_936
MAX_HEADER_BYTES = 8 * 1024**2
MAX_CAPTURE_BYTES = 1024**2
MAX_PROMPT_BYTES = 32_768
MAX_TOKENS = 4096
MARKER_IDS = {'im_start':151644, 'im_end':151645, 'think':151667,
              'end_think':151668, 'assistant':77091, 'lf':198}
ASSISTANT_PREFIX = b'<|im_start|>assistant\n'
EMPTY_THINK_SUFFIX = b'<think>\n\n</think>\n\n'
NONTHINK_TEMPLATE = br"""{%- for message in messages -%}
{{- '<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>\n' -}}
{%- endfor -%}
{%- if add_generation_prompt -%}
{{- '<|im_start|>assistant\n<think>\n\n</think>\n\n' -}}
{%- endif -%}
"""
NONTHINK_TEMPLATE_SHA256 = '78e9c797d8a0125945c3f81383f92002b2ed6576a7e48ebb35dee185cf7dd589'
_FORMATS = {0:'B', 1:'b', 2:'H', 3:'h', 4:'I', 5:'i', 6:'f', 7:'?',
            10:'Q', 11:'q', 12:'d'}
_PREFIX = rb'(?:0|[1-9][0-9]{0,7})\.[0-5][0-9]\.[0-9]{3}\.[0-9]{3} I '
_ANCHOR = re.compile(rb'(?m)^(?P<prefix>'+_PREFIX+rb')?llama_completion: number of tokens in prompt = (?P<count>[0-9]{1,5})(?P<nl>\r?\n)')
_TOKEN_HEADER = re.compile(rb' *([0-9]{1,6}) -> \x27')
_LOG_PREFIX = re.compile(_PREFIX)


def _metadata(stream):
    """Read GGUF metadata only, with bounded counts and no tensor traversal."""
    digest = hashlib.sha256()
    start = stream.tell()
    stream.seek(0)
    def read(size):
        if size < 0 or stream.tell()+size > MAX_HEADER_BYTES:
            raise ValueError('GGUF metadata byte bound exceeded')
        raw = stream.read(size)
        if len(raw) != size: raise ValueError('Truncated GGUF metadata')
        digest.update(raw)
        return raw
    def scalar(fmt):
        return struct.unpack('<'+fmt, read(struct.calcsize(fmt)))[0]
    def string():
        size = scalar('Q')
        if size > 1024**2: raise ValueError('GGUF metadata string bound exceeded')
        return read(size)
    def value(kind, keep):
        if kind == 8:
            raw = string()
            return raw if keep else None
        if kind == 9:
            subtype, count = scalar('I'), scalar('Q')
            if (count > (VOCABULARY_SIZE if keep else 2_000_000)
                    or subtype not in {*_FORMATS,8}):
                raise ValueError('Unsupported GGUF metadata array')
            if keep: return [value(subtype, True) for _ in range(count)]
            for _ in range(count): value(subtype, False)
            return None
        if kind not in _FORMATS: raise ValueError('Unknown GGUF metadata type')
        result = scalar(_FORMATS[kind])
        return result if keep else None
    try:
        if read(4) != b'GGUF' or scalar('I') != 3: raise ValueError('Expected GGUF v3')
        tensors, count = scalar('Q'), scalar('Q')
        if tensors != 398 or count > 128: raise ValueError('Unexpected pinned GGUF counts')
        result = {}
        for _ in range(count):
            key = string().decode('utf-8', 'strict')
            if key in result: raise ValueError('Duplicate GGUF metadata key')
            kind = scalar('I')
            keep = kind != 9 or key in {'tokenizer.ggml.tokens','tokenizer.ggml.token_type'}
            result[key] = value(kind, keep)
        return result, stream.tell(), digest.hexdigest()
    finally:
        stream.seek(start)


def _pieces(meta):
    if (meta.get('general.architecture') != b'qwen3'
            or meta.get('tokenizer.ggml.model') != b'gpt2'
            or meta.get('tokenizer.ggml.pre') != b'qwen2'
            or meta.get('tokenizer.ggml.escape_whitespaces',False)):
        raise ValueError('Unexpected pinned Qwen vocabulary encoding')
    tokens, kinds = meta.get('tokenizer.ggml.tokens'), meta.get('tokenizer.ggml.token_type')
    if not isinstance(tokens,list) or not isinstance(kinds,list) or len(tokens)!=VOCABULARY_SIZE or len(kinds)!=VOCABULARY_SIZE:
        raise ValueError('Expected exact pinned Qwen vocabulary size')
    direct = list(range(0x21,0x7f))+list(range(0xa1,0xad))+list(range(0xae,0x100))
    inverse = {chr(x):x for x in direct}
    for index, byte in enumerate(x for x in range(256) if x not in direct):
        inverse[chr(256+index)] = byte
    pieces = []
    for token, kind in zip(tokens,kinds):
        if not isinstance(token,bytes): raise ValueError('Invalid GGUF token string')
        if kind in (2,3,4): piece = token
        elif kind == 1:
            try: piece = bytes(inverse[c] for c in token.decode('utf-8','strict'))
            except (KeyError,UnicodeError) as exc: raise ValueError('Invalid pinned BPE byte encoding') from exc
        elif kind == 5: piece = b''
        else: raise ValueError('Unsupported pinned vocabulary token type')
        pieces.append(piece)
    for name, expected, kind in [('im_start',b'<|im_start|>',3),('im_end',b'<|im_end|>',3),
            ('think',b'<think>',4),('end_think',b'</think>',4),('assistant',b'assistant',1),('lf',b'\n',1)]:
        token = MARKER_IDS[name]
        if pieces[token]!=expected or kinds[token]!=kind:
            raise ValueError('Pinned Qwen marker ID/type mismatch')
    return pieces


def _timestamp(raw):
    # _PREFIX already validates the exact canonical numeric layout.
    minute, second, ms, us = map(int,raw[:-3].split(b'.'))
    return ((minute*60+second)*1000+ms)*1000+us


def _audit_records(raw, pieces, exact_prompt):
    """Frame each record by its pinned piece length, including raw LF/quotes."""
    anchors = list(_ANCHOR.finditer(raw))
    if len(anchors)!=1: raise ValueError('Missing or ambiguous exact actual prompt-count anchor')
    anchor = anchors[0]
    count, newline = int(anchor['count']), anchor['nl']
    if not 1<=count<=MAX_TOKENS or str(count).encode()!=anchor['count']:
        raise ValueError('Unexpected actual prompt token count')
    prefixed = anchor['prefix'] is not None
    previous = _timestamp(anchor['prefix']) if prefixed else None
    cursor = anchor.end()
    joined, token_digest = bytearray(), hashlib.sha256()
    for index in range(count):
        if prefixed:
            prefix = _LOG_PREFIX.match(raw,cursor)
            if not prefix: raise ValueError('Missing exact per-token logger prefix')
            now = _timestamp(prefix[0])
            if now<previous: raise ValueError('Nonmonotonic native logger timestamp')
            previous, cursor = now, prefix.end()
        head = _TOKEN_HEADER.match(raw,cursor)
        if not head: raise ValueError('Missing exact actual token record')
        token = int(head[1])
        if token>=len(pieces): raise ValueError('Actual token ID outside pinned vocabulary')
        piece = pieces[token]
        if b'\0' in piece: raise ValueError('NUL-truncated native token diagnostic')
        printed = piece.replace(b'\n',b'\r\n') if newline==b'\r\n' else piece
        record = f'{token:6d} -> \x27'.encode('ascii')+printed+b"'"+newline
        if raw[cursor:cursor+len(record)]!=record:
            raise ValueError('Actual token diagnostic differs from pinned vocabulary bytes')
        cursor += len(record)
        joined.extend(piece)
        if len(joined)>MAX_CAPTURE_BYTES: raise ValueError('Reconstructed prompt exceeds bound')
        token_digest.update(struct.pack('<I',token))
    if prefixed:
        blank = _LOG_PREFIX.match(raw,cursor)
        if not blank or _timestamp(blank[0])<previous:
            raise ValueError('Missing exact final verbose-block logger prefix')
        cursor = blank.end()
    if raw[cursor:cursor+len(newline)]!=newline:
        raise ValueError('Missing exact end of actual token diagnostics')
    joined = bytes(joined)
    suffix = joined.rsplit(ASSISTANT_PREFIX,1)[-1] if ASSISTANT_PREFIX in joined else None
    if suffix==EMPTY_THINK_SUFFIX: state = 'empty_think_block_prefilled'
    elif suffix in (b'',b'<think>\n'): state = 'reasoning_off_not_established'
    else: state = 'unrecognized_assistant_prefix'
    expected_start = b'<|im_start|>user\n'+exact_prompt+b'<|im_end|>\n'+ASSISTANT_PREFIX
    matches = suffix is not None and joined==expected_start+suffix
    return {'version':VERSION,'count':count,'reasoning_template_state':state,
            'entire_single_user_template_matches':matches,
            'capture_bytes':len(raw),'capture_sha256':hashlib.sha256(raw).hexdigest(),
            'exact_prompt_bytes':len(exact_prompt),'exact_prompt_sha256':hashlib.sha256(exact_prompt).hexdigest(),
            'reconstructed_bytes':len(joined),'reconstructed_sha256':hashlib.sha256(joined).hexdigest(),
            'token_ids_u32le_sha256':token_digest.hexdigest(),
            'logger_prefix_mode':'timestamp-info' if prefixed else 'none',
            'record_newline_hex':newline.hex(),
            'assistant_suffix_bytes':None if suffix is None else len(suffix),
            'assistant_suffix_first64_hex':None if suffix is None else suffix[:64].hex(),
            'assistant_suffix_sha256':None if suffix is None else hashlib.sha256(suffix).hexdigest(),
            'scope':'Initial tokenized prompt only; no claim about generated reasoning, prefill performance or musical quality.'}


def audit_capture(raw_capture, protected_model_stream, exact_prompt_bytes):
    """Return a small audit receipt; require an externally full-SHA guarded model.

    raw_capture must be the untouched bounded stderr bytes. The caller must keep
    the verified runtime/model guard and its owned protected read stream alive.
    This function verifies the exact metadata SHA, size, vocabulary and marker
    IDs, preserving the stream position. Both returned reasoning/template fields
    must pass, and the caller must separately enforce its token budget. A valid
    receipt with a bare prefix is an observed compatibility failure, not success.
    """
    if type(raw_capture) is not bytes or not 0<len(raw_capture)<=MAX_CAPTURE_BYTES:
        raise ValueError('Expected bounded raw native stderr bytes')
    if type(exact_prompt_bytes) is not bytes or not 0<len(exact_prompt_bytes)<=MAX_PROMPT_BYTES:
        raise ValueError('Expected bounded exact frozen prompt bytes')
    meta, size, digest = _metadata(protected_model_stream)
    if size!=METADATA_BYTES or digest!=METADATA_SHA256:
        raise ValueError('Pinned GGUF metadata identity differs')
    receipt = _audit_records(raw_capture,_pieces(meta),exact_prompt_bytes)
    receipt.update(metadata_bytes=size,metadata_sha256=digest,
                   required_model_sha256=REQUIRED_MODEL_SHA256,
                   whole_model_verification='Required external protected runtime guard; tensor bytes are not rehashed here.')
    return receipt
