const isChat = location.pathname === '/chat';
document.querySelector('#home').hidden = isChat;
document.querySelector('#chat').hidden = !isChat;
if (isChat) {
  const $ = id => document.getElementById(id);
  let history = [], busy = false, controller, remaining = null;
  const prompt = $('prompt'), send = $('send'), stop = $('stop'), notice = $('notice');
  function controls() { send.disabled = busy || remaining === 0 || !prompt.value.trim(); stop.hidden = !busy; send.hidden = busy; }
  async function status() {
    try {
      const response = await fetch('/demo/status', {cache:'no-store'});
      if (!response.ok) throw new Error();
      const data = await response.json(); remaining = data.remaining;
      $('compact-quota').textContent = `${remaining} / 100 shared messages left · resets midnight Taipei`;
      $('remaining').replaceChildren(document.createTextNode(`${remaining} `));
      const small = document.createElement('small'); small.textContent = 'messages left'; $('remaining').append(small);
      $('meter-fill').style.width = `${remaining}%`;
      if (!data.available) { remaining = 0; notice.textContent = 'Chat is not configured yet. Please check back soon.'; }
      else if (!remaining) notice.textContent = 'Our shared 100-message allowance is used up. See you after midnight, Taipei time.';
      else if (!busy && notice.textContent.startsWith('Our shared')) notice.textContent = '';
      controls();
    } catch { notice.textContent = 'Could not check availability. Please check your connection.'; }
  }
  function scroll() { $('message-scroll').scrollTop = $('message-scroll').scrollHeight; }
  function bubble(role, text) {
    $('welcome').hidden = true;
    const row = document.createElement('div'); row.className = `message ${role}`;
    if(role === 'assistant') { const icon = document.createElement('span'); icon.className='avatar'; icon.textContent='e'; row.append(icon); }
    const content = document.createElement('div'); content.className='bubble'; content.textContent=text; row.append(content); $('messages').append(row); scroll(); return content;
  }
  function clear() { if (busy) return; history=[]; $('messages').replaceChildren(); $('welcome').hidden=false; notice.textContent=''; prompt.value=''; controls(); status(); prompt.focus(); }
  $('new-chat').onclick = clear; $('clear-mobile').onclick = clear;
  stop.onclick = () => controller?.abort();
  prompt.oninput = () => { prompt.style.height='auto'; prompt.style.height=`${Math.min(prompt.scrollHeight,150)}px`; controls(); };
  prompt.onkeydown = event => { if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if(!send.disabled) $('chat-form').requestSubmit(); } };
  document.querySelectorAll('[data-prompt]').forEach(button => button.onclick = () => { if(busy || remaining===0)return; prompt.value=button.dataset.prompt; controls(); $('chat-form').requestSubmit(); });
  $('chat-form').onsubmit = async event => {
    event.preventDefault(); const question=prompt.value.trim(); if(!question || busy || remaining===0)return;
    busy=true; controller=new AbortController(); notice.textContent=''; const previous=history.slice(-20);
    bubble('user',question); const answer=bubble('assistant','Thinking…'); let text='', done=false;
    prompt.value=''; prompt.style.height='auto'; controls();
    try {
      const response=await fetch('/demo/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:question,messages:previous}),signal:controller.signal});
      if(!response.ok) { let message='Could not send your message. Please try again.'; try { const data=await response.json(); if(typeof data.detail==='string')message=data.detail; }catch{} throw new Error(message); }
      const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='';
      function consume(frame) {
        const lines=frame.split('\n'), type=lines.find(line=>line.startsWith('event:'))?.slice(6).trim();
        const raw=lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n'); if(!raw)return;
        const data=JSON.parse(raw);
        if(type==='delta'){text+=data.text;answer.textContent=text;scroll();}
        if(type==='error')throw new Error(data.error);
        if(type==='done')done=true;
      }
      while(true){ const chunk=await reader.read(); if(chunk.done)break; buffer+=decoder.decode(chunk.value,{stream:true}); let index; while((index=buffer.indexOf('\n\n'))!==-1){consume(buffer.slice(0,index));buffer=buffer.slice(index+2);} }
      if(!done)throw new Error('The connection ended before the reply was complete.');
      history.push({role:'user',content:question},{role:'assistant',content:text}); history=history.slice(-20);
    } catch(error) {
      const message=error.name==='AbortError'?'Reply stopped.':error.message;
      if(!text)answer.textContent=message; notice.textContent=message;
      if(!text)prompt.value=question;
    } finally {busy=false; controller=null; controls(); await status(); scroll();}
  };
  status(); setInterval(status,30000); controls();
}
