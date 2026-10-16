"""Existing preview lane -> owned bounded tone child -> independently bound CAS.

Only explicit clean selections enter this path. No analyzer or dry renderer is
replaced. Caller owns worker.lock and publishes the final asset afterward.
"""
from contextlib import ExitStack
from pathlib import Path
from audio.managed_process_receipts import run_worker
from render_evidence import RenderEvidence, _held, MAX_RAW_BYTES, MAX_RECEIPT_BYTES
from tone_evidence import ToneEvidence
from audio.electric_tone_worker import work_path

PROFILE={'wall_seconds':60,'memory_bytes':512*1024**2,'maximum_input_seconds':31,
         'memory_scope':'Sampled actual worker working set; not an OS hard memory cap'}


def bounded_process(value):
    """Preserve bounded lifecycle facts, never promote absent proof to success."""
    import json
    try:
        data=json.dumps(value,allow_nan=False,separators=(',',':')).encode()
        if len(data)>32768:raise ValueError()
        return json.loads(data)
    except (ValueError,TypeError,OverflowError,RecursionError):
        return {'record_unavailable':'Invalid or oversized process record','cleanup':{'proven':False}}


class ToneRefused(ValueError):
    def __init__(self,diagnosis,process):
        super().__init__('Electric tone would overload the export. Choose a lower explicit tone gain; dry evidence is preserved.')
        self.diagnosis=diagnosis;self.process=process


def process_preview(library,identity,arrangement,dry_reference,selection,*,cancel,progress,wall_seconds):
    output=library.stage/(identity+'.wav')
    store=RenderEvidence(library)
    with ExitStack() as stack:
        # These protected handles outlive the managed child AND its cleanup.
        raw_path,receipt_path=store._paths(dry_reference)
        _held(stack,raw_path,MAX_RAW_BYTES);_held(stack,receipt_path,MAX_RECEIPT_BYTES)
        import hashlib
        wav=_held(stack,output,44+31*44100*4)
        wav_sha=hashlib.file_digest(wav,'sha256').hexdigest();wav.seek(0)
        dry=store.verify(dry_reference,wav_sha256=wav_sha,arrangement=arrangement,cancel=cancel)['receipt']
        request=dict(root=str(library.root.resolve()),job_id=identity,
            input=dict(sha256=wav_sha,bytes=44+dry['frames']*4),settings=selection['electric_tone'],
            source_origin_sample=dry['source_origin_sample'],dry_evidence=dry_reference,
            source_commands_sha256=dry['source_commands_sha256'])
        library.space(64*1024**2)
        response=None
        try:
            response=run_worker('audio.electric_tone_worker',request,cancel=cancel,progress=progress,
                                wall_seconds=min(PROFILE['wall_seconds'],wall_seconds),memory_bytes=PROFILE['memory_bytes'])
            if response.get('cleanup',{}).get('proven') is not True:raise ValueError('Tone worker cleanup is unverified')
            value=response['result']
            if value.get('state')=='refused':raise ToneRefused(value['refusal'],response)
            if set(value)!={'state','receipt'} or value['state']!='processed':raise ValueError('Invalid tone stage result')
            reference,receipt=ToneEvidence(library).publish(identity,value['receipt'],dry_reference=dry_reference,
                arrangement=arrangement,settings=selection['electric_tone'],cancel=cancel)
        except Exception as error:
            metrics=getattr(error,'metrics',None)
            evidence=(dict(metrics=response.get('metrics'),cleanup=response.get('cleanup')) if type(response) is dict
                      else {'metrics':metrics,'cleanup':metrics.get('cleanup',{'proven':False}) if type(metrics) is dict else {'proven':False}})
            error.tone_process=bounded_process(evidence)
            raise
        return work_path(library.root,identity)/'processed.wav',reference,receipt,{
            'metrics':response['metrics'],'cleanup':response['cleanup']}
