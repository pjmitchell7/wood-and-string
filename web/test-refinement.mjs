import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {refinementDraft,mountRefinement,pitchLabel,editablePerformance,restoreSavedPreview} from './refinement.mjs';
import {originalPassage} from './mixed-preview.mjs';
const note=(id,start,pitch=60,role='melody')=>({id,start,end:start+10,pitch,role});
const performance={sample_rate:100,notes:[note('a',100),note('b',120),note('c',140,62),note('d',160,48,'bass')]};
const job={id:'parent',state:'complete',request:{kind:'mixed',start_seconds:1,end_seconds:2,performers:1,asset_id:'song',sha256:'hash'},result:{arrangement:{source:performance}}};
assert.equal(pitchLabel(60),'C4');assert.equal(pitchLabel(48),'C3');
let draft=refinementDraft(job);draft.add('join',['b','a']);
assert.deepEqual(draft.request(),{parent_job_id:'parent',operations:[{kind:'join',note_ids:['a','b']}]});
assert.throws(()=>draft.add('omit',['a']),/earlier edit/);draft.remove(0);draft.add('omit',['a']);
assert.equal(draft.request().operations[0].kind,'omit');
draft=refinementDraft(job);assert.throws(()=>draft.add('join',['a','c']),/same pitch/);assert.throws(()=>draft.add('join',['a']),/at least two/);
const intervening=structuredClone(job);intervening.result.arrangement.source.notes[2].pitch=60;
assert.throws(()=>refinementDraft(intervening).add('join',['a','c']),/intervening/);
const overlap=structuredClone(job);overlap.result.arrangement.source.notes[0].end=125;
assert.throws(()=>refinementDraft(overlap).add('join',['a','b']),/Overlapping/);
const crossing=structuredClone(job);crossing.result.arrangement.source.notes.push({...note('crossing',90),end:105});
assert.throws(()=>refinementDraft(crossing).add('join',['a','b']),/intervening/);
draft=refinementDraft(job);draft.add('omit',['a','b','c','d']);assert.throws(()=>draft.request(),/at least one/);
const duet=structuredClone(job);duet.request.performers=2;draft=refinementDraft(duet);draft.add('omit',['d']);assert.throws(()=>draft.request(),/both melody and bass/);
draft=refinementDraft(job);draft.add('omit',['a','b','c']);assert.equal(draft.request().operations.length,1);
const guided=structuredClone(job);guided.request.guidance={accompaniment:'melody-only'};draft=refinementDraft(guided);draft.add('omit',['a','b','c']);assert.throws(()=>draft.request(),/melody-only/);
// Bass solo must retain its chosen role, even when the other role remains.
const bassLine=structuredClone(job);bassLine.request.instrument='jazz-bass';bassLine.request.bass_part='bass-line';
draft=refinementDraft(bassLine);draft.add('omit',['d']);assert.throws(()=>draft.request(),/estimated bass line/);
draft=refinementDraft(bassLine);draft.add('omit',['a','b','c']);assert.equal(draft.request().operations.length,1);
const bassMelody=structuredClone(bassLine);bassMelody.request.bass_part='melody';
draft=refinementDraft(bassMelody);draft.add('omit',['a','b','c']);assert.throws(()=>draft.request(),/estimated melody/);
const bassDuo=structuredClone(bassLine);bassDuo.request.performers=2;bassDuo.request.bass_part='melody-and-bass';
draft=refinementDraft(bassDuo);draft.add('omit',['d']);assert.throws(()=>draft.request(),/both melody and bass/);
const refined=structuredClone(job);refined.request.kind='refine';refined.result.arrangement.refinement_source={sample_rate:100,notes:[note('joined',100)]};
assert.equal(editablePerformance(refined).notes[0].id,'joined');assert.equal(refined.result.arrangement.source.notes.length,4);
assert.equal(originalPassage(refined,[{id:'song',variant:'original',sha256:'hash',name:'Song'}]).start,1);

