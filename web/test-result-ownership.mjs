import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const source=app.slice(app.indexOf('let resultGeneration='),app.indexOf("$('cancelDiagnostic').onclick"));
const nodes=new Map();
class Node{constructor(text=''){this.textContent=text;this.children=[];this.disabled=false;this.value='1'}append(...n){this.children.push(...n)}replaceChildren(...n){this.children=n}}
const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id)};
let releaseRefresh,hold=false,stops=0,starts=0,apiResolve;
const c={$,activeJob:null,activeJobKind:null,pollTimer:null,localStorage:{setItem(){},removeItem(){}},
  jobMessage:text=>$('message').textContent=text,button:(text,fn)=>Object.assign(new Node(text),{onclick:fn}),
  action:fn=>fn(),refresh:()=>hold?new Promise(resolve=>releaseRefresh=resolve):Promise.resolve(),
  tracks:[{id:'asset'}],queue:[],play:()=>{throw Error('Playback forbidden')},download(){},
  restoreMixedSettings(){},showSavedGuidance(){},mountRefinement(){},mixedCandidateSummary:()=>'',describeAdaptation:()=>'',
  originalPassage:()=>({label:'Original passage'}),comparison:{stop:()=>stops++,start:()=>starts++},
  document:{createElement:()=>new Node()},setTimeout:()=>1,clearTimeout(){},
  apiJSON:()=>new Promise(resolve=>apiResolve=resolve)};
vm.createContext(c);vm.runInContext(source,c);
const complete={id:'old',state:'complete',request:{kind:'mixed'},result:{asset:{id:'asset'},arrangement:{adaptations:[]}}};
await c.showJob(complete);
const compare=$('diagnosticResult').children.find(n=>n.textContent==='Compare original passage');assert.ok(compare);
hold=true;let stale=c.showJob(complete);await Promise.resolve();
const before=stops;compare.onclick();const sentinel=new Node('new comparison state');$('diagnosticResult').replaceChildren(sentinel);
releaseRefresh();await stale;assert.equal(stops,before);assert.equal(starts,1);assert.equal($('diagnosticResult').children[0],sentinel);
stale=c.showJob(complete);await Promise.resolve();
const start=$('diagnostic').onclick();assert.equal($('diagnostic').disabled,true);
const afterIntent=stops;releaseRefresh();await stale;
assert.equal(stops,afterIntent);assert.equal($('diagnosticResult').children.length,0);assert.equal($('message').textContent,'Starting sample…');
apiResolve({id:'new-job',state:'queued',request:{kind:'diagnostic'}});await start;
assert.equal(c.activeJob,'new-job');assert.equal($('diagnostic').disabled,true);
assert.equal($('message').textContent,'Waiting to start…');
console.log('PASS actual showJob delayed-refresh ownership: new comparison is not stopped; new job intent/result are not replaced; no playback.');
