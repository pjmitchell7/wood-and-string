// Explicit edits to estimated events. No playback, inference, or implicit smoothing.
export async function restoreSavedPreview({savedId,load,isPending,show}){
  const usable=job=>job?.state==='complete'&&['mixed','refine'].includes(job.request?.kind)&&job.result?.asset&&job.result?.arrangement;
  if(isPending())return;
  if(savedId){
    try{const job=await load(`/api/jobs/${encodeURIComponent(savedId)}`);if(isPending())return;if(usable(job)){await show(job);return}}
    catch{if(isPending())return}
  }
  if(isPending())return;
  try{const job=await load('/api/previews/latest');if(!isPending()&&usable(job))await show(job)}catch{/* Empty history or offline does not interrupt the player. */}
}
export function editablePerformance(job){
  const arrangement=job?.result?.arrangement;
  const performance=arrangement?.refinement_source??arrangement?.source;
  if(job?.state!=='complete'||!['mixed','refine'].includes(job.request?.kind)||!performance?.notes?.length||!(performance.sample_rate>0))throw Error('This saved preview has no editable note estimates.');
  return performance;
}
export function pitchLabel(pitch){
  if(!Number.isInteger(pitch)||pitch<0||pitch>127)throw Error('Invalid estimated pitch.');
  return `${['C','C♯','D','E♭','E','F','F♯','G','A♭','A','B♭','B'][pitch%12]}${Math.floor(pitch/12)-1}`;
}
export function refinementDraft(job){
  const performance=editablePerformance(job),notes=performance.notes;
  const byId=new Map(notes.map(n=>[n.id,n]));
  let operations=[];
  const reserved=()=>new Set(operations.flatMap(o=>o.note_ids));
  function add(kind,ids){
    const unique=[...new Set(ids)];
    if(!['join','omit'].includes(kind)||!unique.length)throw Error('Select notes first.');
    if(operations.length>=128)throw Error('Use at most 128 edits in one preview.');
    if(unique.some(id=>!byId.has(id)))throw Error('A selected note is no longer available.');
    if(unique.some(id=>reserved().has(id)))throw Error('Remove the earlier edit before reusing its notes.');
    const selected=unique.map(id=>byId.get(id)).sort((a,b)=>a.start-b.start||a.end-b.end||a.id.localeCompare(b.id));
    if(kind==='join'){
      if(selected.length<2)throw Error('Select at least two fragments to join.');
      if(selected.some(n=>n.pitch!==selected[0].pitch||n.role!==selected[0].role))throw Error('Join fragments with the same pitch and the same melody or bass role.');
      if(selected.some((n,i)=>i>0&&n.start<selected[i-1].end))throw Error('Overlapping notes cannot be joined. Select separate fragments.');
      const first=selected[0].start,last=selected.at(-1).end;
      if(notes.some(n=>n.role===selected[0].role&&n.start<last&&n.end>first&&!unique.includes(n.id)))throw Error('Include every intervening note of this role, or choose a shorter join.');
    }
    operations.push({kind,note_ids:selected.map(n=>n.id)});
  }
  function request(){
    if(!operations.length)throw Error('Queue at least one edit.');
    const omitted=new Set(operations.filter(o=>o.kind==='omit').flatMap(o=>o.note_ids));
    const remaining=notes.filter(n=>!omitted.has(n.id));
    if(!remaining.length)throw Error('Keep at least one note in the passage.');
    if(job.request.performers===2&&!['melody','bass'].every(role=>remaining.some(n=>n.role===role)))throw Error('Two performers need both melody and bass notes. Keep notes for each performer.');
    if(job.request.instrument==='jazz-bass'&&job.request.performers===1){
      const role=job.request.bass_part==='bass-line'?'bass':'melody';
      if(!remaining.some(n=>n.role===role))throw Error(`This Jazz Bass preview follows the estimated ${role==='bass'?'bass line':'melody'}. Keep at least one note in that part.`);
    }
    if(job.request.instrument==='electric'&&!remaining.some(n=>n.role==='melody'))throw Error('This Electric preview needs at least one estimated melody note.');
    if(job.request.guidance?.accompaniment==='melody-only'&&!remaining.some(n=>n.role==='melody'))throw Error('This melody-only preview needs at least one melody note.');
    return {parent_job_id:job.id,operations:operations.map(o=>({...o,note_ids:[...o.note_ids]}))};
  }
  return {notes, sampleRate:performance.sample_rate, reserved,
    get operations(){return operations.map(o=>({...o,note_ids:[...o.note_ids]}))},add,
    remove(index){operations.splice(index,1)},request};
}

