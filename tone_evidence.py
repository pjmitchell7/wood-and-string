"""Separate processed-tone evidence. Existing Electric dry receipts stay unchanged.

The receipt binds a trusted processor's output, not a perceptual or fresh DSP
execution claim. Verification reconstructs PCM from retained F64 and verifies
the complete dry chain. Unknown/unreferenced CAS files are conservatively kept.
"""
import array
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid

from render_evidence import (RenderEvidence, _held, _plain, _json, _need, _sha,
                             _cancel, validate_reference as validate_dry)
from audio import electric_tone as component
from audio import electric_tone_stream as overlap
from audio import electric_tone_worker as stage

FORMAT = 'electric-tone-evidence-v1'
RENDER_VERSION = 'electric-preview-tone-v1'


def verify_failed_preview(library,result,selection,state,cancel=lambda:False):
    """A post-dry failed/cancelled stage preserves dry truth, never a wet asset."""
    _need(state in ('failed','cancelled') and 'electric_tone' in selection,'Invalid effects failure state')
    _need(type(result) is dict and not any(key in result for key in ('asset','effects','tone_evidence')),
          'An effects failure cannot declare a processed asset')
    failure=result.get('effects_failure');identity=result.get('dry_input')
    _need(type(failure) is dict and failure.get('version')==1 and failure.get('stage')=='effects'
          and failure.get('status')==state and failure.get('settings')==selection['electric_tone'],
          'Missing or crossed effects failure diagnosis')
    _need(type(identity) is dict and set(identity)=={'sha256','bytes'} and _sha(identity['sha256'])
          and type(identity['bytes']) is int,'Invalid retained dry input identity')
    dry=RenderEvidence(library).verify(result['render_evidence'],wav_sha256=identity['sha256'],
        arrangement=result['arrangement'],cancel=cancel)['receipt']
    _need(identity['bytes']==44+4*dry['frames']
          and stage.canonical(failure.get('source_clock'))==stage.canonical(
              {'sample_rate':44100,'origin_sample':dry['source_origin_sample']}),'Effects failure source clock differs')
    _need(type(result.get('tone_process')) is dict,'Missing bounded process diagnosis')
    return dry


def validate_reference(ref):
    _need(type(ref) is dict and set(ref)=={'format','raw','receipt'} and ref['format']==FORMAT,'Invalid tone evidence reference')
    for key,limit in (('raw',stage.MAX_RAW),('receipt',stage.MAX_RECEIPT)):
        item=ref[key]
        _need(type(item) is dict and set(item)=={'sha256','bytes'} and _sha(item['sha256'])
              and type(item['bytes']) is int and 0<item['bytes']<=limit,'Invalid tone member identity')
    _need(ref['raw']['bytes']%16==0,'Invalid tone F64 geometry')
    return ref


def _read(stack,path,identity,maximum):
    handle=_held(stack,path,maximum)
    _need(os.fstat(handle.fileno()).st_size==identity['bytes'],'Tone member length differs')
    digest=hashlib.file_digest(handle,'sha256').hexdigest();handle.seek(0)
    _need(digest==identity['sha256'],'Tone member hash differs')
    return handle


def _windows(frames,tail):
    if frames<=component.MAX_FRAMES:
        return [dict(input_start=0,input_end=frames,retained_start=0,retained_end=frames+tail)]
    step=component.MAX_FRAMES-tail
    return [dict(input_start=max(0,start-tail),input_end=min(frames,start+step),retained_start=start,
                 retained_end=min(frames,start+step)+(tail if start+step>=frames else 0))
            for start in range(0,frames,step)]


def _dry_levels(stack,dry,reference,cancel):
    handle=_read(stack,dry['raw_path'],reference['raw'],31*44100*8)
    receipt=dry['receipt'];gain=10**(-3/20)/receipt['raw_peak'] if receipt['level_policy']=='peak' else .25
    samples=array.array('d')
    while data:=handle.read(65536):
        _cancel(cancel);values=array.array('f');values.frombytes(data)
        if sys.byteorder!='little':values.byteswap()
        samples.extend(round(value*gain*32768)/32768 for value in values)
    return component._identity(samples),component._levels(samples,cancel)


