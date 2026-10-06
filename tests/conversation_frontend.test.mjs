import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const script=fs.readFileSync(new URL('../kanban/conversation.js',import.meta.url),'utf8');
class Element {
  constructor(){this.value='';this.textContent='';this.className='';this.children=[];this.hidden=false;this.checked=false;this.disabled=false;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;this.textContent='';}
  setAttribute(){} focus(){} select(){}
}
async function fixture(){
  const elements=new Map(), requests=[];
  const state={date:'2026-10-05',account:'synthetic-account',token:'synthetic-token',snapshot_at:'2026-10-05T12:00:00+08:00',
    model:{ready:true,allowed:false,signature:'synthetic-model',service:'http://127.0.0.1:12345/v1',name:'synthetic'},job:null,
    sources:[{id:'C_11111111',name:'合成甲',kind:'群聊',exported_messages:2,scene_choice:'auto',scene:{label:'待确认',reason:'合成'}},
      {id:'C_22222222',name:'合成乙',kind:'私聊',exported_messages:1,scene_choice:'daily',scene:{label:'日常',reason:'手动'}}]};
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),hidden:false},
    navigator:{clipboard:{writeText:async()=>{}}},setInterval(){},console,
    fetch:async(path,options={})=>{requests.push([path,options]);let value=path==='/api/state'?structuredClone(state):path.startsWith('/api/result')?{result:null}:{ok:true};return{ok:true,json:async()=>value};}});
  vm.runInContext(script+'\nglobalThis.ui={poll,select,act,renderSummary,drafts,toggleChecked,checkedSources};',context);
  await new Promise(resolve=>setImmediate(resolve));
  return {ui:context.ui,get,state,requests};
}
function replyJob(source='C_11111111'){
  return {id:'synthetic-job',operation:'reply',status:'complete',account:'synthetic-account',source_id:source,date:'2026-10-05',
    result:{reply:'新生成的合成回复',reason:'合成依据',context_count:2}};
}
test('startup never starts reading, generation or native interaction',async()=>{
  const f=await fixture();
  assert.equal(f.get('source-name').textContent,'合成甲');
  assert.ok(f.requests.every(([url,opts])=>!opts.method));
  assert.equal(f.get('scene-label').textContent,'待确认');
});
test('a late reply preserves hand-written text and offers the generated alternative',async()=>{
  const f=await fixture();await f.ui.act('reply');
  f.get('draft').value='我正在手写';f.get('draft').oninput();
  f.state.job=replyJob();await f.ui.poll();
  assert.equal(f.get('draft').value,'我正在手写');assert.equal(f.get('use-new').hidden,false);
  f.get('use-new').onclick();assert.equal(f.get('draft').value,'新生成的合成回复');
});
test('switching conversations retains drafts and routes a late reply to its own conversation',async()=>{
  const f=await fixture();await f.ui.act('reply');await f.ui.select('C_22222222');
  f.get('draft').value='乙会话的手写内容';f.get('draft').oninput();
  f.state.job=replyJob();await f.ui.poll();assert.equal(f.get('draft').value,'乙会话的手写内容');
  await f.ui.select('C_11111111');assert.equal(f.get('draft').value,'新生成的合成回复');
});
test('switching accounts or calendar day clears the current editor and consent',async()=>{
  const f=await fixture();f.get('draft').value='昨天草稿';f.get('consent').checked=true;
  f.state.date='2026-10-06';f.state.sources=[];await f.ui.poll();
  assert.equal(f.get('draft').value,'');assert.equal(f.get('consent').checked,false);assert.equal(f.get('generate').disabled,true);
  f.state.sources=[{id:'C_11111111',name:'合成甲',kind:'群聊',exported_messages:1,scene_choice:'auto',scene:{label:'待确认',reason:'合成'}}];
  await f.ui.poll();assert.equal(f.get('draft').value,'');
});
test('unchanged polling preserves conversation buttons and their focus',async()=>{
  const f=await fixture();const first=f.get('sources').children[0];await f.ui.poll();
  assert.equal(f.get('sources').children[0],first);
});
test('model changes clear any previous consent before the next request',async()=>{
  const f=await fixture();f.get('consent').checked=true;f.state.model.signature='new-service';await f.ui.poll();
  assert.equal(f.get('consent').checked,false);
});
test('chat and model text are rendered as text instead of HTML',async()=>{
  const f=await fixture();const text='<img src=x onerror=alert(1)>';
  f.ui.renderSummary({date:'2026-10-05',snapshot_at:'2026-10-05T12:00:00+08:00',count:1,excluded:0,missing_shards:[],evidence:{},
    analysis:{overview:text,highlights:[],followups:[]}});
  assert.equal(f.get('summary').children[0].textContent,text);
  assert.equal(f.get('summary').children[0].innerHTML,undefined);
});
test('multi-selection submits exactly the checked conversations',async()=>{
  const f=await fixture();f.ui.toggleChecked('C_22222222',true);await f.ui.act('batch-summary');
  const request=f.requests.find(([url])=>url==='/api/batch-summary');
  assert.deepEqual(JSON.parse(request[1].body).source_ids,['C_22222222']);
  assert.equal(JSON.parse(request[1].body).source_id,undefined);
});
test('select-all includes today\'s conversations outside the search filter',async()=>{
  const f=await fixture();f.get('search').value='合成甲';f.get('search').oninput();
  f.get('select-all').checked=true;f.get('select-all').onchange();
  assert.equal(f.ui.checkedSources.size,2);assert.equal(f.get('selection-count').textContent,'已选 2 个');
  f.get('consent').checked=true;f.ui.toggleChecked('C_11111111',false);
  assert.equal(f.get('consent').checked,false);assert.equal(f.get('select-all').indeterminate,true);
});
test('a new account or day clears batch selections',async()=>{
  const f=await fixture();f.ui.toggleChecked('C_11111111',true);
  f.state.account='different';await f.ui.poll();
  assert.equal(f.ui.checkedSources.size,0);assert.equal(f.get('batch-summarize').disabled,true);
});
test('batch progress keeps each result attached to its conversation',async()=>{
  const f=await fixture();f.state.job={id:'batch1',operation:'batch-summary',status:'running',phase:'正在整理',
    account:f.state.account,date:f.state.date,items:[{source_id:'C_11111111',name:'合成甲',status:'complete'},
      {source_id:'C_22222222',name:'合成乙',status:'running'}]};
  await f.ui.poll();assert.equal(f.get('batch-panel').hidden,false);assert.equal(f.get('stop-batch').hidden,false);
  assert.equal(f.get('batch-items').children[1].disabled,true);
  await f.get('batch-items').children[0].onclick();assert.equal(f.get('source-name').textContent,'合成甲');
});
