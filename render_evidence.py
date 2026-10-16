"""Private, content-addressed electric dry-render evidence; no native execution.

The trusted managed renderer must create the inputs. These checks bind its exact
prototype receipt to commands, dry float and PCM bytes; they are not a fresh bank
attestation, musical judgment or proof of what ran inside a native process.
Publication uses verified copies and Windows no-replace rename. A DB-result
commit is separate; verified unreferenced blobs can be reconciled afterward.
No power-loss durability or portable POSIX file-sharing guarantee is claimed.
"""
import array
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import uuid

from audio.electric_render import ARTICULATION, RENDER_VERSION, validate as validate_arrangement
from audio.emilyguitar import MAX_FRET, OPEN, PINS, PRELOAD, PROFILE_VERSION
from audio.full_decode import _protected_reader

FORMAT = 'electric-render-evidence-v1'
MAX_RAW_BYTES = 31 * 44100 * 8
MAX_RECEIPT_BYTES = 2 * 1024**2
CHUNK = 65536
SHA = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'([0-9a-f]{64})\.(f32|json)')
COMMAND_KEYS = ('id', 'start', 'end', 'pitch', 'velocity', 'player', 'string', 'fret')
LIMIT_TEXT = 'No loop; stored duration is not an audible sustain or exact engine-tail guarantee.'
RECEIPT_KEYS = frozenset('renderer_version profile_version instrument samples frames sample_rate source_origin_sample '
    'peak raw_peak gain_db level_policy level_measurement target_sample_peak_dbfs clipped_samples quantization '
    'source_commands source_commands_sha256 source_gates_pitch_velocity_changed command_transpose_semitones '
    'instances quality_by_instance processing_mode preload_floats_per_channel raw_float bank sample_limits '
    'round_robin_policy random_amplitude dynamics same_string_choke maximum_reattack_damping_seconds '
    'render_tail_seconds effects loops_added original_audio_mixed contours_not_rendered physical_assumptions '
    'native_adapter_acceptance musical_acceptance app_mode_enabled wav_sha256'.split())


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _cancel(cancel):
    if cancel():
        raise InterruptedError('Render evidence cancelled')


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def validate_reference(reference):
    _need(type(reference) is dict and set(reference) == {'format', 'raw', 'receipt'}
          and reference['format'] == FORMAT, 'Invalid render evidence reference')
    for key, maximum in (('raw', MAX_RAW_BYTES), ('receipt', MAX_RECEIPT_BYTES)):
        item = reference[key]
        _need(type(item) is dict and set(item) == {'sha256', 'bytes'}
              and _sha(item['sha256']) and type(item['bytes']) is int
              and 0 < item['bytes'] <= maximum, 'Invalid render evidence member')
    _need(reference['raw']['bytes'] % 8 == 0, 'Invalid stereo float geometry')
    return reference


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
              'Linked render evidence path')
        if item == path:
            _need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
                  'Nonregular render evidence path')
            _need(directory or info.st_nlink == 1, 'Hard-linked render evidence file')
    return path


def _held(stack, path, maximum):
    path = _plain(path)
    before = path.stat()
    _need(0 < before.st_size <= maximum, 'Render evidence file exceeds bound')
    stream = stack.enter_context(_protected_reader(path))
    actual = os.fstat(stream.fileno())
    _need((actual.st_dev, actual.st_ino, actual.st_size, actual.st_nlink) ==
          (before.st_dev, before.st_ino, before.st_size, 1), 'Render evidence identity changed')
    return stream


def _pairs(items):
    result = {}
    for key, value in items:
        _need(key not in result, 'Repeated render receipt JSON key')
        result[key] = value
    return result


def _json(raw):
    _need(0 < len(raw) <= MAX_RECEIPT_BYTES, 'Render receipt exceeds bound')
    def invalid(value):
        raise ValueError('Nonfinite render receipt JSON')
    try:
        return json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs, parse_constant=invalid)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid render receipt JSON') from error


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _commands(arrangement):
    rate, origin, strings = validate_arrangement(arrangement)
    commands = [{key: note[key] for key in COMMAND_KEYS} for note in arrangement['notes']]
    return rate, origin, strings, commands