def _verify(stack,raw_path,receipt,ref,*,dry_reference,arrangement,wav_sha256,settings,library,wav_path=None,cancel=lambda:False):
    validate_reference(ref);validate_dry(dry_reference)
    _need(type(receipt) is dict and set(receipt)=={'version','binding','processing','raw','wav','pcm_peak',
          'maximum_quantization_residual_lsb','clipped_samples','musical_acceptance'} and receipt['version']==stage.VERSION,'Tone receipt schema changed')
    config=component.normalize_settings(settings)
    _need(config==settings and config['mode']=='clean' and config['overload']=='refuse','Invalid saved tone settings')
    binding=receipt['binding'];metadata=receipt['processing']
    _need(type(binding) is dict and set(binding)=={'input','settings','source_origin_sample','dry_evidence','source_commands_sha256'},'Invalid tone input binding')
    _need(binding['settings']==config and binding['dry_evidence']==dry_reference,'Tone settings/dry reference differ')
    _need(type(binding['input']) is dict and set(binding['input'])=={'sha256','bytes'} and _sha(binding['input'].get('sha256')),'Invalid tone dry WAV identity')
    dry=RenderEvidence(library).verify(dry_reference,wav_sha256=binding['input']['sha256'],arrangement=arrangement,cancel=cancel)
    dry_receipt=dry['receipt'];frames=dry_receipt['frames'];origin=dry_receipt['source_origin_sample'];tail=overlap.support(config)
    dry_identity,dry_levels=_dry_levels(stack,dry,dry_reference,cancel)
    _need(type(binding['input']['bytes']) is int and binding['input']['bytes']==44+frames*4
          and type(binding['source_origin_sample']) is int and binding['source_origin_sample']==origin
          and binding['source_commands_sha256']==dry_receipt['source_commands_sha256'],'Tone source clock/commands differ')
    _need(type(metadata) is dict and metadata.get('version')==overlap.VERSION and metadata.get('component_version')==component.VERSION
          and metadata.get('settings')==config and stage.canonical(metadata.get('windows'))==stage.canonical(_windows(frames,tail)),'Tone processing policy differs')
    _need(set(metadata)=={'version','component_version','settings','sample_rate','source_origin_sample','input_frames','output_frames',
        'added_tail_samples','dry_input','output','input_levels','output_levels','windows','policy','inserted_alignment_delay_samples',
        'phase','quantization','pcm_sample_peak_limit','true_peak_measured','normalization','musical_acceptance','source_events_changed','export_refused'},'Unexpected tone diagnostic fields')
    _need(metadata['dry_input']==dry_identity and stage.canonical(metadata['input_levels'])==stage.canonical(dry_levels),'Tone input measurement differs')
    _need(metadata['policy']=='Exact finite-support lookback; retain each frame once; no crossfade or alignment fit'
          and metadata['phase']=='Frequency-dependent; group delay not compensated or measured'
          and metadata['quantization']=='round(sample*32768), nearest ties-to-even, no dither, no clipping','Tone method label differs')
    for key,expected in dict(sample_rate=44100,source_origin_sample=origin,input_frames=frames,
                             output_frames=frames+tail,added_tail_samples=tail,inserted_alignment_delay_samples=0).items():
        _need(type(metadata.get(key)) is int and metadata[key]==expected,'Tone geometry differs')
    for key in ('export_refused','true_peak_measured','normalization','musical_acceptance','source_events_changed'):
        _need(metadata.get(key) is False,'Tone scope flag differs')
    _need(metadata.get('pcm_sample_peak_limit')==overlap.PCM_LIMIT and receipt['clipped_samples']==0
          and type(receipt['clipped_samples']) is int and receipt['musical_acceptance'] is False,'Tone headroom/scope differs')
    _need(receipt['raw']==ref['raw'] and receipt['raw']['bytes']==(frames+tail)*16,'Tone raw identity differs')
    _need(type(receipt['wav']) is dict and receipt['wav']=={'sha256':wav_sha256,'bytes':44+(frames+tail)*4}
          and _sha(wav_sha256),'Tone WAV identity differs')
    raw=_read(stack,raw_path,ref['raw'],stage.MAX_RAW)
    wav=_held(stack,wav_path,stage.MAX_WAV) if wav_path is not None else None
    first=stage.header(frames+tail);checksum=hashlib.sha256(first)
    if wav is not None:
        _need(os.fstat(wav.fileno()).st_size==receipt['wav']['bytes'] and wav.read(44)==first,'Tone WAV header differs')
    peak=0.;energy=0.;pcm_peak=0;residual=0.;nonzero=0
    while data:=raw.read(8192*8):
        _cancel(cancel)
        values=array.array('d');values.frombytes(data)
        if sys.byteorder!='little':values.byteswap()
        pcm=array.array('h')
        for value in values:
            _need(math.isfinite(value) and abs(value)<=overlap.PCM_LIMIT,'Tone float overload or nonfinite value')
            peak=max(peak,abs(value));nonzero+=value!=0
            number=round(value*32768);pcm_peak=max(pcm_peak,abs(number));residual=max(residual,abs(number-value*32768));pcm.append(number)
        energy=math.fsum((energy,math.fsum(value*value for value in values)))
        if sys.byteorder!='little':pcm.byteswap()
        encoded=pcm.tobytes();checksum.update(encoded)
        if wav is not None:_need(wav.read(len(encoded))==encoded,'Tone PCM differs from retained float')
    _need(checksum.hexdigest()==wav_sha256 and type(receipt['pcm_peak']) is float and receipt['pcm_peak']==pcm_peak/32768
          and type(receipt['maximum_quantization_residual_lsb']) is float and receipt['maximum_quantization_residual_lsb']==residual and residual<=.5,'Tone quantization differs')
    measured=dict(sample_peak=peak,sample_peak_dbfs=20*math.log10(peak) if peak else None,
                  rms=math.sqrt(energy/(2*(frames+tail))),sum_squares=energy,nonzero_samples=nonzero,samples_above_full_scale=0)
    _need(stage.canonical(metadata.get('output_levels'))==stage.canonical(measured),'Tone level measurements differ')
    _need(metadata.get('output')==dict(sha256=ref['raw']['sha256'],bytes=ref['raw']['bytes'],typecode='d',
                                     byte_order='little',layout='interleaved-stereo'),'Tone float representation differs')
    return receipt


