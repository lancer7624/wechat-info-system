'use strict';
const $ = id => document.getElementById(id);
let state = null, selected = '', selectedAccount = '', handledJob = '', resultRequest = 0, polling = false, sourcesKey = '';
const drafts = new Map();
const checkedSources = new Set();
let batchKey = '', openingNote = false;
const draftKey = () => (state?.date||'') + ':' + selectedAccount + ':' + selected;
function message(text, error=false) { $('notice').textContent=text; $('notice').className='notice'+(text?' visible':'')+(error?' error':''); }
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body)};
  const response = await fetch(path, {...options, cache:'no-store'});
  const value = await response.json();
  if(!response.ok) throw new Error(value.error || '操作未完成，请重试。');
  return value;
}
function source() { return state?.sources.find(s=>s.id===selected); }
function saveDraft() { if(selected) drafts.set(draftKey(),{...drafts.get(draftKey()),text:$('draft').value,goal:$('goal').value,reason:$('reply-reason').textContent}); }
function renderSources() {
  const query=$('search').value.trim().toLowerCase();
  const key=JSON.stringify([state.account,state.sources,selected,query,[...checkedSources],state.job?.status==='running']);
  if(key===sourcesKey)return;
  sourcesKey=key;
  $('sources').replaceChildren();
  for(const item of state.sources.filter(s=>s.name.toLowerCase().includes(query))) {
    const row=document.createElement('div');row.className='source-row';
    const check=document.createElement('input');check.type='checkbox';check.checked=checkedSources.has(item.id);
    check.setAttribute('aria-label','选择 '+item.name+' 加入批量总结');check.disabled=state.job?.status==='running';
    check.onchange=()=>toggleChecked(item.id,check.checked);
    const button=document.createElement('button'); button.className='source'+(item.id===selected?' active':'');
    button.setAttribute('aria-pressed',String(item.id===selected));
    const title=document.createElement('strong'); title.textContent=item.name;
    const note=document.createElement('small'); note.textContent=`${item.kind} · ${item.exported_messages} 条可读消息`;
    button.append(title,note); button.onclick=()=>select(item.id);row.append(check,button);$('sources').append(row);
  }
  if(!$('sources').children.length) {const p=document.createElement('p');p.className='empty';p.textContent=state.sources.length?'没有匹配的会话':'暂无今天的会话，点击“更新今天”。';$('sources').append(p);}
  $('source-count').textContent=state.sources.length;
}
function toggleChecked(id, enabled){
  if(!state.sources.some(s=>s.id===id)||state.job?.status==='running')return;
  if(enabled)checkedSources.add(id);else checkedSources.delete(id);
  $('consent').checked=false;renderSources();renderSelection();
}
function renderBatch(){
  const job=state.job;
  if(job?.operation!=='batch-summary'||job.account!==state.account||job.date!==state.date){$('batch-panel').hidden=true;batchKey='';return;}
  $('batch-panel').hidden=false;
  const key=JSON.stringify(job);if(key===batchKey)return;batchKey=key;
  const counts={complete:0,failed:0,skipped:0};
  for(const item of job.items||[])if(item.status in counts)counts[item.status]++;
  $('batch-status').textContent=`共 ${job.items?.length||0} 个 · 已完成 ${counts.complete} 个 · 失败 ${counts.failed} 个`+(counts.skipped?` · 未处理 ${counts.skipped} 个`:'');
  $('stop-batch').hidden=job.status!=='running';$('stop-batch').disabled=Boolean(job.cancel_requested);
  $('stop-batch').textContent=job.cancel_requested?'正在停止…':'停止后续总结';
  $('batch-items').replaceChildren();
  const labels={pending:'等待中',running:'正在整理',complete:'查看总结',failed:'失败',skipped:'未处理'};
  for(const item of job.items||[]){
    const button=document.createElement('button');button.className='batch-result '+item.status;
    const title=document.createElement('strong');title.textContent=item.name;
    const status=document.createElement('span');status.textContent=labels[item.status]||item.status;
    button.append(title,status);button.disabled=item.status!=='complete';
    if(item.error){const error=document.createElement('small');error.textContent=item.error;button.append(error);}
    button.onclick=()=>select(item.source_id);$('batch-items').append(button);
  }
}
function renderSelection() {
  const item=source(), busy=state.job?.status==='running';
  $('source-name').textContent=item?.name||'先更新今天的聊天';
  $('source-status').textContent=item?`今天已读取 ${item.exported_messages} 条可读消息 · ${item.kind}`:'只读取当天，更新后按最近消息排列会话。';
  $('scene-label').textContent=item?.scene.label||'待确认';
  $('scene').value=item?.scene_choice||'auto'; $('scene').disabled=!item||busy;
  $('scene-reason').textContent=item?.scene.reason||'识别出近期话题后会显示标签，也可以手动选择。';
  for(const id of ['summarize','generate']) $(id).disabled=!item||busy||!state.model.ready;
  $('refresh').disabled=busy;
  $('copy').disabled=!$('draft').value.trim();
  $('use-new').hidden=!drafts.get(draftKey())?.alternative;
  $('select-all').disabled=busy||!state.sources.length;
  $('select-all').checked=Boolean(state.sources.length)&&checkedSources.size===state.sources.length;
  $('select-all').indeterminate=checkedSources.size>0&&checkedSources.size<state.sources.length;
  $('selection-count').textContent=`已选 ${checkedSources.size} 个`;
  $('batch-summarize').disabled=busy||!checkedSources.size||!state.model.ready;
  $('batch-summarize').textContent=`总结所选会话${checkedSources.size?'（'+checkedSources.size+'）':''}`;
  renderBatch();
}
async function select(id) {
  saveDraft(); selected=id; selectedAccount=state.account;
  const draft=drafts.get(draftKey())||{text:'',goal:'',reason:''};
  $('draft').value=draft.text; $('goal').value=draft.goal; $('reply-reason').textContent=draft.reason;
  $('consent').checked=false;
  $('reply-note').textContent='生成后可编辑、复制，由你发送。';
  renderSources(); renderSelection();
  const request=++resultRequest, account=selectedAccount;
  $('summary').className='summary empty'; $('summary').textContent='正在读取已有总结…';
  if(!id) {$('summary').textContent='更新今天后，选择需要整理的群聊或联系人。';return;}
  try { const response=await api(`/api/result?source_id=${encodeURIComponent(id)}&account=${encodeURIComponent(account)}`);
    if(request===resultRequest) renderSummary(response.result);
  } catch(error) {if(request===resultRequest){$('summary').textContent=error.message;}}
}
function evidence(container, uids, rows) {
  const details=document.createElement('details'), title=document.createElement('summary');title.textContent='查看原文依据';details.append(title);
  for(const uid of uids) {const row=rows[uid];if(!row)continue;const quote=document.createElement('blockquote');quote.textContent=`${row.time.slice(11,16)} · ${row.sender}\n${row.text}`;details.append(quote);}
  container.append(details);
}
function renderSummary(result) {
  const box=$('summary');box.replaceChildren();box.className='summary'+(result?'':' empty');
  if(!result){box.textContent='还没有今天的总结。点击“总结今天”，完整整理当前会话的当天文字。';return;}
  const overview=document.createElement('p');overview.className='overview';overview.textContent=result.analysis.overview;box.append(overview);
  for(const [key,label] of [['highlights','值得留意'],['followups','事项进展']]) {
    if(!result.analysis[key].length)continue;
    const heading=document.createElement('h4');heading.textContent=label;const list=document.createElement('ul');
    for(const item of result.analysis[key]) {
      const li=document.createElement('li'), strong=document.createElement('strong');strong.textContent=item.title;
      const body=document.createElement('p');body.textContent=key==='highlights'?item.text:`${item.status} · ${item.owner} · ${item.due_text}\n${item.next_action}`;
      li.append(strong,body);evidence(li,item.evidence_uids,result.evidence);list.append(li);
    }box.append(heading,list);
  }
  const coverage=document.createElement('p');coverage.className='coverage';
  coverage.textContent=`${result.date} 00:00—${result.snapshot_at.slice(11,16)} · 覆盖全部 ${result.count} 条可读消息 · ${result.excluded} 条非文字或未解析内容未纳入。新消息可能尚未写入微信主库。`+(result.missing_shards?.length?' 部分分库缺失，覆盖不完整。':'');box.append(coverage);
}
async function poll() {
  if(polling)return;polling=true;
  try {
    const next=await api('/api/state');
    const changed=state&&(state.account!==next.account||state.date!==next.date);
    if(state&&state.model.signature!==next.model.signature)$('consent').checked=false;
    if(changed){saveDraft();selected='';selectedAccount=next.account;checkedSources.clear();$('consent').checked=false;$('draft').value='';$('goal').value='';$('reply-reason').textContent='';}
    state=next;
    for(const id of checkedSources)if(!state.sources.some(s=>s.id===id)){checkedSources.delete(id);$('consent').checked=false;}
    $('capture-note').textContent=state.snapshot_at?`${state.date} · 最近读取 ${state.snapshot_at.slice(0,16).replace('T',' ')}，点击更新可读取新消息。`:`${state.date} · 点击“更新今天”，读取已登录账号的当天消息。`;
    $('model-note').textContent=state.model.ready?`模型：${state.model.name} · ${state.model.service}`:state.model.error;
    $('consent-wrap').hidden=!state.model.ready||state.model.allowed;
    if(!state.sources.some(s=>s.id===selected)){await select(state.sources[0]?.id||'');}else{renderSources();renderSelection();}
    const job=state.job;
    if(job?.status==='running') message(job.phase+'…');
    if(job&&job.status!=='running'&&job.id!==handledJob){
      handledJob=job.id;
      if(job.status==='failed')message(job.error,true);
      else if(job.operation==='refresh'){message(`今天已读取 ${job.result.count} 条可读消息。`);}
      else if(job.operation==='batch-summary'){
        message(`本批已完成 ${job.result.complete} 个，失败 ${job.result.failed} 个，未处理 ${job.result.skipped} 个。可点会话查看总结。`,job.result.failed>0);
        if(job.account===selectedAccount&&job.date===state.date&&job.items.some(item=>item.source_id===selected&&item.status==='complete'))await select(selected);
      }
      else if(job.operation==='reply'&&job.date===state.date){
        const key=job.date+':'+job.account+':'+job.source_id, current=key===draftKey();
        if(current)saveDraft();
        const original=drafts.get(key)||{text:'',goal:'',reason:''};
        if(original.pendingText===undefined||original.text!==original.pendingText){
          drafts.set(key,{...original,alternative:job.result});
          message(current?'新建议已保存。正在编辑的草稿已保留，可点“使用新建议”切换。':'上一会话的新建议已保存。');
        }else{
          drafts.set(key,{text:job.result.reply,goal:original.goal,reason:job.result.reason});
          if(current){$('draft').value=job.result.reply;$('reply-reason').textContent=job.result.reason;$('reply-note').textContent=`参考今天最近 ${job.result.context_count} 条消息`;}
          message(current?'建议已生成，请检查后复制。':'上一会话的建议已保存，切回后可查看。');
        }
      }else if(job.account===selectedAccount&&job.source_id===selected&&job.date===state.date){
        ++resultRequest;renderSummary(job.result);message('当天总结已保存，也可以在信息看板查看。');
      }else{message('上一会话的处理已完成。切回对应会话可查看总结。');}
      renderSelection();
    }
  } catch(error){message('连接暂时中断，请确认工作台仍在运行。',true);}finally{polling=false;}
}
function requestArgs() {return {source_id:selected,account:selectedAccount,model_signature:state.model.signature,consent:$('consent').checked,goal:$('goal').value};}
async function act(operation){
  try {if(operation==='reply'){saveDraft();drafts.get(draftKey()).pendingText=$('draft').value;}
    const args=operation==='refresh'?{}:requestArgs();
    if(operation==='batch-summary'){delete args.source_id;args.source_ids=state.sources.filter(s=>checkedSources.has(s.id)).map(s=>s.id);}
    await api('/api/'+operation,args);await poll();
  }catch(error){message(error.message,true);}
}
$('refresh').onclick=()=>act('refresh');$('summarize').onclick=()=>act('summary');$('generate').onclick=()=>act('reply');
$('batch-summarize').onclick=()=>act('batch-summary');
$('select-all').onchange=()=>{if(!state||state.job?.status==='running')return;checkedSources.clear();if($('select-all').checked)for(const item of state.sources)checkedSources.add(item.id);$('consent').checked=false;renderSources();renderSelection();};
$('stop-batch').onclick=async()=>{try{await api('/api/cancel-batch',{job_id:state.job.id});await poll();}catch(error){message(error.message,true);}};
$('open-note').onclick=async()=>{if(openingNote)return;openingNote=true;$('open-note').disabled=true;try{const result=await api('/api/note',{});message(result.message||'会话笺已打开。');}catch(error){message(error.message,true);}finally{openingNote=false;$('open-note').disabled=false;}};
$('search').oninput=()=>state&&renderSources();
$('scene').onchange=async()=>{try{await api('/api/scene',{source_id:selected,account:selectedAccount,scene:$('scene').value});await poll();}catch(error){message(error.message,true);}};
$('draft').oninput=()=>{saveDraft();$('copy').disabled=!$('draft').value.trim();};
$('goal').oninput=saveDraft;
$('use-new').onclick=()=>{const saved=drafts.get(draftKey());if(!saved?.alternative)return;$('draft').value=saved.alternative.reply;$('reply-reason').textContent=saved.alternative.reason;delete saved.alternative;drafts.set(draftKey(),saved);saveDraft();renderSelection();};
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('draft').value);message('回复已复制。');}catch(error){$('draft').focus();$('draft').select();message('请按 Ctrl+C 复制选中的回复。');}};
poll();setInterval(()=>{if(!document.hidden)poll();},1500);
