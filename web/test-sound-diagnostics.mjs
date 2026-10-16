import assert from 'node:assert/strict';
import {soundDiagnosticSections,mountSoundDiagnostics} from './sound-diagnostics.mjs';

const complete={state:'complete',request:{instrument:'electric'},result:{
  arrangement:{sample_rate:44100,notes:[{id:'held-E',start:5336117,end:5600717}]},
  metrics:{sample_limits:[{note_id:'held-E',transposed_stored_frames_estimate:192984,gate_exceeds_stored_sample_estimate:true}]}}};
const unchanged=structuredClone(complete),sections=soundDiagnosticSections(complete);
assert.equal(sections.length,1);
assert.equal(sections[0].title,'Long holds need review');
assert.match(sections[0].summary,/renderer capacity warning, not a measurement of audible sustain/);
assert.match(sections[0].summary,/Compare the dry sound, requested hold and original phrase/);
assert.match(sections[0].rows[0],/source samples \[5336117, 5600717\) at 44100 Hz; requested gate 6\.000 s/);
assert.match(sections[0].rows[0],/recorded length about 4\.376 s at this pitch/);
assert.match(sections[0].rows[0],/hold exceeds that estimate by 1\.624 s/);
assert.deepEqual(complete,unchanged);
for(const instrument of ['acoustic','jazz-bass',undefined])assert.deepEqual(soundDiagnosticSections({...complete,request:{instrument}}),[]);
assert.deepEqual(soundDiagnosticSections({...complete,state:'running'}),[]);
const short=structuredClone(complete);short.result.metrics.sample_limits[0].gate_exceeds_stored_sample_estimate=false;
assert.deepEqual(soundDiagnosticSections(short),[]);

const failed={state:'failed',request:{instrument:'electric'},result:{
  refusal:{stage:'arrangement',expected:{render_origin_sample:5336117,performers:2},
    observed:{render_origin_sample:0,players:[1],issues:[{note_id:'missing-bass',reason:'No available string.'},null,{reason:''}]},
    source_clock:{sample_rate:44100,origin_sample:5336117,end_sample:5384627},
    next_discriminating_check:'Inspect the source gate and available strings.'},
  arrangement:{adaptations:[{kind:'omission',note_id:'missing-bass',reason:'No available string.'},null]}}};
const refusal=soundDiagnosticSections(failed);
assert.equal(refusal.length,2);assert.match(refusal[0].summary,/No audio was created/);
assert.match(refusal[0].summary,/does not judge the song or transcription/);
assert.equal(refusal[0].rows.filter(row=>row==='Note missing-bass: No available string.').length,1);
assert.ok(refusal[0].rows.includes('Requested 2 players; assigned parts to 1.'));
assert.ok(refusal[0].rows.includes('Passage start: expected source sample 5336117; received 0.'));
assert.ok(refusal[0].rows.includes('Requested passage: source samples [5336117, 5384627) at 44100 Hz.'));
assert.equal(refusal[1].summary,'Inspect the source gate and available strings.');
assert.deepEqual(soundDiagnosticSections({...failed,result:{...failed.result,asset:{id:'already-created'}}}),[]);
assert.deepEqual(soundDiagnosticSections({...failed,request:{instrument:'acoustic'}}),[]);
// A valid maximum-size refusal can have more issues/adaptations than notes.
const fullRefusal=structuredClone(failed);
fullRefusal.result.refusal.observed.issues=[...Array.from({length:4000},(_,i)=>({note_id:`n${i}`,reason:'Not assigned.'})),
  {player:1,reason:'No material.'},{player:2,reason:'No material.'}];
fullRefusal.result.arrangement.adaptations=[...Array.from({length:4000},(_,i)=>({kind:'contour-not-rendered',note_id:`n${i}`})),
  {kind:'omission',note_id:'n3999',reason:'Not assigned.'}];
const fullRows=soundDiagnosticSections(fullRefusal)[0].rows;
assert.ok(fullRows.includes('Note n0: Not assigned.'));assert.ok(fullRows.includes('Note n3999: Not assigned.'));
assert.ok(fullRows.includes('Player 2: No material.'));
fullRefusal.result.refusal.observed.issues=[];
assert.ok(soundDiagnosticSections(fullRefusal)[0].rows.includes('Note n3999: Not assigned.'));

// Unsupported or incomplete stored data never invents a measurement or crashes.
for(const value of [null,{},[],{state:'complete',request:{instrument:'electric'},result:null}])assert.doesNotThrow(()=>soundDiagnosticSections(value));
const malformed=structuredClone(complete);malformed.result.arrangement.notes=[null,5,{},[]];
malformed.result.arrangement.sample_rate=NaN;
malformed.result.metrics.sample_limits=[null,[],5,{note_id:'<img onerror="play()">',transposed_stored_frames_estimate:Infinity,gate_exceeds_stored_sample_estimate:true}];
const malformedRows=soundDiagnosticSections(malformed)[0].rows;
assert.equal(malformedRows.length,1);assert.doesNotMatch(malformedRows[0],/NaN|Infinity|requested gate|recorded length/);

class Node{
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.hidden=false}
  append(...nodes){this.children.push(...nodes)}
  set innerHTML(value){throw Error('Markup is forbidden')}
  play(){throw Error('Playback is forbidden')}
}
const document={createElement:tag=>{assert.ok(['details','summary','p','ul','button','li'].includes(tag));return new Node(tag)}};
const root=new Node('root');mountSoundDiagnostics({job:malformed,root,document});
assert.equal(root.children[0].children[2].children[0].textContent,malformedRows[0]);
assert.ok(root.children[0].children[2].children[0].textContent.includes('<img onerror="play()">'));

const many=structuredClone(complete);many.result.metrics.sample_limits=Array.from({length:201},(_,i)=>({
  note_id:`held-${i}`,transposed_stored_frames_estimate:100,gate_exceeds_stored_sample_estimate:true}));
const paged=new Node('root');mountSoundDiagnostics({job:many,root:paged,document});
const items=paged.children[0].children[2],more=paged.children[0].children[3];
assert.equal(items.children.length,100);assert.equal(more.hidden,false);
more.onclick();assert.equal(items.children.length,200);assert.equal(more.hidden,false);
more.onclick();assert.equal(items.children.length,201);assert.equal(more.hidden,true);
assert.equal(new Set(items.children.map(row=>row.textContent)).size,201);
assert.equal(items.children.at(-1).textContent,'Note held-200; recorded length about 0.002 s at this pitch.');
const bounded=structuredClone(many);bounded.result.metrics.sample_limits.push(...Array(3800).fill({gate_exceeds_stored_sample_estimate:true}));
assert.deepEqual(soundDiagnosticSections(bounded),[]);
console.log('PASS descriptive sound diagnostics: exact source clock/units; capacity versus audibility; refusal and next check; safe text; all201 rows reachable; malformed-data limits; no audio/network.');