def _receipt(receipt, arrangement, raw_name=None):
    _need(type(receipt) is dict and set(receipt) == RECEIPT_KEYS, 'Expected complete unchanged prototype receipt')
    rate, origin, strings, commands = _commands(arrangement)
    # Validate the supplied receipt commands independently: Python bool == int
    # equality must never hide a malformed command behind the comparison below.
    _need(type(receipt.get('source_commands')) is list, 'Missing source commands')
    receipt_notes = [dict(note, articulation=ARTICULATION) if type(note) is dict else note
                     for note in receipt['source_commands']]
    validate_arrangement({'sample_rate': receipt.get('sample_rate'),
                          'render_origin_sample': receipt.get('source_origin_sample'),
                          'performers': arrangement['performers'], 'notes': receipt_notes})
    _need(all(set(note) == set(COMMAND_KEYS) for note in receipt['source_commands']), 'Unexpected command metadata')
    frames = max(note['end'] for note in commands) - origin + rate
    _need(receipt.get('renderer_version') == RENDER_VERSION
          and receipt.get('profile_version') == PROFILE_VERSION
          and type(receipt.get('frames')) is int and receipt['frames'] == frames
          and type(receipt.get('sample_rate')) is int and receipt['sample_rate'] == rate
          and type(receipt.get('source_origin_sample')) is int and receipt['source_origin_sample'] == origin
          and receipt['source_commands'] == commands
          and receipt.get('source_commands_sha256') == hashlib.sha256(_canonical(commands)).hexdigest(),
          'Render receipt version, clock or commands differ')
    for key in ('source_gates_pitch_velocity_changed', 'loops_added', 'original_audio_mixed',
                'native_adapter_acceptance', 'musical_acceptance', 'app_mode_enabled'):
        _need(receipt.get(key) is False, 'Prototype receipt scope changed: ' + key)
    for key, expected in (('command_transpose_semitones', 0), ('random_amplitude', 0),
                          ('instances', len(strings)), ('preload_floats_per_channel', PRELOAD),
                          ('render_tail_seconds', 1), ('clipped_samples', 0)):
        _need(type(receipt.get(key)) is int and receipt[key] == expected, 'Invalid render receipt setting: ' + key)
    _need(receipt.get('effects') == [] and type(receipt.get('effects')) is list
          and receipt.get('instrument') == 'Epiphone Emily the Strange direct electric samples'
          and receipt.get('samples') == 'Karoryfer Emilyguitar v1.001'
          and receipt.get('processing_mode') == 'freewheeling'
          and receipt.get('round_robin_policy') == 'fixed first recorded variation only'
          and receipt.get('dynamics') == 'Author velocity layers/curves retained; uncalibrated, measured energy inversions remain.'
          and receipt.get('same_string_choke') == 'At most5ms half-cosine outgoing output fade followed by exact-onset all_sound_off'
          and receipt.get('maximum_reattack_damping_seconds') == .005
          and receipt.get('quantization') == 'PCM16 nearest, no dither'
          and receipt.get('level_measurement') == 'sample_peak_not_lufs_or_true_peak',
          'Unsupported electric render processing')
    quality = receipt.get('quality_by_instance')
    _need(type(quality) is list and len(quality) == len(strings)
          and all(type(q) is dict and set(q) == {'sample', 'oscillator'}
                  and type(q['sample']) is int and q['sample'] == 2
                  and type(q['oscillator']) is int and q['oscillator'] == 1 for q in quality),
          'Electric engine quality differs')
    _need(receipt.get('contours_not_rendered') == [note['id'] for note in arrangement['notes'] if note.get('contour')],
          'Unrendered contour ledger differs')
    physical = receipt.get('physical_assumptions')
    _need(type(physical) is dict and set(physical) == {'open_midi', 'max_fret', 'source_string_identity_known', 'fingering_biomechanics_verified'}
          and type(physical.get('open_midi')) is list and physical['open_midi'] == list(OPEN)
          and all(type(pitch) is int for pitch in physical['open_midi'])
          and type(physical.get('max_fret')) is int and physical['max_fret'] == MAX_FRET
          and physical.get('source_string_identity_known') is False and physical.get('fingering_biomechanics_verified') is False,
          'Electric physical scope differs')
    bank = receipt.get('bank')
    _need(type(bank) is dict and set(bank) == {'profile_version', 'manifest_sha256', 'mapping_sha256',
          'bank_files_verified', 'source_wav_git_identities_verified', 'wav_headers_verified', 'integrity_checked',
          'selected_attack_regions', 'selected_release_regions', 'native', 'mode_enabled', 'scope'}
          and bank.get('profile_version') == PROFILE_VERSION
          and bank.get('manifest_sha256') == PINS and bank.get('integrity_checked') is True
          and bank.get('mode_enabled') is False and _sha(bank.get('mapping_sha256')),
          'Electric bank receipt differs')
    for key, expected in (('bank_files_verified', 331), ('source_wav_git_identities_verified', 323),
                          ('wav_headers_verified', 323), ('selected_attack_regions', 64), ('selected_release_regions', 16)):
        _need(type(bank.get(key)) is int and bank[key] == expected, 'Electric bank count differs')
    _need(type(bank['native']) is list and len(bank['native']) == 4
          and bank['scope'] == 'Pinned bank and observed static engine closure; no sound-quality or OS attestation.',
          'Missing observed static engine receipt')
    for native in bank['native']:
        _need(type(native) is dict and set(native) == {'path', 'bytes', 'sha256', 'held_read_share_only'}
              and type(native['path']) is str and 0 < len(native['path']) <= 4000
              and type(native['bytes']) is int and native['bytes'] > 0 and _sha(native['sha256'])
              and type(native['held_read_share_only']) is bool, 'Invalid static engine receipt')
    limits = receipt.get('sample_limits')
    _need(type(limits) is list and len(limits) == len(commands), 'Missing sample-duration warnings')
    for note, limit in zip(commands, limits):
        keys = {'note_id', 'region_id', 'sample', 'stored_frames', 'sample_root_midi',
                'transposed_stored_frames_estimate', 'gate_exceeds_stored_sample_estimate', 'limit'}
        _need(type(limit) is dict and set(limit) == keys and limit['note_id'] == note['id']
              and type(limit['region_id']) is str and 0 < len(limit['region_id']) <= 200
              and type(limit['sample']) is str and 0 < len(limit['sample']) <= 300,
              'Invalid sample-duration warning identity')
        sample = PurePosixPath(limit['sample'])
        _need(not sample.is_absolute() and '\\' not in limit['sample'] and ':' not in limit['sample']
              and all(part not in ('', '.', '..') for part in limit['sample'].split('/')),
              'Invalid sample-duration warning path')
        _need(type(limit['stored_frames']) is int and 0 < limit['stored_frames'] <= PRELOAD
              and type(limit['sample_root_midi']) is int and 0 <= limit['sample_root_midi'] <= 127,
              'Invalid stored sample geometry')
        estimate = limit['stored_frames'] / 2**((note['pitch']-limit['sample_root_midi'])/12)
        _need(_number(limit['transposed_stored_frames_estimate'])
              and math.isclose(limit['transposed_stored_frames_estimate'], estimate, rel_tol=1e-12, abs_tol=1e-12)
              and limit['gate_exceeds_stored_sample_estimate'] is (note['end']-note['start'] > estimate)
              and limit['limit'] == LIMIT_TEXT, 'Sample-duration warning differs')
    raw = receipt.get('raw_float')
    _need(type(raw) is dict and set(raw) == {'file', 'bytes', 'sha256', 'sample_peak', 'rms', 'format', 'stage'}
          and type(raw['bytes']) is int and raw['bytes'] == frames*8 <= MAX_RAW_BYTES
          and _sha(raw['sha256']) and type(raw['file']) is str
          and raw['format'] == 'stereo little-endian float32 interleaved'
          and raw['stage'] == 'dry adapter output after declared string damping, before constant gain'
          and all(_number(raw[key]) and raw[key] >= 0 for key in ('sample_peak', 'rms')),
          'Invalid dry-float receipt')
    try:
        identity = raw['file'][:-len('.wav.dry.f32')]
        _need(str(uuid.UUID(identity)) == identity and raw['file'] == identity+'.wav.dry.f32', 'Invalid raw input filename')
    except (ValueError, AttributeError) as error:
        raise ValueError('Invalid raw input filename') from error
    _need(raw_name is None or raw['file'] == raw_name, 'Raw input filename differs')
    _need(_sha(receipt.get('wav_sha256')), 'Missing WAV identity')
    return frames


