import assert from 'node:assert/strict';
import {normalizeElectricTone,electricTonePreset,electricToneForPreset,electricToneLabel,electricToneDescription} from './electric-tone.mjs';
import {mixedPreviewRequest,previewSelection,previewChoice} from './mixed-preview.mjs';
import {ProcessingOutbox} from './processing-outbox.mjs';

const clean={mode:'clean',gain_db:-6,low_cut_hz:90,high_cut_hz:5500,ambience:'off',ambience_mix:0,overload:'refuse'};
assert.equal(normalizeElectricTone(),undefined);
assert.equal(normalizeElectricTone({mode:'bypass'}),undefined);
assert.equal(electricToneForPreset('dry'),undefined);
assert.deepEqual(electricToneForPreset('clean'),clean);
for(const arm of ['room','delay'])assert.deepEqual(electricToneForPreset(arm),{...clean,ambience:arm,ambience_mix:.1});
for(const arm of ['dry','clean','room','delay'])assert.equal(electricTonePreset(electricToneForPreset(arm)),arm);
const custom=Object.freeze({...clean,gain_db:-3,low_cut_hz:110,ambience:'room',ambience_mix:.17});
assert.equal(electricTonePreset(custom),'custom');assert.deepEqual(electricToneForPreset('custom',custom),custom);
assert.notEqual(electricToneForPreset('custom',custom),custom);
assert.throws(()=>electricToneForPreset('custom'));assert.throws(()=>electricToneForPreset('missing'));
const changed=electricToneForPreset('room');changed.ambience_mix=.2;assert.equal(electricToneForPreset('room').ambience_mix,.1);
for(const invalid of [null,[],true,'clean',new Date(),{mode:'drive'},{overload:'report'},{unexpected:1},
  {mode:'bypass',gain_db:-6},{mode:'clean',gain_db:true},{mode:'clean',gain_db:NaN},
  {mode:'clean',low_cut_hz:59},{mode:'clean',high_cut_hz:7501},{mode:'clean',ambience_mix:.1},
  {mode:'clean',ambience:'room',ambience_mix:.251}])assert.throws(()=>normalizeElectricTone(invalid));
assert.match(electricToneLabel(custom),/room/);assert.doesNotMatch(electricToneLabel(custom),/dry/);
assert.match(electricToneDescription(custom),/17%.*-3 dB.*retained exactly/);
const source={id:'source',url:'/api/media/source'};
for(const performers of [1,2]){
  const dry=mixedPreviewRequest(source,0,2,performers,{}, {instrument:'electric',electric_tone:{mode:'bypass'}});
  assert.deepEqual(dry,{asset_id:'source',start_seconds:0,end_seconds:2,performers,instrument:'electric'});
  for(const tone of [clean,custom,electricToneForPreset('delay')]){
    const request=mixedPreviewRequest(source,0,2,performers,{}, {instrument:'electric',electric_tone:tone});
    assert.deepEqual(request.electric_tone,tone);assert.notEqual(request.electric_tone,tone);
    assert.deepEqual(previewSelection(JSON.parse(JSON.stringify(request)),performers).electric_tone,tone);
    assert.doesNotMatch(previewChoice(request),/dry Electric/);
  }
}
for(const instrument of ['acoustic','jazz-bass'])assert.throws(()=>previewSelection({instrument,electric_tone:clean},1),/require Electric/);

// Use the actual durable intent validator to catch disagreement with form choices.
class Store{constructor(){this.rows=new Map()}async get(id){return this.rows.get(id)}async update(id,fn){const row=fn(this.rows.get(id));this.rows.set(id,row);return row}async list(){return [...this.rows.values()]}}
const uuid='00000000-0000-4000-8000-000000000001';
for(const tone of [clean,custom,electricToneForPreset('room'),electricToneForPreset('delay')]){
  const store=new Store(),box=new ProcessingOutbox({store,uuid:()=>uuid,now:()=>1});
  const request=mixedPreviewRequest(source,0,2,1,{}, {instrument:'electric',electric_tone:tone});delete request.asset_id;
  await box.enqueue({sha256:'a'.repeat(64),size:4,type:'audio/wav',name:'authored',track_id:'source'},request);
  assert.deepEqual((await store.get(uuid)).request.electric_tone,tone);
}
console.log('PASS tone presets/custom restoration, strict ranges, unchanged dry identity, non-Electric refusal, saved labels and actual outbox intent validation; silent.');
