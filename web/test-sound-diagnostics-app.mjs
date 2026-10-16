import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as tones from './electric-tone.mjs';
import {mountSoundDiagnostics} from './sound-diagnostics.mjs';

// Actual result-view function and diagnostic renderer, with only browser,
// library refresh and unrelated panels replaced. No playback or networking.
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const partsLifecycle=app.slice(app.indexOf('function disposeAcousticParts(){'),app.indexOf('function pausePlayback(){'));
assert.match(app,/import \{ mountSoundDiagnostics \} from '\.\/sound-diagnostics\.mjs\?v=24'/);
const code=app.slice(app.indexOf('let resultGeneration='),app.indexOf("$('cancelDiagnostic').onclick"));
class Node{
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.disabled=false}
  append(...nodes){this.children.push(...nodes)}
  replaceChildren(...nodes){this.children=nodes}
  set innerHTML(value){throw Error('Markup forbidden')}
}
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
let refreshes=0,requests=0,playing=0,partsDisposals=0;
const c={...tones,$ ,activeJob:null,activeJobKind:null,pollTimer:null,tracks:[{id:'render'}],
  acousticParts:{dispose(){partsDisposals++}},
  document:{createElement:tag=>{assert.notEqual(tag,'audio');return new Node(tag)}},
  localStorage:{setItem(){},removeItem(){}},mountSoundDiagnostics,
  jobMessage:text=>$('message').textContent=text,button:(text,onclick)=>Object.assign(new Node('button'),{textContent:text,onclick}),
  action:fn=>fn(),refresh:async()=>{refreshes++},restoreMixedSettings(){},showSavedGuidance(){},mountRefinement(){},
  mixedCandidateSummary:()=>'',describeAdaptation:()=>'',
  originalPassage:()=>({label:'Original reference passage'}),comparison:{stop(){},start:()=>{playing++;throw Error('Playback forbidden')}},
  queue:[],play:()=>{playing++;throw Error('Playback forbidden')},download(){throw Error('Download forbidden')},
  setTimeout:()=>1,clearTimeout(){},apiJSON:()=>{requests++;throw Error('Networking forbidden')}};
vm.createContext(c);vm.runInContext(partsLifecycle+code,c);
const failed={id:'stopped',state:'failed',error:'Assignment stopped',request:{kind:'mixed',instrument:'electric'},result:{
  refusal:{stage:'arrangement',expected:{performers:2},observed:{players:[1],issues:[{note_id:'b1',reason:'No available string.'}]},next_discriminating_check:'Inspect this gate.'}}};
await c.showJob(failed);
assert.equal(partsDisposals,1,'A failed result releases the previous saved-part engine.');assert.equal(c.acousticParts,null);
assert.equal($('message').textContent,'Assignment stopped');
assert.ok($('diagnosticResult').children.some(n=>n.tag==='details'&&n.children[0].textContent==='Why this preview stopped'));
assert.equal(refreshes,0);
const complete={id:'saved',state:'complete',request:{kind:'mixed',instrument:'electric'},result:{asset:{id:'render'},
  arrangement:{sample_rate:44100,notes:[{id:'held',start:5336117,end:5600717}],adaptations:[]},
  metrics:{sample_limits:[{note_id:'held',transposed_stored_frames_estimate:192984,gate_exceeds_stored_sample_estimate:true}]}}};
await c.showJob(complete);
const details=$('diagnosticResult').children.filter(n=>n.tag==='details');
assert.equal(details.length,1);assert.equal(details[0].children[0].textContent,'Long holds need review');
assert.match(details[0].children[2].children[0].textContent,/source samples \[5336117, 5600717\)/);
assert.ok($('diagnosticResult').children.some(n=>n.textContent==='Play Electric guitar preview'));
assert.equal(refreshes,1);assert.equal(requests,0);assert.equal(playing,0);
// A superseded response cannot replace a newer result or mount diagnostics.
const current=$('diagnosticResult').children.slice(),currentParts={dispose(){partsDisposals++}};c.acousticParts=currentParts;await c.showJob(failed,0);
assert.deepEqual($('diagnosticResult').children,current);
assert.equal(partsDisposals,1);assert.equal(c.acousticParts,currentParts,'A superseded response cannot dispose the current part engine.');
console.log('PASS actual saved/failed Electric result diagnostics, existing controls and stale-response ownership; no audio/network.');
