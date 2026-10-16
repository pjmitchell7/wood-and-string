// Delivered chunks are journaled independently of the final library transaction.
export class IndexedCaptureStore {
  constructor(indexedDB=globalThis.indexedDB){this.ready=new Promise((resolve,reject)=>{const request=indexedDB.open('wood-capture-journal',1);request.onupgradeneeded=()=>request.result.createObjectStore('takes',{keyPath:'id'});request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);request.onblocked=()=>reject(Error('Close other recording tabs before starting.'))})}
  async update(id,change){const db=await this.ready;return new Promise((resolve,reject)=>{const tx=db.transaction('takes','readwrite'),store=tx.objectStore('takes'),get=store.get(id);let result;get.onsuccess=()=>{try{result=change(get.result);if(result)store.put(result);else store.delete(id)}catch(error){tx.abort();reject(error)}};tx.oncomplete=()=>resolve(result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('Temporary recording transaction stopped.'))})}
  async list(){const db=await this.ready;return new Promise((resolve,reject)=>{const tx=db.transaction('takes','readonly'),get=tx.objectStore('takes').getAll();tx.oncomplete=()=>resolve(get.result);tx.onerror=()=>reject(tx.error)})}
}
const digest=async blob=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))].map(n=>n.toString(16).padStart(2,'0')).join('');
export class CaptureJournal {
  constructor(store=new IndexedCaptureStore()){this.store=store}
  begin(id,name){return this.store.update(id,old=>{if(old)throw Error('Recording identity already exists.');return {id,name,createdAt:Date.now(),complete:false,chunks:[],hashes:[]}})}
  async append(id,blob){const hash=await digest(blob);return this.store.update(id,take=>{if(!take)throw Error('Temporary take is missing.');return {...take,type:blob.type||take.type,chunks:[...take.chunks,blob],hashes:[...take.hashes,hash]}})}
  complete(id){return this.store.update(id,take=>take?{...take,complete:true}:null)}
  remove(id){return this.store.update(id,()=>null)}
  list(){return this.store.list()}
  async recover(take){if(!take.chunks.length)return null;for(let i=0;i<take.chunks.length;i++)if(await digest(take.chunks[i])!==take.hashes[i])throw Error('A temporary recording chunk failed verification. The journal was retained.');return {id:take.id,name:take.name,blob:new Blob(take.chunks,{type:(take.type||'application/octet-stream').split(';')[0]}),complete:take.complete,chunkCount:take.chunks.length}}
}
// Active takes are not recovered/deleted by another supporting browser tab.
export async function takeLease(id,locks=globalThis.navigator?.locks){
  if(!locks)return {exclusive:false,release(){}};
  return new Promise((resolve,reject)=>{locks.request(`wood-capture-${id}`,{ifAvailable:true},lock=>{if(!lock){resolve(null);return}return new Promise(release=>resolve({exclusive:true,release}))}).catch(reject)});
}
