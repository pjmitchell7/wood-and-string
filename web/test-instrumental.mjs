import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {instrumentalRequest,instrumentalPassage,instrumentalChanges,candidateBundle,mountInstrumentalCandidates} from './instrumental.mjs';
import {electricToneForPreset} from './electric-tone.mjs';
const decoder=new TextDecoder('utf-8',{fatal:true});
for(const file of ['instrumental.mjs','test-instrumental.mjs','index.html','app.js','sw.js','test-sw-update.cjs'])decoder.decode(fs.readFileSync(new URL(file,import.meta.url)));
const track={id:'source',name:'Recording',variant:'original',url:'/api/media/source',sha256:'hash'};
assert.deepEqual(instrumentalRequest(track,2,12),{asset_id:'source',start_seconds:2,end_seconds:12});
for(const [start,end] of [[0,10.01],[-1,2],[2,2],[NaN,3]])assert.throws(()=>instrumentalRequest(track,start,end));
assert.throws(()=>instrumentalRequest({...track,variant:'render'},0,5));
const bundle={version:1,source_part:{id:'umx:other',instrument:'unknown'},sample_rate:44100,origin:88200,end:308700,
  interpretations:{published:{events:[{id:'p1',start:90000,end:95000,pitch:60},{id:'p2',start:96000,end:99000,pitch:64}]},onset_only:{events:[{id:'o1',start:91000,end:93000,pitch:62}]}}};
const job={id:'parent',state:'complete',request:{kind:'instrumental',asset_id:'source',sha256:'hash',start_seconds:2,end_seconds:7},result:{candidates:bundle}};
const selectedNotes=[64,67,69,60,62].map((pitch,i)=>({id:`published_default-${i}`,pitch,start:88200+18522+i*4410,end:120000+i*4410}));
const arranged={sample_rate:44100,render_origin_sample:88200,source:{sample_rate:44100,notes:selectedNotes},
  notes:selectedNotes.filter((_,i)=>i!==3).map((n,i)=>({...n,source_pitch:n.pitch,pitch:i<3?n.pitch-12:n.pitch})),
  adaptations:[...selectedNotes.slice(0,3).map(n=>({note_id:n.id,kind:'octave',semitones:-12})),
    {note_id:selectedNotes[3].id,kind:'omission',reason:'No supported placement'},
    ...Array.from({length:41},(_,i)=>({note_id:`published_default-unchecked-${i}`,kind:'candidate-not-selected'}))]};
