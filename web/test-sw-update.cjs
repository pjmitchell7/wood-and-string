// Frozen v19/v22/v23 controllers and installed v24 cache. Silent VM evidence.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),{createHash}=require('node:crypto');
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
  for(const path of ['/app.js','/app.js?v=23','/mixed-preview.mjs?v=22','/instrumental.mjs?v=22','/refinement.mjs?v=22','/processing-outbox.mjs?v=23'])
    store('wood-shell-v23').set(path,new Response('stale v23 asset'));
  store('wood-media-v1').set('/offline-media/song',new Response('saved music'));
  const old=worker(fs.readFileSync(__dirname+'/fixtures/sw-v19.js','utf8'));
  const html=fs.readFileSync(__dirname+'/index.html','utf8');assert.match(html,/src="\/app\.js\?v=24"/);
  const app=await(await old.shellResponse(new Request(origin+'/app.js?v=24'))).text();
  assert.match(app,/mixed-preview\.mjs\?v=24/);assert.match(app,/canPreviewMixed/);assert.match(app,/transport\.mjs\?v=22/);
  assert.match(app,/processing-outbox\.mjs\?v=24/);
  const old22=worker(fs.readFileSync(__dirname+'/fixtures/sw-v22.js','utf8'));
  assert.equal(await(await old22.shellResponse(new Request(origin+'/app.js?v=24'))).text(),app);
  const frozen23=fs.readFileSync(__dirname+'/fixtures/sw-v23.js');
  assert.equal(createHash('sha256').update(frozen23).digest('hex'),'fbb1008de8afc5df426fd25f5af5d910d5d9eee8643f114d95ffa587baedc60b');
  const old23=worker(frozen23.toString('utf8'));
  assert.equal(await(await old23.shellResponse(new Request(origin+'/app.js?v=24'))).text(),app);
  const outbox=await(await network(new Request(origin+'/processing-outbox.mjs?v=24'))).text();assert.match(outbox,/ProcessingOutbox/);
  const transport=await(await network(new Request(origin+'/transport.mjs?v=22'))).text();assert.match(transport,/scheduledTransport/);
  const module=await(await old.shellResponse(new Request(origin+'/mixed-preview.mjs?v=24'))).text();
  assert.match(module,/Approximate original cue/);
  const refinement=await(await network(new Request(origin+'/refinement.mjs?v=24'))).text();assert.match(refinement,/refinementDraft/);
  const instrumental=await(await network(new Request(origin+'/instrumental.mjs?v=24'))).text();assert.match(instrumental,/mountInstrumentalCandidates/);
  for(const [path,expected]of [['/mixed-preview.mjs?v=24',module],['/instrumental.mjs?v=24',instrumental],['/refinement.mjs?v=24',refinement],['/processing-outbox.mjs?v=24',outbox]])
    assert.equal(await(await old23.shellResponse(new Request(origin+path))).text(),expected);
  const diagnostics=await(await network(new Request(origin+'/sound-diagnostics.mjs?v=24'))).text();assert.match(diagnostics,/export/);
  const current=worker(fs.readFileSync(__dirname+'/sw.js','utf8'));await current.installShell();offline=true;
  assert.equal(await(await current.shellResponse(new Request(origin+'/app.js?v=24'))).text(),app);
  assert.equal(await(await current.shellResponse(new Request(origin+'/processing-outbox.mjs?v=24'))).text(),outbox);
  assert.equal(await(await current.shellResponse(new Request(origin+'/mixed-preview.mjs?v=24'))).text(),module);
  assert.equal(await(await current.shellResponse(new Request(origin+'/transport.mjs?v=22'))).text(),transport);
  assert.equal(await(await current.shellResponse(new Request(origin+'/refinement.mjs?v=24'))).text(),refinement);
  assert.equal(await(await current.shellResponse(new Request(origin+'/instrumental.mjs?v=24'))).text(),instrumental);
  assert.equal(await(await current.shellResponse(new Request(origin+'/sound-diagnostics.mjs?v=24'))).text(),diagnostics);
  assert.equal(await store('wood-media-v1').get('/offline-media/song').text(),'saved music');
  assert.equal(await store('wood-shell-v19').get('/app.js').text(),'old app');
  assert.equal(await store('wood-shell-v23').get('/app.js?v=23').text(),'stale v23 asset');
  console.log('PASS: v19/v22/v23 controllers fetch v24 app; v24 retains Electric and diagnostic modules offline and preserves existing media.');
})().catch(error=>{console.error(error);process.exit(1)});