export function mountRefinement({job,root,submit,document:doc=globalThis.document}){
  const draft=refinementDraft(job),selected=new Set();
  const element=(tag,text)=>{const node=doc.createElement(tag);if(text!==undefined)node.textContent=text;return node};
  const origin=Math.round(job.request.start_seconds*draft.sampleRate);
  const seconds=sample=>((sample-origin)/draft.sampleRate).toFixed(2);
  const role=note=>note.role==='bass'?'bass':'melody';
  const describe=note=>`${role(note)} · ${pitchLabel(note.pitch)} · ${seconds(note.start)}–${seconds(note.end)} s`;
  const edits=job.result.arrangement.refinement?.edits;
  if(edits?.length){
    const receipt=element('details'),items=element('ul');receipt.append(element('summary','Your edits'));
    receipt.append(element('p','These are your explicit edits to the estimates, not automatically detected musical corrections. Times are relative to the passage start.'));
    for(const edit of edits){
      const members=edit.members||[];if(!members.length)continue;
      let text;
      if(edit.kind==='join'){
        const start=Math.min(...members.map(n=>n.start)),end=Math.max(...members.map(n=>n.end));
        const gaps=(edit.unobserved_gaps||[]).reduce((sum,gap)=>sum+gap.end-gap.start,0)/draft.sampleRate;
        text=`Joined ${members.length} ${role(members[0])} fragments at ${pitchLabel(members[0].pitch)}, ${seconds(start)}–${seconds(end)} s; filled ${gaps.toFixed(2)} s of gaps by your choice. This asserts a held note across those gaps.`;
      }else text=`Left out ${members.length} note${members.length===1?'':'s'} by your choice: ${members.map(describe).join('; ')}.`;
      items.append(element('li',text));
    }
    receipt.append(items);root.append(receipt);
  }
  const panel=element('details'),heading=element('summary','Refine this passage');panel.append(heading);
  panel.append(element('p','Select estimated fragments to join into one held note, or leave notes out. Joining replaces repeated attacks and fills the gap between them; use it only where you intend a sustained note. Your original and previous preview stay unchanged.'));
  panel.append(element('p',`The whole passage is fitted to the ${job.request.instrument==='jazz-bass'?'Jazz Bass players':job.request.instrument==='electric'?'Electric guitar players':'acoustic guitar players'} again. Even unedited notes may move by an octave or be left out. Review the arrangement changes before keeping the revision.`));
  panel.append(element('p','Times are seconds from the start of this passage. These are estimated pitches, not a score verified by a musician. No pitch or timing adjustments are available here.'));
  if(job.request.guidance?.accompaniment==='melody-only')panel.append(element('p','This preview keeps your melody-only choice. Bass estimates remain visible as evidence but will not be rendered.'));
  if(job.request.instrument==='jazz-bass')panel.append(element('p',job.request.performers===2?'This experimental Jazz Bass preview keeps upper melody and lower bass foundation as separate parts.':`This experimental Jazz Bass preview keeps your choice to follow the estimated ${job.request.bass_part==='bass-line'?'bass line':'melody'}. The other part remains visible as evidence but will not be rendered.`));
  if(job.request.instrument==='electric')panel.append(element('p','This experimental preview uses dry Electric samples, with no added amplifier, reverb or sustain. The selected instrument does not identify the source guitar; estimated notes and musical fidelity still need review.'));
  const list=element('div');list.style.maxHeight='20rem';list.style.overflowY='auto';
  const boxes=[];
  for(const note of [...draft.notes].sort((a,b)=>a.start-b.start||a.id.localeCompare(b.id))){
    const label=element('label'),box=element('input');box.type='checkbox';
    label.style.display='flex';label.style.alignItems='start';label.style.gap='.5rem';label.style.padding='.5rem 0';
    box.onchange=()=>{if(box.checked)selected.add(note.id);else selected.delete(note.id)};
    const text=element('span',describe(note));
    label.append(box,text);list.append(label);boxes.push([box,note.id]);
  }
  const pending=element('ol'),message=element('p');message.setAttribute('role','status');message.setAttribute('aria-live','polite');
  const controls=[];let busy=false;
  const button=(text,fn)=>{const b=element('button',text);b.type='button';b.onclick=fn;controls.push(b);return b};
  function repaint(){
    pending.replaceChildren();
    draft.operations.forEach((op,index)=>{const descriptions=op.note_ids.map(id=>describe(draft.notes.find(n=>n.id===id)));const li=element('li',`${op.kind==='join'?'Join':'Leave out'}: ${descriptions.join('; ')} `),remove=element('button','Remove edit');remove.type='button';remove.onclick=()=>{if(!busy){draft.remove(index);repaint()}};remove.disabled=busy;li.append(remove);pending.append(li)});
    const reserved=draft.reserved();for(const [box,id]of boxes){box.disabled=busy||reserved.has(id);box.checked=selected.has(id)&&!reserved.has(id)}
    save.disabled=busy||!draft.operations.length;
  }
  function queue(kind){try{draft.add(kind,[...selected]);selected.clear();message.textContent='Edit queued. Review it below before making a new preview.';repaint()}catch(e){message.textContent=e.message}}
  const join=button('Join selected fragments',()=>queue('join')),omit=button('Leave selected notes out',()=>queue('omit'));
  const save=button('Make refined preview',async()=>{
    if(busy)return;
    try{
      const request=draft.request();busy=true;for(const b of controls)b.disabled=true;repaint();message.textContent='Starting a new preview from these edits…';
      await submit(request);
    }catch(e){message.textContent=e.message}
    finally{busy=false;for(const b of controls)b.disabled=false;repaint()}
  });
  panel.append(list,join,omit,element('h3','Queued edits'),pending,save,message);root.append(panel);repaint();
  return {draft,panel};
}
