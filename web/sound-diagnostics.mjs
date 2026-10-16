// Descriptive saved evidence only. This module never requests or plays audio.
const list=(value,limit=4000)=>Array.isArray(value)&&value.length<=limit?value.filter(row=>row&&typeof row==='object'&&!Array.isArray(row)):[];
const text=value=>typeof value==='string'?value:'';
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const seconds=(frames,rate)=>finite(frames)&&frames>=0&&Number.isSafeInteger(rate)&&rate>0&&finite(frames/rate)?`${(frames/rate).toFixed(3)} s`:null;
const gate=(note,rate)=>Number.isSafeInteger(note?.start)&&Number.isSafeInteger(note?.end)&&note.start>=0&&note.end>note.start&&Number.isSafeInteger(rate)&&rate>0
  ?`source samples [${note.start}, ${note.end}) at ${rate} Hz; requested gate ${seconds(note.end-note.start,rate)}`:null;

export function soundDiagnosticSections(job){
  const result=job?.result,sections=[];
  if(job?.state==='failed'&&job.request?.instrument==='electric'&&result?.refusal?.stage==='arrangement'&&!result.asset){
    const refusal=result.refusal,rows=[],seen=new Set();
    const add=value=>{if(value&&!seen.has(value)){seen.add(value);rows.push(value)}};
    const expected=refusal.expected||{},observed=refusal.observed||{};
    const clock=refusal.source_clock;
    if(Number.isSafeInteger(clock?.origin_sample)&&Number.isSafeInteger(clock?.end_sample)&&clock.origin_sample>=0&&clock.end_sample>clock.origin_sample&&Number.isSafeInteger(clock.sample_rate)&&clock.sample_rate>0)
      add(`Requested passage: source samples [${clock.origin_sample}, ${clock.end_sample}) at ${clock.sample_rate} Hz.`);
    if(Number.isSafeInteger(expected.render_origin_sample)&&Number.isSafeInteger(observed.render_origin_sample)&&expected.render_origin_sample!==observed.render_origin_sample)
      add(`Passage start: expected source sample ${expected.render_origin_sample}; received ${observed.render_origin_sample}.`);
    if(Number.isInteger(expected.performers)&&Array.isArray(observed.players)&&observed.players.length<=2&&observed.players.every(Number.isSafeInteger))
      add(`Requested ${expected.performers} player${expected.performers===1?'':'s'}; assigned parts to ${observed.players.length}.`);
    // One issue per source note plus two empty-player issues; up to an
    // omission/octave change and a contour disclosure for each of 4,000 notes.
    for(const issue of list(observed.issues,4002))if(text(issue.reason))add(`${text(issue.note_id)?`Note ${issue.note_id}: `:Number.isInteger(issue.player)?`Player ${issue.player}: `:''}${text(issue.reason)}`);
    for(const change of list(result.arrangement?.adaptations,8000))if(change.kind==='omission')
      add(`Note ${text(change.note_id)}: ${text(change.reason)||'No available assignment was found.'}`);
    sections.push({title:'Why this preview stopped',
      summary:'No audio was created. The original, estimated notes and assignment reasons are preserved. This checks whether the notes fit the chosen players; it does not judge the song or transcription.',rows});
    if(text(refusal.next_discriminating_check))sections.push({title:'Next diagnostic check',summary:refusal.next_discriminating_check,rows:[]});
  }
  if(job?.state==='complete'&&job.request?.instrument==='electric'){
    const notes=new Map(list(result?.arrangement?.notes).filter(note=>text(note.id)).map(note=>[note.id,note]));
    const rate=result?.arrangement?.sample_rate,rows=[];
    for(const limit of list(result?.metrics?.sample_limits))if(limit.gate_exceeds_stored_sample_estimate===true){
      const note=notes.get(limit.note_id),requested=gate(note,rate);
      const stored=seconds(limit.transposed_stored_frames_estimate,rate);
      const excess=requested&&stored?seconds(note.end-note.start-limit.transposed_stored_frames_estimate,rate):null;
      rows.push(`Note ${text(limit.note_id)||'(unknown ID)'}${requested?`: ${requested}`:''}${stored?`; recorded length about ${stored} at this pitch`:''}${excess?`; hold exceeds that estimate by ${excess}`:''}.`);
    }
    if(rows.length)sections.push({title:'Long holds need review',
      summary:'The requested holds exceed the estimated lengths of their recorded guitar notes. This is a renderer capacity warning, not a measurement of audible sustain. Compare the dry sound, requested hold and original phrase before changing gates or adding sustain.',rows});
  }
  return sections;
}

export function mountSoundDiagnostics({job,root,document:doc=globalThis.document}){
  for(const section of soundDiagnosticSections(job)){
    const details=doc.createElement('details'),title=doc.createElement('summary'),description=doc.createElement('p');
    title.textContent=section.title;description.textContent=section.summary;details.append(title,description);
    if(section.rows.length){
      const items=doc.createElement('ul'),more=doc.createElement('button');let shown=0;
      more.type='button';
      const append=()=>{
        const end=Math.min(shown+100,section.rows.length);
        while(shown<end){const item=doc.createElement('li');item.textContent=section.rows[shown++];items.append(item)}
        more.hidden=shown===section.rows.length;
        more.textContent=`Show more notes (${shown} of ${section.rows.length} shown)`;
      };
      more.onclick=append;append();details.append(items,more);
    }
    root.append(details);
  }
}