// Exercise actual mounted handlers and asynchronous request lifecycle with a tiny DOM.
class Node {constructor(tag){this.tag=tag;this.children=[];this.style={};this.disabled=false;this.textContent=''}append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=nodes}setAttribute(){}set innerHTML(value){throw Error('Unsafe HTML rendering')} }
const document={createElement:tag=>new Node(tag)},root=new Node('root');
let calls=[],resolve;
mountRefinement({job,root,document,submit:request=>{calls.push(request);return new Promise(r=>resolve=r)}});
const all=node=>[node,...node.children.flatMap(all)];
const bassRoot=new Node('root');mountRefinement({job:bassLine,root:bassRoot,document,submit:()=>{throw Error('Unexpected submit')}});
assert.ok(all(bassRoot).some(n=>n.textContent.includes('fitted to the Jazz Bass players again')));
assert.ok(all(bassRoot).some(n=>n.textContent.includes('follow the estimated bass line. The other part remains visible')));
const button=text=>all(root).find(n=>n.tag==='button'&&n.textContent===text);
let boxes=all(root).filter(n=>n.tag==='input');
for(const box of boxes.slice(0,2)){box.checked=true;box.onchange()}
button('Join selected fragments').onclick();assert.equal(boxes[0].disabled,true);
button('Remove edit').onclick();assert.equal(boxes[0].disabled,false);
for(const box of boxes.slice(0,2)){box.checked=true;box.onchange()}
button('Join selected fragments').onclick();
const save=button('Make refined preview'),flight=save.onclick();await save.onclick();
assert.equal(calls.length,1);assert.equal(save.disabled,true);assert.equal(boxes[2].disabled,true);
resolve();await flight;assert.equal(save.disabled,false);assert.equal(boxes[2].disabled,false);
assert.deepEqual(calls[0],{parent_job_id:'parent',operations:[{kind:'join',note_ids:['a','b']}]});
assert.ok(all(root).some(n=>n.textContent.includes('0.00–0.10 s')));
assert.deepEqual(job.result.arrangement.source,performance);
const receiptJob=structuredClone(refined),markup='<img src=x onerror=bad()>',receiptRoot=new Node('root');
receiptJob.result.arrangement.refinement={edits:[{kind:'join',members:[note(markup,100),note('private-id',120)],result_note_id:markup,unobserved_gaps:[{start:110,end:120}]},{kind:'omit',members:[note('hidden-bass-id',160,48,'bass')]}]};
const mounted=mountRefinement({job:receiptJob,root:receiptRoot,document,submit:async()=>{throw Error(markup)}});
assert.ok(all(receiptRoot).some(n=>n.textContent==='Your edits'));
assert.ok(all(receiptRoot).some(n=>n.textContent.includes('Joined 2 melody fragments at C4, 0.00–0.30 s; filled 0.10 s of gaps by your choice')));
assert.ok(all(receiptRoot).some(n=>n.textContent.includes('Left out 1 note by your choice: bass · C3 · 0.60–0.70 s')));
assert.ok(!all(receiptRoot).some(n=>n.textContent.includes('private-id')||n.textContent.includes(markup)));
// A failed request retains its exact operations and exposes an error as text only.
const failureRoot=new Node('root');
const failure=mountRefinement({job,root:failureRoot,document,submit:async()=>{throw Error(markup)}});
const failureBoxes=all(failureRoot).filter(n=>n.tag==='input');failureBoxes[3].checked=true;failureBoxes[3].onchange();
all(failureRoot).find(n=>n.textContent==='Leave selected notes out').onclick();
assert.ok(all(failureRoot).some(n=>n.textContent==='Leave out: bass · C3 · 0.60–0.70 s '));
const failedSave=all(failureRoot).find(n=>n.textContent==='Make refined preview');await failedSave.onclick();
assert.equal(failedSave.disabled,false);assert.equal(failure.draft.operations.length,1);
assert.deepEqual(failure.draft.request(),{parent_job_id:'parent',operations:[{kind:'omit',note_ids:['d']}]});
assert.ok(all(failureRoot).some(n=>n.textContent===markup));
const saved={...job,result:{...job.result,asset:{id:'render'}}};
let loads=[],shown=[];
const restore=options=>restoreSavedPreview({savedId:null,load:async url=>{loads.push(url);return saved},isPending:()=>false,show:async value=>shown.push(value),...options});
await restore({});assert.deepEqual(loads,['/api/previews/latest']);assert.equal(shown[0],saved);
loads=[];shown=[];await restore({savedId:'saved/id'});assert.deepEqual(loads,['/api/jobs/saved%2Fid']);assert.equal(shown.length,1);
loads=[];shown=[];await restore({savedId:'stale',load:async url=>{loads.push(url);return url.includes('/jobs/')?{state:'failed'}:saved}});assert.deepEqual(loads,['/api/jobs/stale','/api/previews/latest']);assert.equal(shown.length,1);
loads=[];shown=[];await restore({savedId:'missing',load:async url=>{loads.push(url);if(url.includes('/jobs/'))throw Error('404');return saved}});assert.equal(shown.length,1);
shown=[];await restore({savedId:'offline',load:async()=>{throw Error('offline')}});assert.equal(shown.length,0);
loads=[];await restore({isPending:()=>true});assert.equal(loads.length,0);
let pending=false;shown=[];loads=[];
await restore({savedId:'old',isPending:()=>pending,load:async url=>{loads.push(url);pending=true;return saved}});assert.equal(shown.length,0);assert.equal(loads.length,1);
pending=false;await restore({isPending:()=>pending,load:async()=>{pending=true;return saved}});assert.equal(shown.length,0);
// Execute the real app startup branch, not a duplicated approximation.
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const startup=app.slice(app.indexOf("if(localStorage.getItem('activeJob'))"),app.indexOf("\n",app.indexOf('else {restoreSavedPreview')));
let startupFlight,polls=0;loads=[];shown=[];
const context={localStorage:{getItem:()=>null},activeJob:null,activeJobKind:null,refinementStarting:false,$:()=>({disabled:false}),
  apiJSON:async url=>{loads.push(url);return saved},showJob:async value=>shown.push(value),pollJob:()=>polls++,
  restoreSavedPreview:options=>(startupFlight=restoreSavedPreview(options))};
vm.runInNewContext(startup,context);await startupFlight;assert.deepEqual(loads,['/api/previews/latest']);assert.equal(shown.length,1);
context.localStorage={getItem:key=>key==='activeJob'?'pending-job':'mixed'};loads=[];vm.runInNewContext(startup,context);
assert.equal(polls,1);assert.equal(context.activeJob,'pending-job');assert.equal(loads.length,0);
console.log('PASS refinement: current source, join boundaries, reservations/removal, omission guards, real mounted submit single-flight, source-relative labels, comparison identity; no playback.');
