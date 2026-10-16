"""Authored arrays/archives and tiny temporary Libraries; no retained music."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile
import zlib

import numpy as np

import instrumental_phrase as phrase
from instrumental_preview import source_anchors
from library import Library

PHASE = Path(__file__).resolve().parents[1]/'work/acoustic-parts-integration-001/phrase-boundary-001'


def npy(array):
    stream = io.BytesIO()
    np.lib.format.write_array(stream, array, allow_pickle=False)
    return stream.getvalue()


def archive(arrays, *, payloads=None, method=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=method) as target:
        for key in phrase.WIDTHS:
            target.writestr(key+'.npy', (payloads or {}).get(key, npy(arrays[key])))
    return stream.getvalue()


def fixture(tail=0):
    origin, end = 987654, 987654+88200+tail
    anchors = source_anchors(origin, end); frames = len(anchors)-1
    arrays = {key:np.zeros((frames, width), dtype='<f4') for key, width in phrase.WIDTHS.items()}
    arrays['note'][10:80, 55-21] = .7  # Support continues past the saved proposal.
    arrays['onset'][10, 55-21] = .8
    arrays['onset'][50:52, 55-21] = .6  # Existing first-frame plateau rule.
    arrays['note'][20:50, 43-21] = .8
    arrays['onset'][20, 43-21] = .9
    arrays['note'][40:60, 56-21] = .2
    arrays['onset'][40, 56-21] = .1
    arrays['note'][50:66, 52-21] = .4
    for first, last in ((100, 114), (120, 134)):
        arrays['note'][first:last, 60-21] = .6
        arrays['onset'][first, 60-21] = .75
    arrays['contour'][:, 101:104] = np.array([.11, .22, .33], dtype='<f4')
    raw = archive(arrays)
    def event(ident, first, last, pitch):
        return {'id':ident, 'start':anchors[first], 'end':anchors[last], 'pitch':pitch,
                'start_frame':first, 'end_frame_exclusive':last, 'mean_note_activation':.4,
                'start_evidence':'authored proposal, not source truth', 'authored_velocity':80}
    events = [event('sustain', 10, 30, 55), event('octave', 20, 50, 43),
              event('weak', 40, 60, 56), event('other-string', 50, 66, 52),
              event('pluck-1', 100, 114, 60), event('pluck-2', 120, 134, 60)]
    clock = {'sample_rate':44100, 'origin':origin, 'end':end, 'frame_samples':anchors[:-1],
        'source_coverage':{'first_anchor':origin, 'last_boundary':anchors[-1],
                          'passage_end':end, 'uncovered_tail_samples':end-anchors[-1]}}
    onset = deepcopy(events); onset[0]['id']='onset-sustain'; onset[0]['end']=anchors[40]; onset[0]['end_frame_exclusive']=40
    bundle = {'version':1, 'source_id':'authored-original', 'source_sha256':hashlib.sha256(b'original').hexdigest(),
        'sample_rate':44100, 'origin':origin, 'end':end,
        'source_part':{'id':'umx:other', 'instrument':'unknown', 'label':'Authored uncertain mixture'},
        'warnings':['No model or source observation occurred'],
        'provenance':{'pipeline_version':phrase.PIPELINE_VERSION, 'model_sha256':phrase.MODEL_SHA},
        'interpretations':{
            'published':{'policy':'published_default', 'source_clock':deepcopy(clock),
                'settings':dict(onset_thresh=.5, frame_thresh=.3, min_note_len=11,
                                infer_onsets=True, melodia_trick=True, energy_tol=11), 'events':events},
            'onset_only':{'version':'basic-pitch-onset-only-research-v1', 'model_sha256':phrase.MODEL_SHA,
                'source_clock':deepcopy(clock), 'policy':dict(onset_threshold=.5, frame_threshold=.3,
                    minimum_duration_frames=0, gap_tolerance_frames=0, infer_onsets=False, melodia_trick=False),
                'events':onset}},
        'raw_activations':{'file':'activations.npz', 'sha256':hashlib.sha256(raw).hexdigest(), 'bytes':len(raw),
            'finite':True, 'range':[0,1], 'sample_rate':44100, 'origin':origin, 'end':end,
            'frame_samples':anchors[:-1], 'arrays':{key:dict(shape=list(value.shape), dtype='float32') for key,value in arrays.items()}}}
    request = {'start_sample':anchors[10], 'end_sample':anchors[80], 'pitches':[55], 'context_frames':2}
    return bundle, arrays, request, raw


class PureEvidence(unittest.TestCase):
    def setUp(self):
        self.bundle, self.arrays, self.request, self.raw = fixture()
        self.anchors = source_anchors(self.bundle['origin'], self.bundle['end'])

    def report(self, **changes):
        return phrase.phrase_boundary_evidence(self.bundle, self.arrays, {**self.request, **changes})

    def test_sustained_support_after_unchanged_candidate_end_and_both_policies(self):
        before = deepcopy(self.bundle); arrays = {k:v.copy() for k,v in self.arrays.items()}
        request = deepcopy(self.request); report = self.report()
        self.assertEqual(report['clock']['bracket']['samples'], [self.anchors[10], self.anchors[80]])
        self.assertTrue(report['traces']['55']['model_predicates']['note_ge_0_3'][40-8])
        for name in ('published', 'onset_only'):
            self.assertEqual([e['event'] for e in report['interpretations'][name]['events']],
                             [e for e in self.bundle['interpretations'][name]['events'] if e['start_frame']<82])
        self.assertIsNone(report['binding']['parent'])
        self.assertEqual(report['source_part']['instrument'], 'unknown')
        self.assertEqual(self.bundle, before); self.assertEqual(self.request, request)
        for key in arrays: np.testing.assert_array_equal(self.arrays[key], arrays[key])
        report['interpretations']['published']['events'][0]['event']['end'] = 1
        self.assertEqual(self.bundle, before)

    def test_genuine_rest_has_zero_cells_without_invented_events(self):
        report = self.report(start_sample=self.anchors[82], end_sample=self.anchors[98], context_frames=0)
        self.assertEqual(report['traces']['55']['note'], [0.]*16)
        self.assertEqual(report['traces']['55']['onset'], [0.]*16)
        self.assertFalse(report['interpretations']['published']['events'])
        self.assertIn('rest versus estimator or recording dropout', report['semantics']['pending'])

    def test_rapid_reattacks_and_plateau_are_not_merged_or_ranked(self):
        report = self.report(start_sample=self.anchors[99], end_sample=self.anchors[135], pitches=[60], context_frames=0)
        self.assertEqual([e['event']['id'] for e in report['interpretations']['published']['events']], ['pluck-1','pluck-2'])
        self.assertEqual(sum(report['traces']['60']['model_predicates']['onset_only_peak']), 2)
        report = self.report()
        flags = report['traces']['55']['model_predicates']['onset_only_peak']
        self.assertTrue(flags[50-8]); self.assertFalse(flags[51-8])

    def test_octave_neighbor_other_pitch_and_weak_candidates_are_all_retained(self):
        report = self.report()
        rows = {e['event']['id']:e for e in report['interpretations']['published']['events']}
        self.assertEqual(rows['octave']['relations'], ['one_octave'])
        self.assertEqual(rows['weak']['relations'], ['within_two_semitones'])
        self.assertEqual(rows['other-string']['relations'], ['other_overlapping_pitch'])
        self.assertIn('sustain', rows)
        self.assertEqual(rows['weak']['event']['mean_note_activation'], .4)
        self.assertNotIn('confidence', rows['weak'])

    def test_contour_keeps_three_raw_bins_and_sampling_ambiguity(self):
        report = self.report(); contour = report['traces']['55']['contour']
        self.assertEqual(contour['bins'], [101,102,103])
        self.assertEqual(contour['midi_coordinates'], [21+b/3 for b in contour['bins']])
        np.testing.assert_array_equal(np.array(contour['values']), self.arrays['contour'][8:82, 101:104])
        edge = self.report(pitches=[21])['traces']['21']['contour']
        self.assertEqual(edge['bins'], [0,1,2]); self.assertTrue(edge['edge_adjusted'])

    def test_source_seam_partial_frames_and_context_censoring(self):
        report = self.report(start_sample=self.anchors[141]+1, end_sample=self.anchors[143]-1, context_frames=1)
        self.assertEqual(report['clock']['bracket']['first_frame'], 141)
        self.assertEqual(report['clock']['bracket']['end_boundary_index'], 143)
        self.assertEqual(report['frames']['samples'], self.anchors[140:144])
        self.assertNotEqual(self.anchors[142]-self.anchors[141], 512)
        self.assertTrue(report['clock']['censoring']['query_starts_inside_frame'])
        self.assertTrue(report['clock']['censoring']['query_ends_inside_frame'])
        edge = self.report(start_sample=self.anchors[0], end_sample=self.anchors[2], context_frames=8)
        self.assertEqual(edge['clock']['context']['left_missing_frames'], 8)
        self.assertTrue(edge['clock']['censoring']['at_parent_start'])

    def test_uncovered_tail_is_explicit_and_has_no_fabricated_frame(self):
        bundle, arrays, _, _ = fixture(100)
        request = {**self.request, 'start_sample':bundle['end']-50, 'end_sample':bundle['end'], 'context_frames':0}
        report = phrase.phrase_boundary_evidence(bundle, arrays, request)
        self.assertIsNone(report['clock']['bracket']['first_frame'])
        self.assertEqual(report['frames']['indices'], [])
        self.assertEqual(report['clock']['censoring']['uncovered_query_samples'], [bundle['end']-50,bundle['end']])

    def test_request_and_model_geometry_refusals(self):
        for changes in ({'start_sample':True}, {'end_sample':float(self.request['end_sample'])},
                        {'pitches':[]}, {'pitches':[55,55]}, {'pitches':[True]}, {'pitches':list(range(21,30))},
                        {'context_frames':9}, {'context_frames':False}, {'extra':'path'},
                        {'start_sample':self.bundle['origin']-1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.report(**changes)
        for key, value in (('sample_rate',48000), ('source_sha256','bad')):
            bundle=deepcopy(self.bundle); bundle[key]=value
            with self.assertRaises(ValueError): phrase.phrase_boundary_evidence(bundle,self.arrays,self.request)
        bundle=deepcopy(self.bundle); bundle['raw_activations']['frame_samples'][0]=float(self.anchors[0])
        with self.assertRaises(ValueError): phrase.phrase_boundary_evidence(bundle,self.arrays,self.request)

    def test_array_dtype_shape_nonfinite_range_and_input_preservation(self):
        for value in (self.arrays['note'].astype('float64'), self.arrays['note'].astype('>f4'), self.arrays['note'][:-1]):
            with self.assertRaises(ValueError): phrase.phrase_boundary_evidence(self.bundle,{**self.arrays,'note':value},self.request)
        for value in (float('nan'),float('inf'),-.1,1.1):
            arrays={k:v.copy() for k,v in self.arrays.items()}; arrays['note'][0,0]=value
            with self.assertRaises(ValueError): phrase.phrase_boundary_evidence(self.bundle,arrays,self.request)
            self.assertEqual(np.isnan(arrays['note'][0,0]), np.isnan(value))

    def test_policy_hash_distinguishes_full_interpretation_from_declaration(self):
        original = self.report()['binding']['policies']['published']
        bundle=deepcopy(self.bundle); bundle['interpretations']['published']['events'][0]['authored_velocity']=79
        changed=phrase.phrase_boundary_evidence(bundle,self.arrays,self.request)['binding']['policies']['published']
        self.assertNotEqual(original['interpretation_sha256'], changed['interpretation_sha256'])
        self.assertEqual(original['policy_sha256'], changed['policy_sha256'])

    def test_cancellation_and_report_cap_do_not_mutate_candidates(self):
        before=deepcopy(self.bundle); calls=[0]
        def cancel(): calls[0]+=1; return calls[0]>12
        with self.assertRaises(InterruptedError): phrase.phrase_boundary_evidence(self.bundle,self.arrays,self.request,cancel=cancel)
        self.assertEqual(self.bundle,before)
        bundle=deepcopy(self.bundle); template=bundle['interpretations']['published']['events'][0]
        bundle['interpretations']['published']['events']=[{**template,'id':str(i),'padding':'x'*8192} for i in range(140)]
        with self.assertRaisesRegex(ValueError,'one MiB'): phrase.phrase_boundary_evidence(bundle,self.arrays,self.request)


@contextmanager
def ordinary_directory(path):
    phrase._plain(path,True)
    yield


class SavedEvidence(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=PHASE,prefix='authored-')))
        self.library=Library(self.root/'library')
        self.bundle,self.arrays,self.request,self.raw=fixture()
        source_path=self.library.stage/'authored-original'; source_path.write_bytes(b'original')
        self.source=self.library.publish(source_path,'authored.wav','audio/wav')
        self.bundle.update(source_id=self.source['source_id'],source_sha256=self.source['sha256'])
        self.identity=str(uuid.uuid4()); self.original=b'original'
        self.spec={'kind':'instrumental','asset_id':self.source['id'],'sha256':self.source['sha256'],
                   'start_seconds':self.bundle['origin']/44100,'end_seconds':self.bundle['end']/44100}
        (self.library.root/'analysis-evidence').mkdir()
        self.install(self.raw)
        self.stack.enter_context(patch.object(phrase,'_directory',side_effect=ordinary_directory))
        self.readers=[]
        def reader(path):
            stream=Path(path).open('rb'); self.readers.append(stream); return stream
        self.stack.enter_context(patch.object(phrase,'_protected_reader',side_effect=reader))
        self.sentinel=self.root/'unrelated'; self.sentinel.write_bytes(b'preserve unrelated')

    def install(self, raw):
        checksum=hashlib.sha256(raw).hexdigest()
        path=self.library.root/'analysis-evidence'/checksum
        if not path.exists(): path.write_bytes(raw)
        self.reference={'sha256':checksum,'bytes':len(raw),'format':'numpy-npz-v1'}
        self.bundle['raw_activations'].update(sha256=checksum,bytes=len(raw))
        self.result={'candidates':self.bundle,'evidence':self.reference}
        self.save()
        return path

    def save(self, state='complete'):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                (self.identity,state,1.,json.dumps(self.spec),json.dumps(self.result),None,123.))
        self.parent_hash=hashlib.sha256(phrase.parent_canonical(self.result)).hexdigest()

    def load(self, **kwargs):
        return phrase.load_phrase_boundary_evidence(self.library,self.identity,self.parent_hash,self.request,**kwargs)

    def test_valid_saved_load_exact_binding_held_readers_and_zero_writes(self):
        before=self.library.db.read_bytes(); job=deepcopy(self.library.job(self.identity))
        actual=phrase.phrase_boundary_evidence
        def observed(*args,**kwargs):
            self.assertEqual(len(self.readers),2); self.assertTrue(all(not s.closed for s in self.readers))
            return actual(*args,**kwargs)
        with patch.object(phrase,'phrase_boundary_evidence',side_effect=observed): report=self.load()
        self.assertEqual(report['binding']['authority'],'verified_saved_instrumental_job')
        self.assertEqual(report['binding']['parent']['result_sha256'],self.parent_hash)
        self.assertEqual(report['binding']['npz'],self.reference)
        self.assertEqual(self.library.job(self.identity),job); self.assertEqual(self.library.db.read_bytes(),before)
        self.assertTrue(all(s.closed for s in self.readers)); self.assertEqual(self.sentinel.read_bytes(),b'preserve unrelated')
        pure=actual(self.bundle,self.arrays,self.request)
        self.assertEqual(report['traces'],pure['traces'])
        self.assertEqual(report['binding']['array_sha256'],pure['binding']['array_sha256'])

    def test_numpy_force_zip64_and_stored_archives_are_accepted(self):
        stream=io.BytesIO(); np.savez_compressed(stream,**self.arrays)
        for raw in (stream.getvalue(),archive(self.arrays,method=zipfile.ZIP_STORED)):
            self.install(raw); self.assertEqual(self.load()['version'],phrase.VERSION)

    def test_state_parent_hash_source_association_and_paths_refuse(self):
        with self.assertRaises(ValueError): phrase.load_phrase_boundary_evidence(self.library,self.identity,'0'*64,self.request)
        for value in ('../outside',self.identity.upper()):
            with self.assertRaises(ValueError): phrase.load_phrase_boundary_evidence(self.library,value,self.parent_hash,self.request)
        self.save('running')
        with self.assertRaises(ValueError): self.load()
        self.save(); self.spec['sha256']='a'*64; self.save()
        with self.assertRaises(ValueError): self.load()

    def test_actual_source_npz_and_saved_metadata_tampering_refuse(self):
        source=self.library.media/self.source['sha256']; source.write_bytes(b'changed!')
        with self.assertRaises(ValueError): self.load()
        source.write_bytes(self.original)
        path=self.library.root/'analysis-evidence'/self.reference['sha256']; raw=path.read_bytes()
        path.write_bytes(raw[:-1]+b'X')
        with self.assertRaises(ValueError): self.load()
        path.write_bytes(raw)
        self.result['evidence']['bytes']+=1; self.save()
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.sentinel.read_bytes(),b'preserve unrelated')

    def test_archive_members_header_dtype_shape_order_and_nonfinite_payload_refuse(self):
        base=npy(self.arrays['note']); payload=base[128:]
        def header(text):
            raw=text.encode('ascii'); raw+=b' '*((118-len(raw)-1)%64)+b'\n'
            return b'\x93NUMPY\x01\x00'+struct.pack('<H',len(raw))+raw+payload
        malformed=[
            header("{'descr': '|O', 'fortran_order': False, 'shape': (173, 88)}"),
            header("{'descr': '<f4', 'fortran_order': True, 'shape': (173, 88)}"),
            header("{'descr': '<f4', 'fortran_order': False, 'shape': (88, 173)}"),
            header("{'descr': '<f4', 'fortran_order': False, 'shape': (173, 88), 'descr':'<f4'}"),
            b'\x93NUMPY\x01\x00\xff\xff'+b' ' * 20]
        bad=self.arrays['note'].copy(); bad[30,0]=np.nan; malformed.append(npy(bad))
        for raw in malformed:
            self.install(archive(self.arrays,payloads={'note':raw}))
            with self.subTest(header=raw[:16]),self.assertRaises(ValueError): self.load()
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as out:
            for name in ('../note.npy','onset.npy','contour.npy'): out.writestr(name,b'not arrays')
        self.install(stream.getvalue())
        with self.assertRaises(ValueError): self.load()

    def test_excess_deflate_output_cannot_hide_behind_declared_npy_length(self):
        prefix=npy(self.arrays['note'])
        raw=bytearray(archive(self.arrays,payloads={'note':prefix+b'UNDECLARED'*40}))
        # Forge both headers to declare only the valid prefix and its CRC. The
        # compressed stream still inflates the extra data. ZipExtFile hides it.
        crc=zlib.crc32(prefix); struct.pack_into('<L',raw,14,crc); struct.pack_into('<L',raw,22,len(prefix))
        central=struct.unpack('<4s4H2LH',raw[-22:])[6]
        struct.pack_into('<L',raw,central+16,crc); struct.pack_into('<L',raw,central+24,len(prefix))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive_file:
            self.assertEqual(archive_file.read('note.npy'),prefix)
        with self.assertRaisesRegex(ValueError,'beyond declared'):
            phrase._load_arrays(io.BytesIO(raw),len(raw),len(self.arrays['note']),lambda:False)
        self.install(bytes(raw))
        with self.assertRaises(ValueError): self.load()

    def test_cancellation_closes_readers_and_preserves_all_owned_files(self):
        before=self.library.job(self.identity); calls=[0]
        def cancel(): calls[0]+=1; return calls[0]>15
        with self.assertRaises(InterruptedError): self.load(cancel=cancel)
        self.assertTrue(all(s.closed for s in self.readers)); self.assertEqual(self.library.job(self.identity),before)
        self.assertEqual(self.sentinel.read_bytes(),b'preserve unrelated')


if __name__=='__main__': unittest.main()