def _floats(stream, cancel):
    stream.seek(0)
    while raw := stream.read(CHUNK):
        _cancel(cancel)
        _need(len(raw) % 8 == 0, 'Truncated stereo float data')
        values = array.array('f'); values.frombytes(raw)
        if sys.byteorder != 'little':
            values.byteswap()
        _need(all(math.isfinite(value) for value in values), 'Nonfinite dry-float data')
        yield raw, values


def _verify_streams(raw, receipt, arrangement, *, wav_sha256, wav=None, cancel=lambda: False, raw_name=None):
    frames = _receipt(receipt, arrangement, raw_name)
    _need(wav_sha256 == receipt['wav_sha256'], 'WAV reference differs from dry receipt')
    _need(os.fstat(raw.fileno()).st_size == frames*8, 'Dry-float byte count differs')
    digest, peak, energy = hashlib.sha256(), 0., 0.
    for data, values in _floats(raw, cancel):
        digest.update(data)
        peak = max(peak, max(map(abs, values)))
        energy += math.fsum(value*value for value in values)
    rms = math.sqrt(energy/(frames*2))
    _need(digest.hexdigest() == receipt['raw_float']['sha256'] and peak > 1e-8
          and peak == receipt['raw_float']['sample_peak'] and _number(receipt.get('raw_peak')) and receipt['raw_peak'] == peak
          and math.isclose(rms, receipt['raw_float']['rms'], rel_tol=1e-12, abs_tol=1e-15),
          'Dry-float hash, peak or RMS differs')
    policy = receipt.get('level_policy')
    _need(policy in ('peak', 'legacy_fixed'), 'Unknown constant-gain policy')
    gain = 10**(-3/20)/peak if policy == 'peak' else .25
    _need(_number(receipt.get('gain_db')) and math.isclose(receipt['gain_db'], 20*math.log10(gain), abs_tol=1e-12)
          and receipt.get('target_sample_peak_dbfs') == (-3 if policy == 'peak' else None), 'Constant-gain receipt differs')
    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36+frames*4, b'WAVE', b'fmt ', 16,
                         1, 2, 44100, 44100*4, 4, 16, b'data', frames*4)
    wav_hash, pcm_peak = hashlib.sha256(header), 0
    if wav is not None:
        wav.seek(0)
        _need(os.fstat(wav.fileno()).st_size == len(header)+frames*4 and wav.read(len(header)) == header,
              'WAV PCM16 geometry differs')
    for _, values in _floats(raw, cancel):
        pcm = array.array('h')
        for value in values:
            scaled = value*gain
            _need(math.isfinite(scaled) and abs(scaled) < 1, 'PCM clipping/headroom violation')
            sample = round(scaled*32768)
            _need(-32768 < sample < 32767, 'PCM reaches full scale')
            pcm_peak = max(pcm_peak, abs(sample)); pcm.append(sample)
        if sys.byteorder != 'little':
            pcm.byteswap()
        encoded = pcm.tobytes(); wav_hash.update(encoded)
        _need(wav is None or wav.read(len(encoded)) == encoded, 'WAV differs from dry constant-gain quantization')
    _need(wav_hash.hexdigest() == wav_sha256 and _number(receipt.get('peak')) and receipt['peak'] == pcm_peak/32768,
          'WAV hash or PCM peak differs')
    _cancel(cancel)


