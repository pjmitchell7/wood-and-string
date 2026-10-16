import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const partsLifecycle=app.slice(app.indexOf('function disposeAcousticParts(){'),app.indexOf('function pausePlayback(){'));
const source=app.slice(app.indexOf('let resultGeneration='),app.indexOf("$('cancelDiagnostic').onclick"));
const nodes=new Map();
class Node{constructor(text=''){this.textContent=text;this.children=[];this.disabled=false;this.value='1'}append(...n){this.children.push(...n)}replaceChildren(...n){this.children=n}}
const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id)};
let releaseRefresh,hold=false,stops=0,starts=0,apiResolve;const handoffs=[];
const c={$,activeJob:null,activeJobKind:null,pollTimer:null,localStorage:{setItem(){},removeItem(){}},
  acousticParts:null,
  jobMessage:text=>$('message').textContent=text,button:(text,fn)=>Object.assign(new Node(text),{onclick:fn}),
  action:fn=>fn(),refresh:()=>hold?new Promise(resolve=>releaseRefresh=resolve):Promise.resolve(),
  tracks:[{id:'asset'}],queue:[],play:()=>{throw Error('Playback forbidden')},download(){},
  restoreMixedSettings(){},showSavedGuidance(){},mountRefinement(){},mountSoundDiagnostics(){},mixedCandidateSummary:()=>'',describeAdaptation:()=>'',
  originalPassage:()=>({label:'Original passage'}),comparison:{stop:()=>stops++,start:()=>{handoffs.push('comparison-start');starts++}},
  document:{createElement:()=>new Node()},setTimeout:()=>1,clearTimeout(){},
  apiJSON:()=>{handoffs.push('request');return new Promise(resolve=>apiResolve=resolve)}};
vm.createContext(c);vm.runInContext(partsLifecycle+source,c);
const complete={id:'old',state:'complete',request:{kind:'mixed'},result:{asset:{id:'asset'},arrangement:{adaptations:[]}}};
await c.showJob(complete);
const compare=$('diagnosticResult').children.find(n=>n.textContent==='Compare original passage');assert.ok(compare);
hold=true;let stale=c.showJob(complete);await Promise.resolve();
const before=stops,retainedParts={unload(){handoffs.push('parts-unload')},dispose(){handoffs.push('parts-dispose')}};c.acousticParts=retainedParts;compare.onclick();const sentinel=new Node('new comparison state');$('diagnosticResult').replaceChildren(sentinel);
assert.deepEqual(handoffs,['parts-unload','comparison-start'],'Comparison takes over after part audio is released.');assert.equal(c.acousticParts,retainedParts,'Same-result comparison retains the reloadable part panel.');
releaseRefresh();await stale;assert.equal(stops,before);assert.equal(starts,1);assert.equal($('diagnosticResult').children[0],sentinel);
assert.equal(c.acousticParts,retainedParts,'A stale refresh cannot remove the comparison handoff panel.');
stale=c.showJob(complete);await Promise.resolve();
c.acousticParts={dispose(){handoffs.push('parts-dispose')}};
const start=$('diagnostic').onclick();assert.equal($('diagnostic').disabled,true);
assert.deepEqual(handoffs.slice(-2),['parts-dispose','request'],'New result work disposes the part engine before its request.');assert.equal(c.acousticParts,null);
const afterIntent=stops;releaseRefresh();await stale;
assert.equal(stops,afterIntent);assert.equal($('diagnosticResult').children.length,0);assert.equal($('message').textContent,'Starting sample…');
apiResolve({id:'new-job',state:'queued',request:{kind:'diagnostic'}});await start;
assert.equal(c.activeJob,'new-job');assert.equal($('diagnostic').disabled,true);
assert.equal($('message').textContent,'Waiting to start…');
console.log('PASS actual showJob delayed-refresh ownership: new comparison is not stopped; new job intent/result are not replaced; no playback.');
