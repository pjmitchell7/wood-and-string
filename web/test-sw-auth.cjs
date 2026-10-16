const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const handlers={},stored=new Map([['/',new Response('cached-player')],['/index.html',new Response('cached-player')]]);
let network=async()=>new Response('pairing',{headers:{'X-Guitar-Page':'pairing'}});
const context={self:{location:{origin:'https://library.local:8443'},addEventListener:(name,callback)=>handlers[name]=callback},caches:{open:async()=>({match:async key=>stored.get(typeof key==='string'?key:new URL(key.url).pathname)?.clone(),put:async(key,response)=>stored.set(key,response)})},fetch:(...args)=>network(...args),Response,Request,URL,Headers};
vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/sw.js','utf8'),context);
const navigation=path=>({url:'https://library.local:8443'+path,mode:'navigate',method:'GET'});
(async()=>{
  assert.equal(await(await context.shellResponse(navigation('/'))).text(),'cached-player','online pairing HTML must not hide downloaded player');
  network=async()=>new Response('unauthorized',{status:401});assert.equal(await(await context.shellResponse(navigation('/index.html'))).text(),'cached-player');
  network=async()=>new Response('revoked',{status:403});assert.equal(await(await context.shellResponse(navigation('/'))).text(),'cached-player');
  network=async()=>{throw Error('offline')};assert.equal(await(await context.shellResponse(navigation('/'))).text(),'cached-player');
  network=async()=>new Response('online-player',{headers:{'X-Guitar-Page':'player'}});assert.equal(await(await context.shellResponse(navigation('/'))).text(),'online-player');
  let intercepted=false;handlers.fetch({request:navigation('/reconnect'),respondWith(){intercepted=true}});assert.equal(intercepted,false,'explicit reconnect must reach network setup page');
  network=async()=>new Response('pairing',{headers:{'X-Guitar-Page':'pairing'}});await assert.rejects(()=>context.installShell(),/unavailable/);assert.equal(await stored.get('/').clone().text(),'cached-player','install cannot cache pairing page over player');
  stored.clear();assert.equal(await(await context.shellResponse(navigation('/'))).text(),'pairing','new device without a cached player may pair');
  console.log('PASS: cached player survives online revocation/cookie loss/offline; explicit reconnect remains network-only; shell install rejects pairing HTML');
})().catch(error=>{console.error(error);process.exit(1)});
