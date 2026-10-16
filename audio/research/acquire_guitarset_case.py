"""Acquire one authorized official GuitarSet pair, then inspect annotations only.

No model, native renderer, playback, or whole-archive download. Byte limits count
HTTP response bodies (not TLS/HTTP overhead). A fresh directory is mandatory.
"""
import binascii
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import urllib.request
import wave
import zlib

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'guitarset' / 'case-001'
CAP = 10 * 1024 ** 2
EXPANDED_CAP = 20 * 1024 ** 2
PRIOR_ZENODO_BODY_BYTES = 256861  # API7292 + preview HTML118497 + two65536 tails.
CASE = '00_SS3-98-C_comp'
BASE = 'https://zenodo.org/api/records/3371780'
ITEMS = [
    dict(archive='audio_mono-mic.zip', archive_bytes=656927981,
         archive_md5='275966d6610ac34999b58426beb119c3',
         member=CASE+'_mic.wav', compressed=2186166, expanded=3455614,
         crc32='d142651d', local_header_offset=461279378),
    dict(archive='annotation.zip', archive_bytes=39132574,
         archive_md5='b39b78e63d3446f2e54ddb7a54df9b10',
         member=CASE+'.jams', compressed=274689, expanded=4470690,
         crc32='53103392', local_header_offset=36525012),
]


