const form=document.getElementById('pairForm'),status=document.getElementById('pairStatus'),code=document.getElementById('pairCode');
let pending=null,timer,visible=true;
function terminal(message){clearTimeout(timer);timer=undefined;pending=null;sessionStorage.removeItem('guitar-pairing');form.querySelector('button').disabled=false;code.textContent='';status.textContent=message}
function valid(value){return value&&typeof value.id==='string'&&typeof value.secret==='string'&&typeof value.code==='string'&&Number.isFinite(value.expires)}
try{pending=JSON.parse(sessionStorage.getItem('guitar-pairing')||'null');if(pending&&!valid(pending))terminal('The saved request is invalid. Request a new connection.')}catch{terminal('The saved request is invalid. Request a new connection.')}
function retry(){if(!pending||!visible)return;clearTimeout(timer);timer=setTimeout(claim,Math.min(2500,Math.max(0,pending.expires*1000-Date.now())))}
async function claim(){
  if(!pending||!visible)return;
  if(Date.now()/1000>=pending.expires){terminal('This request expired. Request a new connection.');return}
  const request=pending;code.textContent=request.code;status.textContent='Compare this code with the laptop, then approve there.';
  try{
    const response=await fetch('/pair/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:request.id,secret:request.secret})});
    if(pending!==request)return;
    if(response.status>=400&&response.status<500&&![408,429].includes(response.status)){terminal('This request was denied, expired, or is invalid. Request a new connection.');return}
    if(!response.ok)throw Error('The laptop is temporarily unavailable. Retrying until this request expires.');
    const result=await response.json();
    if(result.state==='paired'){terminal('Connected. Opening your player...');location.replace('/');return}
    if(result.state!=='pending'){terminal('This request is no longer pending. Request a new connection.');return}
  }catch(error){if(pending!==request)return;status.textContent='Connection interrupted. Retrying until this request expires.'}
  retry();
}
form.onsubmit=async event=>{event.preventDefault();if(pending)return;clearTimeout(timer);form.querySelector('button').disabled=true;
  try{const response=await fetch('/pair/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:document.getElementById('deviceName').value})});if(!response.ok)throw Error('Unable to request pairing. Wait a few seconds and try again.');const result=await response.json();if(!valid(result))throw Error('The laptop returned an invalid pairing request. Try again.');pending=result;sessionStorage.setItem('guitar-pairing',JSON.stringify(pending));await claim()}
  catch(error){terminal(error.message)}
};
if(pending){form.querySelector('button').disabled=true;claim()}
window.addEventListener('pagehide',()=>{visible=false;clearTimeout(timer);timer=undefined});
window.addEventListener('pageshow',()=>{if(!visible){visible=true;if(pending)claim()}});