const unchanged=structuredClone(arranged),readable=instrumentalChanges(arranged);
assert.match(readable,/5 notes selected; 4 included in this preview/);
assert.match(readable,/E4 at 0\.42 s moved one octave down/);
assert.match(readable,/C4 at 0\.72 s left out because.*playing limits/);
assert.match(readable,/41 unchecked candidates remain saved in the review/);
assert.doesNotMatch(readable,/published_default|unchecked-\d|source_pitch/);
assert.deepEqual(arranged,unchanged);
const acoustic=structuredClone(arranged);acoustic.notes=structuredClone(selectedNotes);acoustic.adaptations=acoustic.adaptations.filter(x=>x.kind==='candidate-not-selected');
assert.match(instrumentalChanges(acoustic),/5 notes selected; 5 included/);assert.doesNotMatch(instrumentalChanges(acoustic),/moved|left out/);
assert.equal(candidateBundle(job),bundle);assert.equal(instrumentalPassage(job,[track]).start,2);
assert.throws(()=>candidateBundle({...job,result:{candidates:{...bundle,origin:NaN}}}));
const malformed=structuredClone(job);malformed.result.candidates.interpretations.published.events[0].end=999999;assert.throws(()=>candidateBundle(malformed));
const capped=structuredClone(job);capped.result.candidates.interpretations.published.events=Array.from({length:4000},(_,i)=>({id:`p${i}`,start:90000,end:95000,pitch:60}));
assert.equal(candidateBundle(capped).interpretations.published.events.length,4000);
capped.result.candidates.interpretations.published.events.push({id:'p4000',start:90000,end:95000,pitch:60});assert.throws(()=>candidateBundle(capped));
class Node{constructor(tag){this.tag=tag;this.children=[];this.style={};this.textContent='';this.disabled=false;this.value=''}append(...n){this.children.push(...n)}replaceChildren(...n){this.children=n}setAttribute(){}set innerHTML(x){throw Error('Unsafe markup')}}
const document={createElement:tag=>new Node(tag)};
const paged=structuredClone(job);paged.result.candidates.interpretations.published.events=capped.result.candidates.interpretations.published.events.slice(0,101);
const pages=mountInstrumentalCandidates({job:paged,root:new Node('root'),document,submit:async()=>{}});
assert.equal(pages.boxes.length,100);pages.boxes[0].checked=true;pages.boxes[0].onchange();pages.nextPage.onclick();
assert.equal(pages.boxes.length,1);assert.equal(pages.boxes[0].checked,false);pages.boxes[0].checked=true;pages.boxes[0].onchange();pages.previousPage.onclick();
assert.equal(pages.boxes[0].checked,true);assert.deepEqual(pages.selected,['p0','p100']);assert.match(pages.pageStatus.textContent,/2 selected across all pages/);
pages.consent.checked=true;pages.policy.value='onset_only';pages.policy.onchange();assert.equal(pages.selected.length,0);assert.equal(pages.consent.checked,false);assert.equal(pages.previousPage.disabled,true);
let requests=[],resolve,current=true,available=false;
const root=new Node('root');const mounted=mountInstrumentalCandidates({job,root,document,isCurrent:()=>current,bassAvailability:()=>({ready:available,reason:'Bank unavailable'}),submit:request=>{requests.push(request);return new Promise(r=>resolve=r)}});
assert.equal(mounted.boxes.every(n=>!n.checked),true);assert.equal(mounted.consent.checked,false);assert.equal(mounted.save.disabled,true);
await mounted.save.onclick();assert.equal(requests.length,0);
mounted.boxes[0].checked=true;mounted.boxes[0].onchange();assert.equal(mounted.save.disabled,true);
mounted.consent.checked=true;mounted.consent.onchange();assert.equal(mounted.save.disabled,false);
mounted.policy.value='onset_only';mounted.policy.onchange();assert.equal(mounted.selected.length,0);assert.equal(mounted.consent.checked,false);
mounted.boxes[0].checked=true;mounted.boxes[0].onchange();mounted.consent.checked=true;mounted.consent.onchange();mounted.instrument.value='jazz-bass';await mounted.save.onclick();assert.equal(requests.length,0);assert.equal(mounted.message.textContent,'Bank unavailable');
mounted.instrument.value='acoustic';const flight=mounted.save.onclick();await mounted.save.onclick();assert.equal(requests.length,1);assert.equal(mounted.policy.disabled,true);
assert.deepEqual(requests[0],{parent_job_id:'parent',interpretation:'onset_only',note_ids:['o1'],instrument:'acoustic',accept_as_lead:true});
resolve();await flight;assert.equal(mounted.policy.disabled,false);current=false;await mounted.save.onclick();assert.equal(requests.length,1);
const failure=mountInstrumentalCandidates({job,root:new Node('root'),document,submit:async()=>{throw Error('<script>failure</script>')}});
failure.boxes[0].checked=true;failure.boxes[0].onchange();failure.consent.checked=true;await failure.save.onclick();
assert.equal(failure.message.textContent,'<script>failure</script>');assert.deepEqual(failure.selected,['p1']);assert.equal(failure.consent.checked,true);

for(const [enabled,availability]of [[false,{ready:true}],[true,{}],[true,{ready:false,reason:'Electric unavailable'}]]){
  let submitted=0;const controls=mountInstrumentalCandidates({job,root:new Node('root'),document,
    electricEnabled:enabled,electricAvailability:()=>availability,submit:async()=>submitted++});
  controls.boxes[0].checked=true;controls.boxes[0].onchange();controls.consent.checked=true;
  controls.instrument.value='electric';await controls.save.onclick();assert.equal(submitted,0);
  controls.tone.value='room';await controls.save.onclick();assert.equal(submitted,0);
  assert.equal(controls.instrument.children.find(n=>n.value==='electric').disabled,true);
  assert.match(controls.message.textContent,enabled?/not available|unavailable/:/still being tested/);
}
let electricRequest;
const electricControls=mountInstrumentalCandidates({job,root:new Node('root'),document,electricEnabled:true,
  electricAvailability:()=>({ready:true}),submit:async request=>{electricRequest=request}});