def save(name, value):
    with (OUT/name).open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.write('\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    used = sum(p.stat().st_size for tree in (ROOT, ROOT.parent/'vendor/analysis')
               for p in tree.rglob('*') if p.is_file())
    free = shutil.disk_usage(ROOT).free
    if used + EXPANDED_CAP > 4*1024**3 or free-EXPANDED_CAP < 8*1024**3:
        raise RuntimeError('Research storage cap or reserve')
    OUT.mkdir(parents=True, exist_ok=False)
    plan = dict(case=CASE, record=3371780, record_version='1.1.0',
                license='CC-BY-4.0', license_url='https://creativecommons.org/licenses/by/4.0/',
                creators=['Qingyang Xi','Rachel M. Bittner','Johan Pauwels','Xuzhou Ye','Juan P. Bello'],
                citation='GuitarSet: A Dataset for Guitar Transcription, ISMIR 2018',
                provenance_url='https://zenodo.org/records/3371780', development_example=True,
                held_out_from_model_training=False, items=ITEMS,
                response_body_cap_bytes=CAP, expanded_retained_cap_bytes=EXPANDED_CAP,
                prior_known_zenodo_body_bytes=PRIOR_ZENODO_BODY_BYTES,
                prior_web_research_traffic='Not measured; not dataset acquisition',
                byte_accounting='HTTP response-body bytes, excludes protocol/TLS overhead',
                research_bytes_before=used, free_disk_before=free,
                source_sha256=digest(Path(__file__).read_bytes()),
                timing='WAV frame zero is JAMS second zero; no fitted shift or original-session offset',
                annotation_gate=dict(note_midi_strings=6, expected_sample_rate=44100,
                    expected_channels=1, expected_sample_width=2,
                    finite_intervals_within_audio=True, duration_tolerance_samples=2,
                    minimum_peak_polyphony=2, no_inference=True,
                    diagnostics=['per-string notes','same-string repeated rounded pitch',
                        'gates >=500ms','global annotated rests >=50ms',
                        'distinct-string consecutive onset spacing <=20ms and 20-200ms'],
                    styles='No assertion of arpeggio or strum from genre or spacing alone'))
    save('frozen-plan.json', plan)
    (OUT/'source-at-acquisition.py').write_bytes(Path(__file__).read_bytes())
    (OUT/'README.md').write_text(
        '# One GuitarSet development example\n\n'
        'Source: https://zenodo.org/records/3371780, version 1.1.0.\n'
        'Creators: Qingyang Xi, Rachel M. Bittner, Johan Pauwels, Xuzhou Ye, Juan P. Bello.\n'
        'Citation: GuitarSet: A Dataset for Guitar Transcription, ISMIR 2018.\n'
        'Dataset license: CC BY 4.0, https://creativecommons.org/licenses/by/4.0/.\n'
        'The MIT license in the helper-code repository does not replace the dataset license.\n'
        'Exact original members only; no audio modifications. No playback.\n'
        'This is a development diagnostic, not model-held-out evidence.\n'
        'The frozen plan precedes acquisition; body-byte ledger excludes TLS/HTTP overhead.\n',encoding='utf-8')
    ledger = []
    used_network = PRIOR_ZENODO_BODY_BYTES

    def get(url, *, interval=None, maximum=131072):
        nonlocal used_network
        headers = {'User-Agent':'Guitar-app-bounded-dataset-acquisition', 'Accept-Encoding':'identity'}
        if interval:
            first,last,total = interval
            headers['Range'] = f'bytes={first}-{last}'
            maximum = last-first+1
        if used_network+maximum+1 > CAP:
            raise RuntimeError('Acquisition body cap would be exceeded')
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            status = response.status
            length = response.headers.get('Content-Length')
            entry = dict(url=url, range=headers.get('Range'), status=status,
                         content_range=response.headers.get('Content-Range'),
                         content_length=length, etag=response.headers.get('ETag'), bytes=0)
            ledger.append(entry)
            if response.headers.get('Content-Encoding') not in (None, 'identity'):
                raise RuntimeError('Unexpected content encoding; no body read')
            if interval:
                expected = f'bytes {first}-{last}/{total}'
                if status != 206 or entry['content_range'] != expected or length != str(maximum):
                    raise RuntimeError('Range/length not honored; no body read')
            elif status != 200 or (length is not None and int(length) > maximum):
                raise RuntimeError('Metadata response status/length')
            data = response.read(maximum+1)
            entry['bytes'] = len(data)
            entry['sha256'] = digest(data)
            used_network += len(data)
            if len(data)>maximum or (interval and len(data)!=maximum):
                raise RuntimeError('Unexpected response body length')
            return data

    try:
        metadata = get(BASE)
        (OUT/'record-metadata.json').write_bytes(metadata)
        record = json.loads(metadata)
        if record['metadata']['license']['id'] != 'cc-by-4.0':
            raise RuntimeError('Dataset license changed')
        files = {f['key']:f for f in record['files']}
        manifest = []
        for item in ITEMS:
            info=files[item['archive']]
            if info['size'] != item['archive_bytes'] or info['checksum'] != 'md5:'+item['archive_md5']:
                raise RuntimeError('Archive metadata changed')
            url=BASE+'/files/'+item['archive']+'/content'; total=item['archive_bytes']
            tail=get(url,interval=(total-22,total-1,total))
            e=struct.unpack('<4s4H2IH',tail)
            if e[0]!=b'PK\x05\x06' or e[1:3]!=(0,0) or e[3]!=e[4] or e[7]!=0:
                raise RuntimeError('Unexpected archive trailer')
            count,size,offset=e[4],e[5],e[6]
            if count!=360 or not 0<size<65536 or offset+size!=total-22:
                raise RuntimeError('Archive directory shape')
            central=get(url,interval=(offset,offset+size-1,total))
            (OUT/(item['archive']+'.central-directory.bin')).write_bytes(central)
            p=0; matches=[]
            for _ in range(count):
                h=struct.unpack_from('<4s6H3I5H2I',central,p)
                if h[0]!=b'PK\x01\x02':raise RuntimeError('Central header signature')
                name=central[p+46:p+46+h[10]].decode('utf-8')
                if name==item['member']:matches.append(h)
                p+=46+h[10]+h[11]+h[12]
            if p!=len(central) or len(matches)!=1:raise RuntimeError('Central member lookup')
            h=matches[0]
            if (h[4],h[8],h[9],h[16],format(h[7],'08x'))!=(8,item['compressed'],item['expanded'],item['local_header_offset'],item['crc32']) or h[3]&1:
                raise RuntimeError('Central member metadata changed')
            pos=item['local_header_offset']; local=get(url,interval=(pos,pos+29,total))
            l=struct.unpack('<4s5H3I2H',local)
            if l[0]!=b'PK\x03\x04' or l[2]!=h[3] or l[3]!=8 or l[9]+l[10]>4096:
                raise RuntimeError('Local header shape')
            names=get(url,interval=(pos+30,pos+29+l[9]+l[10],total))
            if names[:l[9]].decode('utf-8')!=item['member']:raise RuntimeError('Local filename differs')
            if not l[2]&8 and (l[6],l[7],l[8])!=(h[7],h[8],h[9]):
                raise RuntimeError('Local sizes/CRC differ')
            data_start=pos+30+l[9]+l[10]
            packed=get(url,interval=(data_start,data_start+h[8]-1,total))
            if l[2]&8:
                descriptor=get(url,interval=(data_start+h[8],data_start+h[8]+15,total))
                if struct.unpack('<4s3I',descriptor)!=(b'PK\x07\x08',h[7],h[8],h[9]):
                    raise RuntimeError('Data descriptor differs')
            inflater=zlib.decompressobj(-15)
            data=inflater.decompress(packed,h[9]+1)
            if len(data)!=h[9] or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                raise RuntimeError('Expanded member length/stream mismatch')
            if binascii.crc32(data)!=h[7]:raise RuntimeError('Expanded CRC mismatch')
            (OUT/item['member']).write_bytes(data)
            manifest.append({**item,'url':url,'data_range':[data_start,data_start+h[8]-1],
                             'sha256':digest(data),'compressed_sha256':digest(packed),
                             'archive_md5_scope':'Publisher whole-archive value; not recomputed from partial ranges',
                             'member_sha256_scope':'Locally computed; no independent publisher SHA256 for this member'})
        save('manifest.json',manifest)
        inspect_annotations()
        size=sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())
        if size>EXPANDED_CAP:raise RuntimeError('Retained expansion cap')
        save('completion.json',dict(retained_bytes_before_receipt=size, audio_played=False,
                                   inference_run=False, rendered=False))
    except Exception as error:
        save('failed.json',dict(type=type(error).__name__,error=str(error),retained=True))
        raise
    finally:
        save('byte-ledger.json',dict(prior_known_zenodo_body_bytes=PRIOR_ZENODO_BODY_BYTES,
             requests=ledger,new_response_body_bytes=sum(r['bytes'] for r in ledger),
             cumulative_known_zenodo_body_bytes=used_network,cap_bytes=CAP))