class RenderEvidence:
    def __init__(self, library):
        self.library = library
        self.root = library.root/'render-evidence'

    def _paths(self, reference):
        validate_reference(reference)
        _plain(self.root, directory=True)
        return self.root/(reference['raw']['sha256']+'.f32'), self.root/(reference['receipt']['sha256']+'.json')

    def verify(self, reference, *, wav_sha256, arrangement, cancel=lambda: False):
        """Recompute privately; caller separately verifies its music asset bytes."""
        _cancel(cancel)
        raw_path, receipt_path = self._paths(reference)
        with ExitStack() as stack:
            raw = _held(stack, raw_path, MAX_RAW_BYTES)
            metadata = _held(stack, receipt_path, MAX_RECEIPT_BYTES).read(MAX_RECEIPT_BYTES+1)
            _need(len(metadata) == reference['receipt']['bytes']
                  and hashlib.sha256(metadata).hexdigest() == reference['receipt']['sha256']
                  and os.fstat(raw.fileno()).st_size == reference['raw']['bytes'], 'Saved render evidence identity differs')
            receipt = _json(metadata)
            _need(type(receipt) is dict and type(receipt.get('raw_float')) is dict
                  and receipt['raw_float'].get('sha256') == reference['raw']['sha256'], 'Saved raw identity differs')
            _verify_streams(raw, receipt, arrangement, wav_sha256=wav_sha256, cancel=cancel)
        return {'raw_path': raw_path, 'receipt_path': receipt_path, 'receipt': receipt}

    def publish(self, raw_path, receipt_path, *, wav_path, arrangement, cancel=lambda: False):
        """Copy only known active-job staging siblings; never consume/alter inputs.

        Caller owns worker.lock and must retain it through the later job/media
        commit. A crash between two blob renames leaves collectible evidence,
        never a completed job. Cancel/error may also leave verified CAS orphans.
        """
        _need(os.name == 'nt', 'Render evidence publication requires Windows no-replace rename')
        _cancel(cancel)
        stage = _plain(self.library.stage, directory=True)
        wav_path = Path(wav_path).absolute()
        identity = wav_path.name[:-4]
        try:
            _need(str(uuid.UUID(identity)) == identity and wav_path.name == identity+'.wav', 'Invalid render job output name')
        except (ValueError, AttributeError) as error:
            raise ValueError('Invalid render job output name') from error
        _need(wav_path.parent == stage and Path(raw_path).absolute() == stage/(identity+'.wav.dry.f32')
              and Path(receipt_path).absolute() == stage/(identity+'.wav.receipt.json'), 'Render inputs escape known job staging')
        _need(self.library.job(identity)['state'] in ('queued', 'running'), 'Render job is not active')
        pending = []
        try:
            with ExitStack() as stack:
                raw = _held(stack, raw_path, MAX_RAW_BYTES)
                metadata = _held(stack, receipt_path, MAX_RECEIPT_BYTES).read(MAX_RECEIPT_BYTES+1)
                wav = _held(stack, wav_path, 44+MAX_RAW_BYTES//2)
                receipt = _json(metadata)
                _need(type(receipt) is dict, 'Invalid render receipt JSON object')
                _verify_streams(raw, receipt, arrangement, wav_sha256=receipt.get('wav_sha256'), wav=wav,
                                cancel=cancel, raw_name=Path(raw_path).name)
                reference = {'format': FORMAT, 'raw': {'sha256': receipt['raw_float']['sha256'], 'bytes': os.fstat(raw.fileno()).st_size},
                             'receipt': {'sha256': hashlib.sha256(metadata).hexdigest(), 'bytes': len(metadata)}}
                validate_reference(reference)
                self.library.space(reference['raw']['bytes']+reference['receipt']['bytes'])
                _plain(self.library.root, directory=True)
                self.root.mkdir(exist_ok=True)
                targets = self._paths(reference)
                # Refuse corrupt/linked occupied names before copying anything.
                for target, member in zip(targets, (reference['raw'], reference['receipt'])):
                    if target.exists() or target.is_symlink():
                        with ExitStack() as occupied:
                            source = _held(occupied, target, member['bytes'])
                            _need(os.fstat(source.fileno()).st_size == member['bytes']
                                  and hashlib.file_digest(source, 'sha256').hexdigest() == member['sha256'],
                                  'Occupied evidence failed verification; preserved')
                for target, member, source in zip(targets, (reference['raw'], reference['receipt']), (raw, None)):
                    _cancel(cancel)
                    if target.exists():
                        continue
                    temporary = self.root/('.publish-'+str(uuid.uuid4())+'-'+member['sha256']+'.partial')
                    hasher = hashlib.sha256()
                    with temporary.open('xb') as output:
                        owned = os.fstat(output.fileno())
                        pending.append((temporary, owned.st_dev, owned.st_ino))
                        if source is None:
                            output.write(metadata); hasher.update(metadata)
                        else:
                            source.seek(0)
                            while data := source.read(CHUNK):
                                _cancel(cancel); output.write(data); hasher.update(data)
                        output.flush(); os.fsync(output.fileno())
                    _need(temporary.stat().st_size == member['bytes'] and hasher.hexdigest() == member['sha256'], 'Evidence copy changed')
                    _cancel(cancel)
                    try:
                        os.rename(temporary, target)  # Windows refuses an occupied destination.
                    except FileExistsError:
                        with ExitStack() as occupied:
                            stored = _held(occupied, target, member['bytes'])
                            _need(os.fstat(stored.fileno()).st_size == member['bytes']
                                  and hashlib.file_digest(stored, 'sha256').hexdigest() == member['sha256'],
                                  'Concurrent occupied evidence differs')
                self.verify(reference, wav_sha256=receipt['wav_sha256'], arrangement=arrangement, cancel=cancel)
                return reference
        finally:
            # These paths were exclusively created by this call, never a broad
            # directory cleanup. Crash leftovers are preserved for diagnosis.
            for temporary, device, inode in pending:
                try:
                    if temporary.exists():
                        _plain(temporary)
                        owned = temporary.stat()
                        if (owned.st_dev, owned.st_ino) == (device, inode):
                            temporary.unlink()
                except (OSError, ValueError):
                    pass  # A replaced/linked path is no longer ours to remove.


def _recorded_context(value):
    """Command-only context for integrity checks, never inferred source notes."""
    _need(type(value) is dict and type(value.get('source_commands')) is list
          and 0 < len(value['source_commands']) <= 4000
          and all(type(note) is dict and type(note.get('player')) is int for note in value['source_commands'])
          and type(value.get('contours_not_rendered')) is list
          and all(type(identity) is str for identity in value['contours_not_rendered']), 'Unknown receipt content')
    return {'sample_rate': value.get('sample_rate'),
            'render_origin_sample': value.get('source_origin_sample'),
            'performers': max(note['player'] for note in value['source_commands']),
            'notes': [{**note, 'articulation': ARTICULATION,
                       'contour': note.get('id') in value['contours_not_rendered']}
                      for note in value['source_commands']]}


def reconcile_render_evidence(library, cancel=lambda: False):
    """Caller MUST already own worker.lock with prior workers stopped/reaped.

    No internal lock acquisition: startup/reaping already owns the same lock.
    Only bounded, regular, hash-valid namespace files can be collected. Unknown
    names, links, corrupted files and crash pending files remain for diagnosis.
    Any malformed job reference aborts collection before a file is deleted.
    """
    store = RenderEvidence(library)
    if not store.root.exists():
        return {'files': 0, 'bytes': 0}
    _plain(store.root, directory=True)
    referenced = set()
    with library.connect() as db:
        for row in db.execute('SELECT state,request,result FROM jobs WHERE result IS NOT NULL'):
            _cancel(cancel)
            value = json.loads(row['result'])
            _need(type(value) is dict, 'Malformed saved job result; preserve render evidence')
            request=json.loads(row['request'])
            tone=None
            if (any(key in request for key in ('electric_tone','electric_tone_version','electric_tone_window_version'))
                    or any(key in value for key in ('tone_evidence','effects','effects_failure','dry_input'))):
                from tone_evidence import verify_preview,verify_failed_preview
                from audio.preview_instruments import validate_saved_selection
                selection=validate_saved_selection(request)
                _need('electric_tone' in selection,'Unselected tone chain; preserve evidence')
                if 'asset' in value:
                    tone=verify_preview(library,value,selection,cancel=cancel)
                elif 'render_evidence' in value or 'effects_failure' in value:
                    verify_failed_preview(library,value,selection,row['state'],cancel=cancel)
                else:
                    # A planner/analysis failure may occur before dry publication.
                    _need(not any(key in value for key in ('tone_evidence','effects','dry_input')),
                          'Incomplete tone chain; preserve evidence')
            if 'render_evidence' in value:
                reference = validate_reference(value['render_evidence'])
                # A damaged reference can declare raw A but point at a receipt
                # that actually binds raw C. Preserve every file if any saved
                # pair is unverifiable, before treating C as unreferenced.
                _, receipt_path = store._paths(reference)
                with ExitStack() as stack:
                    metadata = _held(stack, receipt_path, MAX_RECEIPT_BYTES).read(MAX_RECEIPT_BYTES+1)
                    _need(len(metadata) == reference['receipt']['bytes']
                          and hashlib.sha256(metadata).hexdigest() == reference['receipt']['sha256'], 'Saved receipt reference changed')
                    receipt = _json(metadata)
                arrangement = value['arrangement'] if 'arrangement' in value else _recorded_context(receipt)
                if 'asset' in value:
                    _need(type(value['asset']) is dict and _sha(value['asset'].get('sha256')), 'Invalid saved WAV reference')
                    wav_sha256 = value['asset']['sha256']
                    if tone is not None:
                        wav_sha256=tone['binding']['input']['sha256']
                else:
                    _need(type(receipt) is dict, 'Invalid saved render receipt')
                    wav_sha256 = receipt.get('wav_sha256')
                store.verify(reference, wav_sha256=wav_sha256, arrangement=arrangement, cancel=cancel)
                referenced.update((reference['raw']['sha256']+'.f32', reference['receipt']['sha256']+'.json'))
    candidates = []
    for path in store.root.iterdir():
        _cancel(cancel)
        match = NAME.fullmatch(path.name)
        if match is None or path.name in referenced:
            continue
        maximum = MAX_RAW_BYTES if match[2] == 'f32' else MAX_RECEIPT_BYTES
        try:
            with ExitStack() as stack:
                stream = _held(stack, path, maximum)
                if match[2] == 'f32':
                    hasher = hashlib.sha256()
                    for raw, _ in _floats(stream, cancel): hasher.update(raw)
                else:
                    raw = stream.read(MAX_RECEIPT_BYTES+1)
                    value = _json(raw)
                    # Reconstruct only the recorded command context needed to
                    # validate an orphan, never a source Performance or inferred
                    # contour. A truthy contour flag checks the existing ledger.
                    context = _recorded_context(value)
                    _receipt(value, context)
                    partner = store.root/(value['raw_float']['sha256']+'.f32')
                    partner_stream = _held(stack, partner, MAX_RAW_BYTES)
                    _verify_streams(partner_stream, value, context, wav_sha256=value['wav_sha256'], cancel=cancel)
                    hasher = hashlib.sha256(raw)
                if hasher.hexdigest() == match[1]:
                    candidates.append((path, path.stat().st_size, path.stat().st_ino))
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError):
            continue
    removed, total = 0, 0
    for path, size, inode in candidates:
        _cancel(cancel)
        # Revalidate immediately before unlink after held readers close. The
        # caller-held worker lock fences cooperating publishers/job commits.
        try:
            _plain(path)
            with _protected_reader(path) as stream:
                _need(path.stat().st_size == size and path.stat().st_ino == inode
                      and hashlib.file_digest(stream, 'sha256').hexdigest() == path.name.split('.')[0], 'Collection candidate changed')
            path.unlink(); removed += 1; total += size
        except (OSError, ValueError):
            continue
    return {'files': removed, 'bytes': total}
