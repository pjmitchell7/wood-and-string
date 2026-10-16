// Frozen v19 source verifies a reload through an old active controller as well
// as an installed v22 cache. This is silent VM evidence, not a device trial.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const origin='https://library.local:8443',stores=new Map();
const key=value=>{const url=new URL(typeof value==='string'?value:value.url,origin);return url.pathname+url.search};
const store=name=>{if(!stores.has(name))stores.set(name,new Map());return stores.get(name)};
let offline=false;
const network=async value=>{if(offline)throw Error('offline');const path=key(value).split('?')[0];return new Response(fs.readFileSync(__dirname+(path==='/'?'/index.html':path)))};
function worker(source){
  const handlers={},context={self:{location:{origin},clients:{claim:async()=>{}},addEventListener:(n,f)=>handlers[n]=f},
    caches:{open:async name=>({match:async request=>store(name).get(key(request))?.clone(),put:async(request,response)=>store(name).set(key(request),response)})},
    fetch:network,Response,Request,URL,Headers};
  vm.createContext(context);vm.runInContext(source,context);return context;
}
(async()=>{
  store('wood-shell-v19').set('/app.js',new Response('old app'));
  store('wood-shell-v19').set('/mixed-preview.mjs',new Response('old cue label'));
  store('wood-media-v1').set('/offline-media/song',new Response('saved music'));
  const old=worker(fs.readFileSync(__dirname+'/fixtures/sw-v19.js','utf8'));
  const html=fs.readFileSync(__dirname+'/index.html','utf8');assert.match(html,/src="\/app\.js\?v=22"/);
  const app=await(await old.shellResponse(new Request(origin+'/app.js?v=22'))).text();
  assert.match(app,/mixed-preview\.mjs\?v=22/);assert.match(app,/canPreviewMixed/);assert.match(app,/transport\.mjs\?v=22/);
  const transport=await(await network(new Request(origin+'/transport.mjs?v=22'))).text();assert.match(transport,/scheduledTransport/);
  const module=await(await old.shellResponse(new Request(origin+'/mixed-preview.mjs?v=22'))).text();
  assert.match(module,/Approximate original cue/);
  const refinement=await(await network(new Request(origin+'/refinement.mjs?v=22'))).text();assert.match(refinement,/refinementDraft/);
  const instrumental=await(await network(new Request(origin+'/instrumental.mjs?v=22'))).text();assert.match(instrumental,/mountInstrumentalCandidates/);
  const current=worker(fs.readFileSync(__dirname+'/sw.js','utf8'));await current.installShell();offline=true;
  assert.equal(await(await current.shellResponse(new Request(origin+'/app.js?v=22'))).text(),app);
  assert.equal(await(await current.shellResponse(new Request(origin+'/mixed-preview.mjs?v=22'))).text(),module);
  assert.equal(await(await current.shellResponse(new Request(origin+'/transport.mjs?v=22'))).text(),transport);
  assert.equal(await(await current.shellResponse(new Request(origin+'/refinement.mjs?v=22'))).text(),refinement);
  assert.equal(await(await current.shellResponse(new Request(origin+'/instrumental.mjs?v=22'))).text(),instrumental);
  assert.equal(await store('wood-media-v1').get('/offline-media/song').text(),'saved music');
  assert.equal(await store('wood-shell-v19').get('/app.js').text(),'old app');
  console.log('PASS: v19 controller fetches versioned modules; v22 retains them offline; existing media is preserved.');
})().catch(error=>{console.error(error);process.exit(1)});