def inspect_annotations():
    with wave.open(str(OUT/(CASE+'_mic.wav'))) as w:
        rate,channels,width,frames=w.getframerate(),w.getnchannels(),w.getsampwidth(),w.getnframes()
        if (rate,channels,width,w.getcomptype())!=(44100,1,2,'NONE'):
            raise ValueError('Unexpected WAV timing/format')
    jam=json.loads((OUT/(CASE+'.jams')).read_bytes())
    duration=frames/rate; annotations=[a for a in jam['annotations'] if a['namespace']=='note_midi']
    if len(annotations)!=6:raise ValueError('Expected six note_midi strings')
    if abs(jam['file_metadata']['duration']-duration)>2/rate:raise ValueError('JAMS/WAV duration differs')
    rows=[]; counts=[]; repeats=[]; identifiers=[]; overlaps=[]
    for string,a in enumerate(annotations):
        ident=a.get('annotation_metadata',{}).get('data_source');identifiers.append(ident)
        notes=[]
        for i,n in enumerate(a['data']):
            t,d,p=n['time'],n['duration'],n['value']
            if any(type(x) not in (int,float) or not math.isfinite(x) for x in (t,d,p)) or not 0<=t<t+d<=duration+2/rate or not 0<=p<=127:
                raise ValueError('Invalid note bounds/value')
            notes.append(dict(string=string,index=i,start=t,end=t+d,pitch=p))
        notes.sort(key=lambda n:(n['start'],n['end'],n['index'])); counts.append(len(notes));rows+=notes
        for a,b in zip(notes,notes[1:]):
            if a['end']>b['start']:overlaps.append([string,a['index'],b['index'],a['end']-b['start']])
            if round(a['pitch'])==round(b['pitch']):repeats.append([string,a['index'],b['index'],b['start']-a['start']])
    if {str(x) for x in identifiers}!={str(x) for x in range(6)}:raise ValueError('Ambiguous string identifiers')
    events=sorted([(n['start'],1) for n in rows]+[(n['end'],-1) for n in rows])
    active=peak=0; previous=0.; poly_seconds=0.; rests=[]
    for t,delta in events:
        if active>=2:poly_seconds+=t-previous
        if active==0 and t-previous>=.05:rests.append([previous,t])
        active+=delta;peak=max(peak,active);previous=t
    if duration-previous>=.05:rests.append([previous,duration])
    ordered=sorted(rows,key=lambda n:(n['start'],n['string'],n['index']))
    spacing=[b['start']-a['start'] for a,b in zip(ordered,ordered[1:]) if a['string']!=b['string']]
    report=dict(schema_timing_gate_passed=True,polyphonic_case_gate_passed=peak>=2,
        wav=dict(sample_rate=rate,channels=channels,sample_width=width,frames=frames,duration_seconds=duration),
        file_metadata=jam['file_metadata'],annotation_namespaces=[a['namespace'] for a in jam['annotations']],
        note_string_ids=identifiers,notes_per_string=counts,total_notes=len(rows),peak_polyphony=peak,
        annotated_polyphony_seconds=poly_seconds,same_string_overlaps=overlaps,
        same_string_same_rounded_pitch_successions=repeats,
        held_gate_count_500ms=sum(n['end']-n['start']>=.5 for n in rows),
        global_annotated_rests_50ms=rests,
        distinct_string_consecutive_onsets_within_20ms=sum(0<=x<=.020 for x in spacing),
        distinct_string_consecutive_onsets_20_to_200ms=sum(.020<x<=.200 for x in spacing),
        limits=['Onsets manually validated with signal adjustment; offsets automatically estimated',
                'Onset-spacing categories do not prove strum/arpeggio technique',
                'Model training overlap unresolved; development example only',
                'No annotation used to change model thresholds or infer events'])
    save('annotation-gate.json',report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
