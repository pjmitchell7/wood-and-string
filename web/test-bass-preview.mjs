import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as tones from './electric-tone.mjs';
import {mixedPreviewRequest,previewSelection,previewChoice,previewGuidance} from './mixed-preview.mjs';
// Buffer.toString silently replaces malformed bytes; web source must be strict UTF-8.
const strictUTF8=new TextDecoder('utf-8',{fatal:true});
assert.throws(()=>strictUTF8.decode(Uint8Array.of(0xb7)),TypeError);
for(const file of ['index.html','app.js','mixed-preview.mjs','sw.js','refinement.mjs',
  'test-bass-preview.mjs','test-guidance.mjs','test-sw-update.cjs','test-refinement.mjs'])
  strictUTF8.decode(fs.readFileSync(new URL(file,import.meta.url)));
const original={id:'original',url:'/api/media/original',name:'Original'};
assert.deepEqual(mixedPreviewRequest(original,1,6,1,{}, {instrument:'acoustic'}),
  {asset_id:'original',start_seconds:1,end_seconds:6,performers:1});
for(const part of ['melody','bass-line']){
  const request=mixedPreviewRequest(original,1,6,1,{source_notes:'Keep the guitar',arrangement_notes:'Space',accompaniment:'melody-only'}, {instrument:'jazz-bass',bass_part:part});
  assert.equal(request.bass_part,part);assert.equal(request.instrument,'jazz-bass');
  assert.equal(request.guidance.accompaniment,'melody-and-bass');assert.equal(request.guidance.source_notes,'Keep the guitar');
  assert.match(previewChoice(request),/Experimental Jazz Bass/);
}
const duo=mixedPreviewRequest(original,1,6,2,{}, {instrument:'jazz-bass'});
assert.equal(duo.bass_part,'melody-and-bass');assert.equal(duo.guidance,undefined);
assert.match(previewChoice(duo),/upper estimated melody and low bass foundation/);
for(const [selection,players] of [[{instrument:'unsupported'},1],[{instrument:'jazz-bass',bass_part:'bass-line'},2],[{instrument:'jazz-bass',bass_part:'melody-and-bass'},1]])
  assert.throws(()=>previewSelection(selection,players));

// Exercise actual UI functions without an audio element or a browser.
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const source=app.slice(app.indexOf('function selectMixed('),app.indexOf("$('mixedClose').onclick"));
const fields=new Map();
const $=id=>{if(!fields.has(id))fields.set(id,{value:id==='mixedInstrument'?'acoustic':id==='mixedPerformers'?'1':'',options:[{},{}],scrollIntoView(){},focus(){}});return fields.get(id)};
let calls=0,response={bass_ready:true,bass_reason:''};
const scope={...tones,restoredElectricTone:undefined,$ ,mixedSource:null,tracks:[original],previewGuidance,previewSelection,previewChoice,
  ELECTRIC_PREVIEW_ENABLED:false,ELECTRIC_TESTING_REASON:'Electric is still being tested.',electricAvailability:{ready:false},
  bassAvailability:{ready:false,reason:'Not ready'},apiJSON:async url=>{assert.equal(url,'/api/status');calls++;return response},
  document:{createElement:()=>({textContent:''})}};
vm.createContext(scope);vm.runInContext(source,scope);
scope.updateAccompaniment();assert.equal($('mixedBassOption').disabled,true);
await scope.loadBassAvailability();assert.equal(calls,1);assert.equal($('mixedBassOption').disabled,false);
$('mixedAccompaniment').value='melody-only';$('mixedInstrument').value='jazz-bass';scope.updateAccompaniment();
assert.equal($('mixedAccompaniment').value,'melody-and-bass');assert.equal($('mixedAccompaniment').disabled,true);
assert.equal($('mixedAccompanimentLabel').hidden,true);assert.equal($('mixedBassPartLabel').hidden,false);
assert.match($('mixedPerformers').options[0].textContent,/bass player/);
const bassLine=mixedPreviewRequest(original,2,7,1,{source_notes:'Observation'}, {instrument:'jazz-bass',bass_part:'bass-line'});
scope.restoreMixedSettings(bassLine);
assert.equal($('mixedInstrument').value,'jazz-bass');assert.equal($('mixedBassPart').value,'bass-line');
assert.equal($('mixedStart').value,2);assert.equal($('mixedSourceNotes').value,'Observation');
assert.equal(calls,1,'restoring settings must not create any request or play audio');
scope.restoreMixedSettings(duo);assert.equal($('mixedBassPartLabel').hidden,true);assert.match($('mixedPartDescription').textContent,/separate bass players/);
const paragraphs=[];scope.showSavedGuidance(bassLine,{append:x=>paragraphs.push(x.textContent)});
assert.match(paragraphs[0],/estimated bass line/);assert.match(paragraphs[1],/saved note, not analyzed/);
scope.restoreMixedSettings({asset_id:'original',start_seconds:0,end_seconds:5,performers:1});
assert.equal($('mixedInstrument').value,'acoustic');assert.equal($('mixedBassPartLabel').hidden,true);assert.equal($('mixedAccompanimentLabel').hidden,false);
response={bass_ready:false,bass_reason:'Bank verification failed.'};await scope.loadBassAvailability();
assert.equal($('mixedBassOption').disabled,true);assert.equal($('mixedBassAvailability').textContent,'Bank verification failed.');
// Availability completion cannot re-enable a submit button owned by a running job.
$('mixedSubmit').disabled=true;response={bass_ready:true};await scope.loadBassAvailability();assert.equal($('mixedSubmit').disabled,true);
const html=strictUTF8.decode(fs.readFileSync(new URL('./index.html',import.meta.url)));
for(const id of ['mixedInstrument','mixedBassOption','mixedBassPart','mixedBassPartLabel','mixedBassAvailability'])assert.equal((html.match(new RegExp(`id="${id}"`,'g'))||[]).length,1);
assert.match(html,/does not yet recover the original guitar pattern, chords or expressive phrasing/);
console.log('PASS Bass request/legacy shape, hidden accompaniment normalization, solo/duo/restore labels, readiness and asynchronous job-button ownership; silent.');
