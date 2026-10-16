// Owner controls exist only on the loopback laptop interface.
if(location.hostname==='127.0.0.1'){
  const section=document.createElement('details');section.className='settings';
  const summary=document.createElement('summary');summary.textContent='Connected devices';
  const info=document.createElement('p');info.textContent='Phone setup is being prepared. When a device requests pairing, compare its code here before approving. Revocation stops new transfers; it does not erase music already downloaded.';
  const refresh=document.createElement('button');refresh.textContent='Check connection requests';
  const state=document.createElement('p');state.setAttribute('role','status');
  const rows=document.createElement('div');
  section.append(summary,info,refresh,state,rows);document.querySelector('main').append(section);
  async function action(path,body){await fetch('/session',{cache:'no-store'});const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});if(!response.ok)throw Error('Unable to update connections. Keep the laptop app running.');return response.json()}
  function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await fn();await show()}catch(e){state.textContent=e.message}finally{b.disabled=false}};return b}
  async function show(){const data=await action('/api/pairing');rows.replaceChildren();for(const request of data.requests){const row=document.createElement('p'),text=document.createElement('strong');text.textContent=`${request.label} · ${request.code} · ${request.state}`;row.append(text);if(request.state==='pending')row.append(button('Approve matching code',()=>action('/api/pairing/approve',{id:request.id,approve:true})),button('Deny',()=>action('/api/pairing/approve',{id:request.id,approve:false})));rows.append(row)}for(const device of data.devices){const row=document.createElement('p');row.textContent=device.label+(device.revoked?' · Revoked':' · Connected');if(!device.revoked)row.append(button('Revoke access',()=>action('/api/pairing/revoke',{id:device.id})));rows.append(row)}state.textContent=data.requests.length?'Compare the code on both screens before approving.':'No waiting connection requests.'}
  refresh.onclick=()=>show().catch(error=>state.textContent=error.message);
}
