"""One bounded render subprocess. No input stems enter this diagnostic."""
import json
from pathlib import Path
import sys
import time
import traceback
import hashlib
import wave
import threading
import ctypes
import os
import math
import sqlite3
from library import Library
from render_evidence import RenderEvidence, reconcile_render_evidence
from audio.performance import Note, Performance, arrange
from audio.render import render, RENDER_VERSION
from audio.preview_instruments import (validate_saved_selection, arrange_selected,
                                       required_roles, renderer_version)
from audio.bass_render import render as render_bass
from audio.electric_preview_render import render as render_electric
from audio.managed_process import run_worker, Cancelled, ManagedProcessError, parent_pipe_lost

MAX_ANALYSIS_BYTES = 4*1024**2

def load_analysis(path, receipt, source_id, start_seconds, end_seconds, *, roles=('melody',)):
    """Verify a bounded analysis envelope before creating renderable notes."""
    try:
        if not isinstance(receipt,dict) or type(receipt.get('result_bytes')) is not int:
            raise ValueError()
        size=receipt['result_bytes'];digest=receipt.get('result_sha256')
        if not 0<size<=MAX_ANALYSIS_BYTES or not isinstance(digest,str) or len(digest)!=64:
            raise ValueError()
        if path.is_symlink() or path.stat().st_size!=size: raise ValueError()
        with path.open('rb') as stream: raw=stream.read(MAX_ANALYSIS_BYTES+1)
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest: raise ValueError()
        def nonfinite(value): raise ValueError()
        value=json.loads(raw,parse_constant=nonfinite)
        if not isinstance(value,dict) or not isinstance(value.get('report'),dict): raise ValueError()
        data=value['performance'];origin=round(start_seconds*44100);end=round(end_seconds*44100)
        if not isinstance(data,dict) or data.get('source_id')!=source_id or type(data.get('sample_rate')) is not int or data['sample_rate']!=44100:
            raise ValueError()
        if type(value.get('render_origin_sample')) is not int or value['render_origin_sample']!=origin: raise ValueError()
        if data.get('version',1)!=1 or data.get('analysis_offset_samples',0)!=0: raise ValueError()
        rows=data['notes']
        if not isinstance(rows,list) or not 0<len(rows)<=4000: raise ValueError()
        notes=[]
        allowed={'id','start','end','pitch','velocity','role','confidence','phrase','contour'}
        for row in rows:
            if not isinstance(row,dict) or set(row)-allowed: raise ValueError()
            if not isinstance(row.get('id'),str) or not 0<len(row['id'])<=200: raise ValueError()
            if row.get('role') not in ('melody','bass'): raise ValueError()
            if type(row.get('start')) is not int or type(row.get('end')) is not int or not origin<=row['start']<row['end']<=end: raise ValueError()
            if 'phrase' in row and (not isinstance(row['phrase'],str) or len(row['phrase'])>200): raise ValueError()
            confidence=row.get('confidence',1)
            if type(confidence) not in (float,int) or not math.isfinite(confidence): raise ValueError()
            contour=row.get('contour',[])
            if not isinstance(contour,list) or len(contour)>4000: raise ValueError()
            for point in contour:
                if not isinstance(point,list) or len(point)!=2 or type(point[0]) is not int or type(point[1]) not in (int,float): raise ValueError()
            notes.append(Note(**row))
        performance=Performance(source_id,44100,notes)
        performance.validate()
        if not set(roles) <= {note.role for note in notes}:
            raise ValueError()
        return performance,value['report'],origin
    except (OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError):
        raise ValueError('The analysis result failed validation. Your original is unchanged.') from None