electricControls.boxes[0].checked=true;electricControls.boxes[0].onchange();electricControls.consent.checked=true;
electricControls.instrument.value='electric';await electricControls.save.onclick();
assert.deepEqual(electricRequest,{parent_job_id:'parent',interpretation:'published',note_ids:['p1'],instrument:'electric',accept_as_lead:true});
assert.equal(electricRequest.bass_part,undefined);assert.equal(electricControls.instrument.children.find(n=>n.value==='electric').disabled,false);
for(const preset of ['clean','room','delay']){
  electricControls.tone.value=preset;electricControls.instrument.onchange();
  assert.equal(electricControls.toneLabel.hidden,false);assert.equal(electricControls.tone.disabled,false);
  await electricControls.save.onclick();assert.deepEqual(electricRequest.electric_tone,electricToneForPreset(preset));
}
electricControls.instrument.value='acoustic';electricControls.instrument.onchange();
assert.equal(electricControls.toneLabel.hidden,true);assert.equal(electricControls.tone.disabled,true);
await electricControls.save.onclick();assert.equal(electricRequest.electric_tone,undefined);

// Execute actual app candidate completion/callback ownership without media.
const app=decoder.decode(fs.readFileSync(new URL('./app.js',import.meta.url)));
const code=app.slice(app.indexOf('let resultGeneration='),app.indexOf("$('cancelDiagnostic').onclick"));
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
const stored=new Map();let callbacks,apiResolve,refreshes=0,stops=0;
const c={$ ,activeJob:null,activeJobKind:null,pollTimer:null,tracks:[track],bassAvailability:{ready:false},
  ELECTRIC_PREVIEW_ENABLED:false,electricAvailability:{ready:false},
  localStorage:{setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
  jobMessage:text=>$('message').textContent=text,button:(text,fn)=>Object.assign(new Node('button'),{textContent:text,onclick:fn}),
  mountInstrumentalCandidates:options=>{callbacks=options},instrumentalPassage,instrumentalChanges,document,mountSoundDiagnostics(){},
  action:fn=>fn(),refresh:async()=>{refreshes++},comparison:{stop:()=>stops++,start:()=>{throw Error('Unexpected playback')}},
  setTimeout:()=>1,clearTimeout(){},apiJSON:()=>new Promise(r=>apiResolve=r),
  mixedCandidateSummary:()=>'',describeAdaptation:()=>'',play:()=>{throw Error('Unexpected playback')}};
vm.createContext(c);vm.runInContext(code,c);
await c.showJob(job);assert.equal(refreshes,0);assert.equal(c.activeJob,null);assert.equal(stored.get('latestInstrumentalJob'),'parent');assert.equal(stored.has('latestPreviewJob'),false);
assert.equal($('instrumentalSaved').hidden,false);assert.equal($('instrumentalSubmit').disabled,false);
const firstCallbacks=callbacks;
const request={parent_job_id:'parent',interpretation:'published',note_ids:['p1'],instrument:'acoustic',accept_as_lead:true};
const pending=firstCallbacks.submit(request);assert.equal(vm.runInContext('refinementStarting',c),true);
// A newer result invalidates this pending callback; its response cannot replace it.
await c.showJob({...job,id:'newer'});const before=stops;
apiResolve({id:'child',state:'queued',request:{kind:'instrumental-arrange'}});await pending;
assert.equal(stored.get('latestInstrumentalJob'),'newer');assert.equal(c.activeJob,null);assert.equal(stops,before);
await assert.rejects(firstCallbacks.submit(request),/no longer current/);
assert.equal(vm.runInContext('refinementStarting',c),false);
await c.showJob({id:'queued',state:'running',progress:.5,request:{kind:'instrumental'}});
assert.equal($('instrumentalSubmit').disabled,true);assert.equal($('mixedSubmit').disabled,true);assert.equal($('cancelDiagnostic').hidden,false);
await c.showJob({id:'queued',state:'cancelled',request:{kind:'instrumental'}});
assert.equal($('instrumentalSubmit').disabled,false);assert.equal($('cancelDiagnostic').hidden,true);
const outputJob={id:'output',state:'complete',request:{...job.request,kind:'instrumental-arrange',parent_job_id:job.id,instrument:'jazz-bass'},
  result:{asset:{id:'render'},arrangement:arranged}};
await c.showJob(outputJob);
const renderedSummary=$('diagnosticResult').children.find(n=>n.textContent?.includes('5 notes selected;'));
assert.equal(renderedSummary.textContent,readable);assert.doesNotMatch(renderedSummary.textContent,/published_default/);
assert.deepEqual(arranged,unchanged);
console.log('PASS instrumental requests, explicit empty selection/lead consent, alternatives, readiness, retry, noasset completion, pending/stale callback and cancellation ownership; silent.');