class ToneEvidence:
    def __init__(self,library):self.library=library;self.root=library.root/'tone-evidence'

    def verify(self,reference,*,dry_reference,arrangement,wav_sha256,settings,cancel=lambda:False):
        validate_reference(reference);_plain(self.root,directory=True)
        with ExitStack() as stack:
            data=_read(stack,self.root/(reference['receipt']['sha256']+'.json'),reference['receipt'],stage.MAX_RECEIPT).read()
            return _verify(stack,self.root/(reference['raw']['sha256']+'.f64'),_json(data),reference,
                dry_reference=dry_reference,arrangement=arrangement,wav_sha256=wav_sha256,settings=settings,library=self.library,cancel=cancel)

    def publish(self,identity,receipt_identity,*,dry_reference,arrangement,settings,cancel=lambda:False):
        _need(os.name=='nt','Tone publication requires Windows no-replace rename')
        folder=stage.work_path(self.library.root,identity)
        _need(self.library.job(identity)['state']=='running','Tone job is not active')
        _need(type(receipt_identity) is dict and set(receipt_identity)=={'sha256','bytes'} and _sha(receipt_identity['sha256'])
              and type(receipt_identity['bytes']) is int and 0<receipt_identity['bytes']<=stage.MAX_RECEIPT,'Invalid returned tone receipt')
        with ExitStack() as stack:
            data=_read(stack,folder/'receipt.json',receipt_identity,stage.MAX_RECEIPT).read();receipt=_json(data)
            _need(type(receipt) is dict,'Expected tone receipt object')
            ref=dict(format=FORMAT,raw=receipt.get('raw'),receipt=receipt_identity);validate_reference(ref)
            _verify(stack,folder/'processed.f64',receipt,ref,dry_reference=dry_reference,arrangement=arrangement,
                wav_sha256=receipt.get('wav',{}).get('sha256'),settings=settings,library=self.library,wav_path=folder/'processed.wav',cancel=cancel)
            self.library.space(ref['raw']['bytes']+ref['receipt']['bytes']);self.root.mkdir(exist_ok=True);_plain(self.root,directory=True)
            for name,suffix in (('raw','.f64'),('receipt','.json')):
                _cancel(cancel);member=ref[name];target=self.root/(member['sha256']+suffix)
                if target.exists():_read(stack,target,member,stage.MAX_RAW if name=='raw' else stage.MAX_RECEIPT);continue
                temporary=self.root/('.publish-'+str(uuid.uuid4())+'.partial')
                source=_read(stack,folder/('processed.f64' if name=='raw' else 'receipt.json'),member,
                             stage.MAX_RAW if name=='raw' else stage.MAX_RECEIPT)
                try:
                    with temporary.open('xb') as output:
                        while chunk:=source.read(65536):_cancel(cancel);output.write(chunk)
                        output.flush();os.fsync(output.fileno())
                    # No overwrite: both existing and new objects remain immutable.
                    _read(stack,temporary,member,stage.MAX_RAW if name=='raw' else stage.MAX_RECEIPT).close()
                    os.rename(temporary,target)
                finally:
                    if temporary.exists():temporary.unlink()
        return ref,receipt


def verify_preview(library,result,selection,*,cancel=lambda:False):
    try:
        asset=result['asset'];arrangement=result['arrangement']
        if 'electric_tone' not in selection:
            _need('tone_evidence' not in result and 'effects' not in result,'Dry preview contains an unexpected tone result')
            return RenderEvidence(library).verify(result['render_evidence'],wav_sha256=asset['sha256'],arrangement=arrangement,cancel=cancel)
        receipt=ToneEvidence(library).verify(result['tone_evidence'],dry_reference=result['render_evidence'],arrangement=arrangement,
            wav_sha256=asset['sha256'],settings=selection['electric_tone'],cancel=cancel)
        _need(result.get('effects')==receipt,'Saved effects diagnostics differ from retained receipt')
        return receipt
    except (KeyError,TypeError,AttributeError,OverflowError,RecursionError) as error:
        raise ValueError('Malformed saved Electric tone chain; preserve evidence') from error


def cleanup_work(library,identity):
    """After owned cleanup/worker.lock only; unknown content blocks deletion."""
    folder=stage.work_path(library.root,identity)
    if not folder.exists():return
    children=list(folder.iterdir())
    _need(len(children)<=len(stage.FILES) and all(p.name in stage.FILES for p in children),'Unknown tone work files; preserve')
    for path in children:_plain(path)
    for path in children:path.unlink()
    folder.rmdir()


def reconcile_work(library):
    for path in library.stage.glob('*.tone'):
        if re.fullmatch('[0-9a-f-]{36}\\.tone',path.name):cleanup_work(library,path.name[:-5])