def run(root, identity, parent_gone=lambda:False):
    library=Library(root)
    output=library.stage/(identity+'.wav')
    cancel=library.stage/(identity+'.cancel')
    analysis=library.stage/(identity+'.analysis.json')
    analysis_partial=library.stage/(identity+'.analysis.partial')
    decoded=analysis.with_suffix('.decoded.wav')
    decoded_partial=decoded.with_suffix(decoded.suffix+'.decode.partial')
    job=library.job(identity)
    started=time.monotonic()
    duration_limit=240 if job['request']['kind'] in ('mixed','instrumental') else 120
    cancelled=lambda:cancel.exists() or parent_gone() or time.monotonic()-started>duration_limit
    try:
        library.update_job(identity,'running',0)
        spec=job['request'];report=None
        if spec['kind']=='instrumental':
            from instrumental_preview import analyze_job
            result=analyze_job(library,spec,identity,cancel=cancelled,
                progress=lambda value:library.update_job(identity,'running',value),run_worker=run_worker)
            if cancelled():raise InterruptedError('Cancelled')
            result['metrics']['seconds']=time.monotonic()-started
            library.update_job(identity,'complete',1,result=result)
            return
        selection=validate_saved_selection(spec)
        instrument=selection.get('instrument','acoustic')
        if selection and spec['kind'] not in ('mixed','refine','instrumental-arrange'):
            raise ValueError('Instrument selection is only supported for mixed previews and their revisions.')
        if spec.get('renderer_version') != renderer_version(selection):
            raise ValueError('The sampled renderer changed. Start a new preview.')
        analysis_metrics=None
        if cancelled(): raise InterruptedError('Cancelled')
        if spec['kind']=='instrumental-arrange':
            from instrumental_preview import prepare_arrangement,arrange_review
            prepared=prepare_arrangement(library,{key:spec[key] for key in ('parent_job_id','interpretation','note_ids','accept_as_lead')} |
                {'instrument':spec.get('instrument','acoustic')})
            if prepared['spec']!=spec:raise ValueError('The saved candidate review changed. Select the notes again.')
            source=prepared['source'];source_id=source['source_id'];origin=prepared['bundle']['origin']
            arrangement=arrange_review(prepared)
            kind='instrumental-selected-'+{'acoustic':'acoustic','jazz-bass':'bass','electric':'electric'}[instrument]
            filename=source['title']+' - selected instrumental notes.wav'
            if instrument=='electric':filename=source['title']+' - selected instrumental Electric notes.wav'
            notice='Your selected uncertain candidates assigned as a lead part. Source instruments remain unknown; fixed note dynamics and estimated endings are not expressive transcription. No source waveform is mixed into this render.'
        elif spec['kind']=='refine':
            from preview_refinement import prepare_refinement, arrange_refinement
            prepared=prepare_refinement(library,spec['parent_job_id'],spec['operations'])
            if prepared['spec'] != spec:
                raise ValueError('The saved parent preview changed. Start this refinement again.')
            source=prepared['source'];source_id=source['source_id'];origin=prepared['origin']
            report=prepared['parent']['result'].get('analysis')
            arrangement=arrange_refinement(prepared)
            kind='mixed-refinement-'+str(spec['performers'])
            filename=source['title']+' - revised acoustic preview.wav'
            if instrument=='jazz-bass':
                kind='bass-refinement-'+str(spec['performers'])+'-'+selection['bass_part']
                filename=source['title']+' - revised Jazz Bass preview.wav'
            elif instrument=='electric':
                kind='electric-refinement-'+str(spec['performers'])
                filename=source['title']+' - revised Electric preview.wav'
            notice='Revised from your explicit note edits. Original analysis and previous preview are preserved. No new automatic transcription or source audio is mixed into this render.'
        elif spec['kind']=='mixed':
            from audio.analysis_runtime import require_runtime
            from audio.mixed_analysis import PIPELINE_VERSION, INPUT_MIMES
            if spec['pipeline_version']!=PIPELINE_VERSION:
                raise ValueError('The analysis pipeline changed. Start a new preview.')
            runtime=require_runtime(cancel=cancelled)
            if runtime['manifest']['runtime_version']!=spec['runtime_version']:
                raise ValueError('The analysis runtime changed. Start a new preview.')
            source=library.get(spec['asset_id']);path=library.media/source['sha256']
            if source['kind']!='original' or source['mime'] not in INPUT_MIMES or source['sha256']!=spec['sha256']:
                raise ValueError('Choose the supported original audio recording again.')
            if not library.verify(source['id']): raise ValueError('Source verification failed. The original was not changed.')
            library.space(256*1024**2)
            def analysis_progress(event):
                value=event.get('progress')
                if type(value) in (int,float) and math.isfinite(value) and 0<=value<=1:
                    library.update_job(identity,'running',value*.65)
            analysis_request={'source_path':str(path.resolve()),'source_sha256':source['sha256'],'source_id':source['source_id'],
                 'start_seconds':spec['start_seconds'],'end_seconds':spec['end_seconds'],
                 'modeldir':str(Path(runtime['modeldir']).resolve()),'result_path':str(analysis.resolve())}
            if selection: analysis_request['required_roles']=list(required_roles(selection,performers=spec['performers']))
            response=run_worker('audio.mixed_analysis', analysis_request,
                python=runtime['python'],cancel=cancelled,progress=analysis_progress,
                wall_seconds=min(180,max(.001,240-(time.monotonic()-started))),memory_bytes=2*1024**3)
            if cancelled(): raise InterruptedError('Cancelled')
            performance,report,origin=load_analysis(analysis,response['result'],source['source_id'],spec['start_seconds'],spec['end_seconds'],
                                                     roles=required_roles(selection,performers=spec['performers']))
            analysis_metrics=response['metrics']
            report['pipeline_version']=spec['pipeline_version'];report['runtime_version']=spec['runtime_version']
            source_id=source['source_id'];kind='mixed-preview-'+str(spec['performers'])
            filename=source['title']+' - mixed melody and bass '+('solo' if spec['performers']==1 else 'duet')+'.wav'
            notice='Experimental mixed melody and bass rendered from guitar samples. No source vocals or backing audio in this preview. No chord or phrasing recovery; fidelity and human playability are not accepted or guaranteed.'
        elif spec['kind']=='extract':
            from audio.extract import extract_wav
            source=library.get(spec['asset_id']);path=library.media/source['sha256']
            with path.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest()!=spec['sha256']: raise ValueError('Source verification failed')
            with wave.open(str(path),'rb') as wav: sr=wav.getframerate()
            origin=round(spec['start_seconds']*sr)
            performance,report=extract_wav(path,source['source_id'],origin,round(spec['end_seconds']*sr),
                cancel=cancelled,progress=lambda p:library.update_job(identity,'running',p*.65))
            source_id=source['source_id'];kind='melody-preview-'+str(spec['performers'])
            filename=source['title']+' - extracted melody.wav'
            notice='Experimental isolated-melody extraction only. No harmony, source vocals, or full-song separation. A second guitarist has no accompaniment to play in this preview.'
        elif spec['kind']=='diagnostic':
            sr=44100;origin=0
            melody=[64,67,69,67,64,62,64,59]
            notes=[Note(f'm{i}',int(i*.43*sr),int((i*.43+.36)*sr),pitch,92,'melody') for i,pitch in enumerate(melody)]
            notes += [Note(f'b{i}',int(i*.86*sr),int((i*.86+.7)*sr),pitch,72,'bass') for i,pitch in enumerate([40,45,43,47])]
            performance=Performance('self-created-diagnostic-v1',sr,notes)
            source_id=performance.source_id;kind='diagnostic-'+str(spec['performers'])
            filename=f"Known phrase - {'solo' if spec['performers']==1 else 'duet'}.wav"
            notice='Self-created known-note diagnostic. Not a conversion of your song.'
        else:
            raise ValueError('Unsupported preview job type.')
        if spec['kind']=='mixed':
            arrangement=arrange_selected(performance,spec['performers'],spec.get('guidance'),selection,render_origin_sample=origin)
            if instrument=='jazz-bass':
                kind='bass-preview-'+str(spec['performers'])+'-'+selection['bass_part']
                filename=source['title']+' - Jazz Bass '+selection['bass_part']+'.wav'
                notice='Experimental sampled Jazz Bass preview following estimated vocal melody and/or bass. No source audio is mixed in. Source timing is retained; octave changes and omissions are listed. Moderate sampled dynamics only; chords, phrasing, fingering and musical fidelity remain unaccepted.'
            elif instrument=='electric':
                kind='electric-preview-'+str(spec['performers'])
                filename=source['title']+' - dry Electric '+('solo' if spec['performers']==1 else 'duet')+'.wav'
                notice='Experimental dry Electric preview following estimated melody and bass. Source timing is retained; octave changes and omissions are listed. No source audio, amplifier, reverb, or added sustain is mixed in. Fixed sample variation and uncalibrated dynamics; instrumental identity, phrasing and musical fidelity remain unaccepted.'
            elif spec.get('guidance',{}).get('accompaniment')=='melody-only':
                kind='mixed-preview-1-melody-only'
                filename=source['title']+' - estimated melody only.wav'
                notice+=' Estimated bass is omitted at your request.'
        elif spec['kind'] not in ('refine','instrumental-arrange'):
            arrangement=arrange(performance,spec['performers'])
        if cancelled():raise InterruptedError('Cancelled')
        if instrument=='electric':
            players=sorted({n['player'] for n in arrangement['notes']})
            valid_origin=type(arrangement.get('render_origin_sample')) is int and arrangement['render_origin_sample']==origin
            valid_players=arrangement.get('performers')==spec['performers'] and players==list(range(1,spec['performers']+1))
            if arrangement.get('renderable') is not True or not valid_origin or not valid_players:
                error=('The Electric arrangement source passage changed. Start a new preview.' if not valid_origin else
                       'These notes could not be assigned to the requested Electric players. Review the omissions or choose another passage.')
                refusal={'version':1,'stage':'arrangement','status':'violates',
                    'expected':{'renderable':True,'render_origin_sample':origin,'performers':spec['performers'],
                                'players':list(range(1,spec['performers']+1))},
                    'observed':{'renderable':arrangement.get('renderable'),
                                'render_origin_sample':arrangement.get('render_origin_sample'),
                                'performers':arrangement.get('performers'),'players':players,
                                'issues':arrangement.get('issues',[]),
                                'omitted_note_ids':[a['note_id'] for a in arrangement.get('adaptations',[]) if a.get('kind')=='omission']},
                    'source_clock':{'sample_rate':44100,'origin_sample':origin,'end_sample':round(spec['end_seconds']*44100)},
                    'units':{'render_origin_sample':'absolute source samples','sample_rate':'Hz','performers':'players'},
                    'scope':'Planner constraints and source-clock consistency only; no musical judgment. No rendering occurred.',
                    'next_discriminating_check':('Compare the requested passage origin with the origin supplied before planning; do not retime retained notes.' if not valid_origin else
                        'Inspect the retained omission and issue records against source gates, available strings and requested players before choosing a different passage or player count.')}
                library.update_job(identity,'failed',result={'arrangement':arrangement,'analysis':report,'refusal':refusal},error=error)
                return
        if spec['kind'] in ('mixed','refine') and spec['performers']==2:
            players={n['player'] for n in arrangement['notes']}
            if players!={1,2}:
                raise ValueError('This passage did not yield both melody and bass for two guitarists. Try a different passage or one guitarist.')
        if instrument=='electric':
            if spec['kind']!='mixed':
                notice+=' Dry Electric samples only: no amplifier, reverb, added sustain or source waveform; dynamics and musical fidelity remain unaccepted.'
        else:
            arrangement['render_origin_sample']=origin
        renderer={'acoustic':render,'jazz-bass':render_bass,'electric':render_electric}[instrument]
        metrics=renderer(arrangement,output,cancel=cancelled,progress=lambda p:library.update_job(identity,'running',.65+p*.35 if report else p))
        if cancelled(): raise InterruptedError('Cancelled')
        evidence=None
        if instrument=='electric':
            # The private sidecar retains the underlying adapter's prototype
            # receipt. Wrapper metrics are a different schema and never replace it.
            evidence=RenderEvidence(library).publish(output.with_suffix('.wav.dry.f32'),
                output.with_suffix('.wav.receipt.json'),wav_path=output,arrangement=arrangement,cancel=cancelled)
            if cancelled(): raise InterruptedError('Cancelled')
        item=library.publish(output,filename,'audio/wav',kind=kind,source_id=source_id)
        metrics['seconds']=time.monotonic()-started
        metrics['uses_gpu']=False
        metrics['published_bytes']=item['size']
        if analysis_metrics is not None: metrics['analysis_process']=analysis_metrics
        if os.name=='nt':
            class Counters(ctypes.Structure):
                _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            ctypes.windll.kernel32.GetCurrentProcess.restype=ctypes.c_void_p
            ctypes.windll.psapi.GetProcessMemoryInfo.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
                metrics['peak_working_set_bytes']=counters.PeakWorkingSetSize
        result={'asset':item,'arrangement':arrangement,'metrics':metrics,'notice':notice,'analysis':report}
        if evidence is not None:result['render_evidence']=evidence
        library.update_job(identity,'complete',1,result=result)
    except (InterruptedError,Cancelled):
        library.update_job(identity,'cancelled',error='Preview cancelled. Your original files are unchanged.')
    except (ValueError,wave.Error) as e:
        library.update_job(identity,'failed',error=str(e))
    except ManagedProcessError:
        library.update_job(identity,'failed',error='The bounded analysis stopped or could not finish. Your original is unchanged. Try a shorter supported audio passage.')
    except Exception as e:
        (library.stage/(identity+'.error.txt')).write_text(traceback.format_exc(),encoding='utf-8')
        library.update_job(identity,'failed',error=f'Preview failed: {type(e).__name__}. Local error details saved.')
    finally:
        attention=[]
        for path in (output,cancel,analysis,analysis_partial,decoded,decoded_partial,
                     output.with_suffix('.wav.partial'),output.with_suffix('.wav.float.partial'),
                     output.with_suffix('.wav.dry.f32'),output.with_suffix('.wav.receipt.json'),
                     output.with_suffix('.wav.receipt.partial')):
            try:path.unlink(missing_ok=True)
            except (OSError,ValueError):attention.append({'operation':'remove-job-staging','file':path.name})
        from analysis_evidence import cleanup_work,reconcile_evidence
        maintenance=[('analysis-work',lambda:cleanup_work(library,identity)),
                     ('render-evidence',lambda:reconcile_render_evidence(library))]
        if job['request']['kind']=='instrumental':maintenance.append(('analysis-evidence',lambda:reconcile_evidence(library)))
        # run() is called while the outer entry point owns worker.lock. Strict
        # evidence maintenance must fail closed, never replace a completed result
        # or broaden deletion after finding a malformed saved reference.
        for name,action in maintenance:
            try:action()
            except (ValueError,OSError,sqlite3.Error) as error:
                attention.append({'operation':name,'error_type':type(error).__name__})
        if attention:
            try:
                with (library.root/(identity+'.cleanup-attention.json')).open('x',encoding='utf-8') as stream:
                    json.dump({'job_id':identity,'attention':attention},stream,sort_keys=True)
            except OSError:pass

if __name__=='__main__':
    gone=threading.Event()
    finished=threading.Event()
    def watch_parent():
        try:
            if parent_pipe_lost(finished): gone.set()
        except Exception: gone.set()
    watcher=threading.Thread(target=watch_parent,daemon=True)
    watcher.start()
    try:
        with Library(Path(sys.argv[1])).exclusive('worker.lock'):
            run(Path(sys.argv[1]),sys.argv[2],gone.is_set)
    finally:
        finished.set()
        watcher.join(timeout=.2)
