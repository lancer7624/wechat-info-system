// The desktop bridge owns all data and filesystem access. This view only renders
// verified state and sends explicit user actions; polling never invokes a model.
const $ = id => document.getElementById(id);
const local = {state:null, revision:null, source:null, account:null, key:null, polling:false, busy:false, fullDraft:'',supplement:'', draftTimer:null, toastTimer:null, consentAction:null, ui:new Map(), dirty:new Set(), saved:new Map(), saving:new Map(), lastReply:null,lastPlacement:null,transcript:{signature:null,atBottom:true,unread:false,needsBottom:false,uids:new Set()}};
const isDemo = new URLSearchParams(location.search).get('demo') === '1';
const bridge = () => window.pywebview?.api;
const text = (id, value) => { $(id).textContent = value ?? ''; };
const list = value => Array.isArray(value) ? value : [];
const isGroup = source => ['group','chatroom','群聊'].includes(source?.kind);
const messageKinds = {reply:'引用回复',file:'文件标题',link:'链接卡片',share:'分享卡片'};
const replyScenes = {auto:'自动',daily:'日常',work:'职场',customer:'客户',romance:'情感恋爱'};
const stylePanel = {account:null,epoch:0,request:0,profile:null,loading:false,saving:false,learning:false,sceneSaving:null,notesDirty:false,enabledDirty:false,signature:null,error:''};
const memoryPanel = {key:null,account:null,source:null,epoch:0,request:0,value:null,loading:false,saving:false,dirty:new Set(),signature:null,error:'',buffers:new Map()};
const autoPanel = {key:null,busy:false,error:''};
const autoLabels = {off:'由你回复',armed:'等当前会话的新消息',settling:'等待对方说完',generating:'正在拟回复',ready:'即将发送',verifying:'正在核对发送'};
const messageUpdates = new Map();
let quickSceneSaving = null;
function renderQuickControls() {
  const state=local.state, automatic=state?.automatic;
  const enabled=automatic?.enabled && automatic.source_id===local.source && automatic.account_fingerprint===local.account;
  $('mode-suggest').setAttribute('aria-pressed',String(!enabled));
  $('mode-auto').setAttribute('aria-pressed',String(Boolean(enabled)));
  $('mode-suggest').disabled=autoPanel.busy;
  $('mode-auto').disabled=!local.source || autoPanel.busy;
  text('mode-description',enabled ? '当前私聊的新消息会自动生成并发送' : state?.capabilities?.window_interaction===false ? '生成草稿后可复制到微信' : '生成后填入微信，由你发送');
  const tag=state?.scene_label;
  text('scene-label',`${tag?.label || (state?.reply_scene!=='auto' ? replyScenes[state?.reply_scene] : '') || '待确认'}${tag?.origin==='manual' ? ' · 手动' : tag?.origin==='recent' ? ' · 识别' : ''}`);
  $('scene-label').title=tag?.reason || '根据近期消息判断，可手动调整';
  if(!quickSceneSaving) $('quick-scene').value=replyScenes[state?.reply_scene] ? state.reply_scene : 'auto';
  $('quick-scene').disabled=!local.source || Boolean(quickSceneSaving);
}
async function selectQuickScene() {
  const key=local.key,source=local.source,account=local.account,scene=$('quick-scene').value;
  if(!source || quickSceneSaving || !replyScenes[scene]) return;
  quickSceneSaving=key;renderQuickControls();
  try {await invoke('set_reply_scene',scene,source,account);await poll();}
  catch(error) {if(local.key===key)toast(error.message);}
  finally {quickSceneSaving=null;renderQuickControls();}
}
async function chooseSuggestionMode() {
  if(!local.state?.automatic?.enabled) return;
  autoPanel.key=local.key;
  await toggleAutoChat();
  if(autoPanel.error)toast(autoPanel.error);
}
function renderAutomatic() {
  renderQuickControls();
  const value=local.state?.automatic;
  const bound=value?.source_id===local.source && value?.account_fingerprint===local.account;
  $('automatic-strip').hidden=!(bound && value.active_contacts>0);
  text('automatic-label',bound ? value.enabled ? autoLabels[value.status] || '自动聊天' : `${value.active_contacts} 个私聊已启用` : '自动聊天');
  if (!$('auto-dialog').open) return;
  text('auto-source',`「${local.state?.binding?.name || '当前会话'}」`);
  text('auto-detail',isDemo ? '虚构界面演示，不连接微信或发送消息。' : bound ? value.reason || autoLabels[value.status] || '由你回复' : '先选择已识别的私聊。');
  text('auto-error',autoPanel.error);
  $('auto-toggle').disabled=isDemo || autoPanel.busy || !bound || (!value.supported && !value.enabled);
  text('auto-toggle',autoPanel.busy ? '正在保存…' : value?.enabled ? '暂停此会话' : '启用此会话');
  const holder=$('auto-history');holder.replaceChildren();
  const labels={attempted:'已调用，等待记录',recorded:'已在本机记录核对',unknown:'发送结果未确认',acknowledged:'你已接管核对'};
  for(const item of list(bound ? value.history : []).slice().reverse()) {
    const row=create('div','memory-row');row.append(create('small','',`${displayTime(item.time,true)} · ${labels[item.status] || '待检查'}`),create('p','',item.text));holder.append(row);
  }
  if(!holder.children.length)holder.append(create('p','style-detail','启用后，发送尝试和本机核对结果会出现在这里。'));
}
function openAutoPanel() { closeDialog('menu-dialog');autoPanel.key=local.key;autoPanel.error='';showDialog('auto-dialog');renderAutomatic(); }
async function toggleAutoChat() {
  if(autoPanel.busy || isDemo || autoPanel.key!==local.key) return;
  const key=local.key,source=local.source,account=local.account,enabled=Boolean(local.state?.automatic?.enabled);
  if(!enabled && !await flushDrafts())return;
  if(key!==local.key)return;
  autoPanel.busy=true;autoPanel.error='';renderAutomatic();
  try { await invoke('set_auto_chat',!enabled,source,account);await poll(); }
  catch(error) { if(key===local.key)autoPanel.error=error.message; }
  finally { if(key===local.key){autoPanel.busy=false;renderAutomatic();} }
}
async function pauseAutomatic() {
  if(!local.state?.automatic?.active_contacts || isDemo)return;
  await action('pause_auto_chat',local.account);
}
let closedImageRequest=null;
const displayTime = (value, date=false) => {
  if (!value) return '尚未读取';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(date ? {month:'2-digit',day:'2-digit'} : {}),hour:'2-digit',minute:'2-digit',hour12:false}).format(parsed);
};
function create(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function toast(message) {
  clearTimeout(local.toastTimer);
  text('toast', message); $('toast').hidden = false;
  local.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4200);
}
async function invoke(method, ...args) {
  const api = bridge();
  let result;
  if (!api || typeof api[method] !== 'function') {
    if (isDemo) result=await demoAction(method, ...args);
    else throw new Error('尚未连接桌面助手，请通过桌面程序打开。');
  } else result=await api[method](...args);
  if (result?.ok === false && !result?.consent_required) throw new Error(result.error || result.message || '操作未完成，请稍后重试。');
  return result;
}
async function action(method, ...args) {
  try { const result = await invoke(method, ...args); await poll(); return result; }
  catch(error) { toast(error.message || '操作未完成，请稍后重试。'); return null; }
}
function showDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
function closeDialog(id) { if ($(id).open) $(id).close(); }
function memoryChanges() { return {enabled:$('memory-enabled').checked,feedback_enabled:$('memory-feedback').checked,notes:$('memory-notes').value}; }
function memorySession(key,epoch) { return key===local.key && key===memoryPanel.key && epoch===memoryPanel.epoch; }
function resetMemoryPanel() {
  if (memoryPanel.key && memoryPanel.dirty.size) memoryPanel.buffers.set(memoryPanel.key,{changes:memoryChanges(),dirty:[...memoryPanel.dirty]});
  closeDialog('memory-dialog');
  Object.assign(memoryPanel,{key:null,account:null,source:null,epoch:memoryPanel.epoch+1,request:memoryPanel.request+1,value:null,loading:false,saving:false,dirty:new Set(),signature:null,error:''});
  $('memory-notes').value='';$('memory-enabled').checked=true;$('memory-feedback').checked=true;
  $('memory-messages').replaceChildren();$('memory-examples').replaceChildren();
}
function renderMemoryPanel() {
  if (!$('memory-dialog').open) return;
  const value=memoryPanel.value,unavailable=!value || memoryPanel.loading || memoryPanel.saving;
  text('memory-source',`「${local.state?.binding?.name || '当前会话'}」专属 · 本机保存`);
  text('memory-summary',value ? `已记住 ${list(value.messages).length} 条原文 · ${list(value.examples).length} 条已发送改稿${value.snapshot_at ? ` · 截至 ${displayTime(value.snapshot_at,true)}` : ''}` : memoryPanel.loading ? '正在读取…' : '记忆暂不可用');
  if (value) {
    if (!memoryPanel.dirty.has('notes')) $('memory-notes').value=value.notes || '';
    if (!memoryPanel.dirty.has('enabled')) $('memory-enabled').checked=value.enabled!==false;
    if (!memoryPanel.dirty.has('feedback_enabled')) $('memory-feedback').checked=value.feedback_enabled!==false;
  }
  text('memory-count',`${$('memory-notes').value.length} / 500`);
  $('memory-save').disabled=unavailable || !memoryPanel.dirty.size || $('memory-notes').value.length>500;
  $('memory-clear').disabled=unavailable || Boolean(memoryPanel.dirty.size);
  $('memory-save').textContent=memoryPanel.saving ? '正在保存…' : '保存偏好';
  text('memory-status',memoryPanel.error || (memoryPanel.dirty.size ? '偏好尚未保存。' : local.state?.memory?.error || '打开和修改记忆不会调用模型。'));
  $('memory-status').classList.toggle('inline-error',Boolean(memoryPanel.error || local.state?.memory?.error));
  const messages=$('memory-messages'),examples=$('memory-examples');messages.replaceChildren();examples.replaceChildren();
  const control=(uid,action,label)=>{
    const button=create('button','quiet-button',label);button.type='button';button.disabled=unavailable || Boolean(memoryPanel.dirty.size);
    button.addEventListener('click',()=>mutateMemory(action,uid));return button;
  };
  for (const entry of list(value?.examples).slice().reverse()) {
    const row=create('div','memory-row');row.append(create('small','',`${displayTime(entry.message?.time,true)} · 实际已发送`));
    row.append(create('p','memory-original',`原草稿：${entry.proposal || ''}`),create('p','',entry.message?.text || ''));
    const actions=create('div','memory-row-actions');actions.append(control(entry.message?.uid,'forget_example','移除示例'));row.append(actions);examples.append(row);
  }
  if (!examples.children.length) examples.append(create('p','style-detail','清楚核对到实际发送的修改后，会在这里显示。'));
  const pins=new Set(list(value?.pins)),all=list(value?.messages),recent=all.slice(-16);
  const visible=[...all.filter(row=>pins.has(row.uid) && !recent.some(item=>item.uid===row.uid)),...recent];
  for (const message of visible.slice().reverse()) {
    const row=create('div','memory-row');row.append(create('small','',`${message.is_self ? '我' : message.sender || '对方'} · ${displayTime(message.time,true)}${pins.has(message.uid) ? ' · 已固定' : ''}`),create('p','',message.text));
    const actions=create('div','memory-row-actions');actions.append(control(message.uid,pins.has(message.uid) ? 'unpin' : 'pin',pins.has(message.uid) ? '取消固定' : '固定'),control(message.uid,'forget','移除'));row.append(actions);messages.append(row);
  }
  if (!messages.children.length) messages.append(create('p','style-detail','读取这段会话后，前情会逐步保存在这里。'));
}
async function loadContactMemory() {
  const key=memoryPanel.key,epoch=memoryPanel.epoch,request=++memoryPanel.request;
  memoryPanel.loading=true;renderMemoryPanel();
  try {
    const result=await invoke('get_contact_memory',memoryPanel.source,memoryPanel.account);
    if (!memorySession(key,epoch) || request!==memoryPanel.request) return;
    if (result.account_fingerprint!==memoryPanel.account || result.source_id!==memoryPanel.source || !result.memory) throw new Error('会话已变化，请重新打开记忆。');
    if (!memoryPanel.value || result.memory.revision>=memoryPanel.value.revision) memoryPanel.value=result.memory;
  } catch(error) { if (memorySession(key,epoch) && request===memoryPanel.request) memoryPanel.error=error.message || '记忆暂不可用。'; }
  finally { if (memorySession(key,epoch) && request===memoryPanel.request) {memoryPanel.loading=false;renderMemoryPanel();} }
}
async function openMemoryPanel() {
  closeDialog('menu-dialog');
  if (!local.source || !local.account || local.account==='current') {toast('先选择一个会话。');return;}
  const newlyBound=memoryPanel.key!==local.key;
  if (newlyBound) resetMemoryPanel();
  Object.assign(memoryPanel,{key:local.key,account:local.account,source:local.source,error:'',signature:JSON.stringify(local.state?.memory || {})});
  const buffer=newlyBound ? memoryPanel.buffers.get(local.key) : null;
  if (buffer) {
    memoryPanel.dirty=new Set(buffer.dirty);$('memory-notes').value=buffer.changes.notes;
    $('memory-enabled').checked=buffer.changes.enabled;$('memory-feedback').checked=buffer.changes.feedback_enabled;
  }
  showDialog('memory-dialog');renderMemoryPanel();await loadContactMemory();
}
function memoryChanged(field) { memoryPanel.dirty.add(field);memoryPanel.error='';renderMemoryPanel(); }
async function saveContactMemory() {
  if (!memoryPanel.value || memoryPanel.loading || memoryPanel.saving || !memoryPanel.dirty.size) return;
  const key=memoryPanel.key,epoch=memoryPanel.epoch,changes=memoryChanges();
  if (changes.notes.length>500) {memoryPanel.error='这个人的口吻偏好最多 500 字。';renderMemoryPanel();return;}
  memoryPanel.saving=true;memoryPanel.error='';renderMemoryPanel();
  try {
    await invoke('save_contact_memory',{...changes,notes:changes.notes.trim()},memoryPanel.source,memoryPanel.account,memoryPanel.value.settings_revision);
    if (!memorySession(key,epoch)) return;
    await loadContactMemory();
    if (!memorySession(key,epoch)) return;
    const current=memoryChanges();
    for (const field of ['enabled','feedback_enabled','notes']) {
      const saved=field==='notes' ? changes.notes.trim() : changes[field];
      if (current[field]===changes[field] && memoryPanel.value?.[field]===saved) memoryPanel.dirty.delete(field);
    }
    if (!memoryPanel.dirty.size) memoryPanel.buffers.delete(key);
    await poll();
  } catch(error) {
    if (memorySession(key,epoch)) {memoryPanel.error=error.message || '偏好尚未保存。';await loadContactMemory();}
  } finally {if (memorySession(key,epoch)) {memoryPanel.saving=false;renderMemoryPanel();}}
}
async function mutateMemory(action,uid=null) {
  if (!memoryPanel.value || memoryPanel.loading || memoryPanel.saving || memoryPanel.dirty.size) return;
  const key=memoryPanel.key,epoch=memoryPanel.epoch,source=memoryPanel.source,account=memoryPanel.account,revision=memoryPanel.value.settings_revision;
  memoryPanel.saving=true;memoryPanel.error='';renderMemoryPanel();
  try {
    if (action==='clear') await invoke('clear_contact_memory',source,account,revision);
    else await invoke('update_memory_item',uid,action,source,account,revision);
    if (!memorySession(key,epoch)) return;
    await loadContactMemory();await poll();
  } catch(error) {if (memorySession(key,epoch)) {memoryPanel.error=error.message || '记忆未修改。';await loadContactMemory();}}
  finally {if (memorySession(key,epoch)) {memoryPanel.saving=false;renderMemoryPanel();}}
}
function resetStylePanel() {
  closeDialog('style-dialog');closeDialog('menu-dialog');
  Object.assign(stylePanel,{account:null,epoch:stylePanel.epoch+1,request:stylePanel.request+1,profile:null,loading:false,saving:false,learning:false,sceneSaving:null,notesDirty:false,enabledDirty:false,signature:null,error:''});
  $('style-notes').value='';$('style-enabled').checked=true;$('reply-scene').value='auto';
  $('style-profiles').replaceChildren();text('style-summary','尚未学习');text('style-range','');text('style-status','');text('style-model','');text('style-notes-count','0 / 500');
}
function styleSessionMatches(account,epoch) { return account===local.account && account===stylePanel.account && epoch===stylePanel.epoch; }
function styleRunning() { return stylePanel.learning || ['running','queued','learning'].includes(local.state?.style?.status); }
function styleModelConfigured() { return Boolean(local.state?.model?.model && local.state?.model?.base_url); }
function styleDate(value) {
  if (!value) return '';
  const date=new Date(value);
  return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
}
function renderStylePanel() {
  if (!$('style-dialog').open) return;
  const profile=stylePanel.profile;
  const running=styleRunning(), dirty=stylePanel.notesDirty || stylePanel.enabledDirty;
  $('style-demo-note').hidden=!isDemo;
  text('style-summary',profile ? profile.sample_count ? `已参考 ${profile.sample_count} 条本人消息${styleDate(profile.learned_at) ? ` · ${styleDate(profile.learned_at)} 学习` : ''}` : '尚未学习 · 可先填写偏好' : stylePanel.loading ? '正在读取口吻档案…' : '档案暂不可用');
  const range=profile?.range || {};
  text('style-range',range.start && range.end ? `消息范围：${styleDate(range.start)}—${styleDate(range.end)}${range.snapshot_at ? `；记录截至 ${displayTime(range.snapshot_at,true)}` : ''}` : '学习后显示实际消息范围。');
  const holder=$('style-profiles');holder.replaceChildren();
  const traitLabels={sentence_length:{short:'短句为主',mixed:'长短句结合',long:'完整长句'},tone:{plain:'自然平实',warm:'温和亲切',playful:'轻松机灵',direct:'直接明快',formal:'正式稳妥'},humor:{none:'少用玩笑',light:'轻轻接梗',frequent:'常有幽默'},emoji:{none:'少用表情',occasional:'偶尔用表情',frequent:'常用表情'},punctuation:{minimal:'标点简约',natural:'标点自然',full:'标点完整'}};
  for (const scene of ['daily','work','customer','romance']) {
    const learned=profile?.profiles?.[scene];
    if (!learned || !learned.sample_count) continue;
    const row=create('div','style-profile');
    row.append(create('strong','',`${replyScenes[scene]} · ${learned.sample_count} 条`));
    const traits=Object.entries(traitLabels).map(([key,labels])=>labels[learned.traits?.[key]]).filter(Boolean);
    row.append(create('p','',traits.length ? traits.join(' · ') : '暂未提炼出稳定习惯'));holder.append(row);
  }
  if (!holder.children.length) holder.append(create('p','style-detail','学习后，这里会显示不同场景的表达习惯。'));
  if (profile && !stylePanel.notesDirty) $('style-notes').value=profile.notes || '';
  if (profile && !stylePanel.enabledDirty) $('style-enabled').checked=profile.enabled!==false;
  text('style-notes-count',`${$('style-notes').value.length} / 500`);
  if (stylePanel.sceneSaving!==local.source) $('reply-scene').value=replyScenes[local.state?.reply_scene] ? local.state.reply_scene : 'auto';
  $('reply-scene').disabled=!local.source || Boolean(stylePanel.sceneSaving);
  text('style-scene-note',local.source ? `「${local.state?.binding?.name || '当前会话'}」专用，选择后立即保存。` : '选择会话后可设置场景。');
  const unavailable=!profile || stylePanel.loading || stylePanel.saving || running;
  $('style-save').disabled=unavailable || !dirty || $('style-notes').value.length>500;
  $('style-learn').disabled=unavailable || dirty || !styleModelConfigured() || local.state?.status?.ai==='running';
  $('style-learn').title=dirty ? '先保存偏好，再重新学习' : '';
  $('style-save').textContent=stylePanel.saving ? '正在保存…' : '保存偏好';
  $('style-learn').textContent=running ? '正在学习…' : '重新学习';
  const message=stylePanel.error || (running ? local.state?.style?.message || '正在总结表达习惯，可继续编辑偏好。' : dirty ? '偏好尚未保存，先保存后可重新学习。' : local.state?.style?.message || '打开、查看和保存偏好不会调用模型。');
  text('style-status',message);$('style-status').classList.toggle('inline-error',Boolean(stylePanel.error) || local.state?.style?.status==='error');
  text('style-model',isDemo ? '演示不调用模型，重新学习不会生成新档案。' : `点击「重新学习」才交给已配置服务：${local.state?.model?.model || '尚未配置'}${local.state?.model?.base_url ? ` · ${local.state.model.base_url}` : ''}`);
}
async function loadStyleProfile() {
  const account=stylePanel.account,epoch=stylePanel.epoch,request=++stylePanel.request;
  stylePanel.loading=true;renderStylePanel();
  try {
    const result=await invoke('get_style_profile');
    if (!styleSessionMatches(account,epoch) || request!==stylePanel.request) return;
    if (result?.account_fingerprint!==account || !result.profile) throw new Error('账号已变化，请等待记录更新后重新打开。');
    if (!stylePanel.profile || result.profile.revision>=stylePanel.profile.revision) stylePanel.profile=result.profile;
  } catch(error) {
    if (styleSessionMatches(account,epoch) && request===stylePanel.request) stylePanel.error=error.message || '口吻档案暂时无法读取。';
  } finally {
    if (styleSessionMatches(account,epoch) && request===stylePanel.request) { stylePanel.loading=false;renderStylePanel(); }
  }
}
async function openStylePanel() {
  closeDialog('menu-dialog');
  if (!local.account || local.account==='current') { toast('等本机账号记录就绪后再打开我的口吻。');return; }
  if (stylePanel.account!==local.account) resetStylePanel();
  stylePanel.account=local.account;stylePanel.error='';stylePanel.signature=JSON.stringify(local.state?.style || {});
  showDialog('style-dialog');renderStylePanel();await loadStyleProfile();
}
function styleChanged(field) {
  stylePanel[`${field}Dirty`]=true;stylePanel.error='';renderStylePanel();
}
async function saveStyleProfile() {
  if (!stylePanel.profile || stylePanel.loading || stylePanel.saving || styleRunning()) return;
  const account=stylePanel.account,epoch=stylePanel.epoch,revision=stylePanel.profile.revision;
  const inputNotes=$('style-notes').value;
  const changes={enabled:$('style-enabled').checked,notes:inputNotes.trim()};
  if (inputNotes.length>500) { stylePanel.error='口吻偏好最多 500 字。';renderStylePanel();return; }
  stylePanel.saving=true;stylePanel.error='';renderStylePanel();
  try {
    await invoke('save_style_profile',changes,account,revision);
    if (!styleSessionMatches(account,epoch)) return;
    await loadStyleProfile();
    if ($('style-notes').value===inputNotes && stylePanel.profile?.notes===changes.notes) stylePanel.notesDirty=false;
    if ($('style-enabled').checked===changes.enabled && stylePanel.profile?.enabled===changes.enabled) stylePanel.enabledDirty=false;
    await poll();
  } catch(error) {
    if (styleSessionMatches(account,epoch)) { stylePanel.error=error.message || '偏好尚未保存，请重试。';await loadStyleProfile(); }
  } finally {
    if (styleSessionMatches(account,epoch)) { stylePanel.saving=false;renderStylePanel(); }
  }
}
async function learnStyleProfile() {
  if (!stylePanel.profile || stylePanel.loading || stylePanel.saving || styleRunning() || stylePanel.notesDirty || stylePanel.enabledDirty || !styleModelConfigured() || local.state?.status?.ai==='running') return;
  const account=stylePanel.account,epoch=stylePanel.epoch,revision=stylePanel.profile.revision;
  stylePanel.learning=true;stylePanel.error='';renderStylePanel();
  try {
    await invoke('learn_style',account,revision);
    if (!styleSessionMatches(account,epoch)) return;
    await poll();
    if (styleSessionMatches(account,epoch)) await loadStyleProfile();
  } catch(error) {
    if (styleSessionMatches(account,epoch)) stylePanel.error=error.message || '本次未能学习，原口吻档案已保留。';
  } finally {
    if (styleSessionMatches(account,epoch)) { stylePanel.learning=false;renderStylePanel(); }
  }
}
async function setReplyScene() {
  const scene=$('reply-scene').value,source=local.source,account=local.account,epoch=stylePanel.epoch;
  if (!source || !replyScenes[scene] || stylePanel.sceneSaving) return;
  stylePanel.sceneSaving=source;stylePanel.error='';renderStylePanel();
  try {
    await invoke('set_reply_scene',scene,source,account);
    if (!styleSessionMatches(account,epoch)) return;
    await poll();
  } catch(error) {
    if (styleSessionMatches(account,epoch) && source===local.source) stylePanel.error=error.message || '场景尚未保存，请重试。';
  } finally {
    if (styleSessionMatches(account,epoch)) { stylePanel.sceneSaving=null;renderStylePanel(); }
  }
}
function preserveConversationUI() {
  if (!local.source) return;
  local.ui.set(local.key,{goal:$('goal-input').value,supplement:local.supplement,short:$('draft-short').value,full:local.fullDraft});
}
function switchConversation(next,state) {
  closeDialog('image-dialog');
  $('image-preview').setAttribute('src','');
  preserveConversationUI(); clearTimeout(local.draftTimer);
  if (local.source && local.dirty.has(local.key)) saveConversationDraft(local.key,local.source,local.account,{short:$('draft-short').value,full:local.fullDraft});
  local.source=next;local.account=state.account_fingerprint || 'current';local.key=JSON.stringify([local.account,next]);
  const saved=local.ui.get(local.key);
  $('goal-input').value=saved?.goal ?? (isDemo ? state.reply?.input?.goal || '' : '');
  local.supplement=saved?.supplement || '';
  const drafts=local.dirty.has(local.key) && saved ? saved : state.drafts || state.reply?.drafts || {};
  $('draft-short').value=drafts.short || '';local.fullDraft=drafts.full || '';
  local.lastReply=state.reply?.generated_at || null;
  $('draft-save-state').hidden=true;
  if (local.consentAction && local.consentAction.key !== local.key) {
    local.consentAction=null;closeDialog('consent-dialog');toast('会话已切换，请在当前会话重新操作。');
  }
}
function renderConversation(state, changedSource) {
  const key=JSON.stringify([state.account_fingerprint,state.binding?.source_id]);
  const rows=[...list(state.context?.messages),...list(state.images?.messages).filter(row=>row.source_id===state.binding?.source_id)];
  const ids=new Set(rows.map(row=>row.uid).filter(Boolean));
  let progress=messageUpdates.get(key);
  if(!progress) {
    progress={seen:new Set(),initialized:false,added:0,reply:null};
    messageUpdates.set(key,progress);
    if(messageUpdates.size>64)messageUpdates.delete(messageUpdates.keys().next().value);
  }
  if(state.context && !progress.initialized) {progress.initialized=true;progress.seen=new Set(ids);}
  else if(progress.initialized) for(const uid of ids) if(!progress.seen.has(uid)) {progress.added++;progress.seen.add(uid);}
  // Count changes since the last generated reply. Repeated polling, sliding
  // windows, switching away and back, or a second account cannot inflate it.
  const reply=state.reply?.generated_at || null;
  if(reply && reply!==progress.reply) {
    progress.added=rows.filter(row=>row.uid && new Date(row.time)>new Date(reply)).length;
    progress.reply=reply;
  }
  if(progress.seen.size>4096)progress.seen=new Set([...progress.seen].slice(-2048));
  text('conversation-count',!state.binding?.source_id ? '等待当前会话' : !state.context ? '正在读取消息' : progress.added ? `对话更新了 ${progress.added} 条` : `已同步 ${ids.size} 条消息`);
  text('conversation-detail',!state.binding?.source_id ? state.capabilities?.window_interaction===false ? '从上方选择会话' : '在微信中打开一个会话' : progress.added ? '生成建议时会参考最新内容' : '有新消息时会自动更新');
  $('conversation-count').title='当前已读取范围的更新数量，生成新建议后重新计数';
}
function renderDrafts(state, changedSource) {
  const generated = state.reply?.generated_at || null;
  const newReply = generated && generated !== local.lastReply;
  const drafts = state.drafts || state.reply?.drafts || {};
  const saved = local.saved.get(local.key);
  // Keep the local edit protected until a later state snapshot echoes the save.
  // A get_state call already in flight when saving may still contain old text.
  if (saved && saved.short === (drafts.short || '') && saved.full === (drafts.full || '') && saved.short === $('draft-short').value && saved.full === local.fullDraft) {
    local.dirty.delete(local.key); local.saved.delete(local.key);
  }
  if (!changedSource && !local.dirty.has(local.key) && document.activeElement !== $('draft-short')) {
    $('draft-short').value = drafts.short || '';
    local.fullDraft = drafts.full || '';
  }
  const hasDrafts = Boolean(state.reply || $('draft-short').value || local.fullDraft);
  $('drafts-area').hidden = !hasDrafts;
  renderReplyMeta(state);
  if (newReply && !changedSource && local.dirty.has(local.key)) toast('新建议已生成，你的修改已保留。');
  local.lastReply = generated;
}
function renderReplyMeta(state) {
  const reply = state?.reply;
  const warnings=[];
  if (reply?.context_hash && state.context?.context_hash && reply.context_hash !== state.context.context_hash) warnings.push('有新消息，以下草稿基于较早上下文');
  if (reply?.input && ((reply.input.goal || '') !== $('goal-input').value || (reply.input.supplement || '') !== local.supplement)) warnings.push('按上次提交的想法生成，可重新生成');
  text('reply-meta',warnings.join('；')); $('reply-meta').hidden=!warnings.length;
  $('reply-meta').classList.toggle('stale',Boolean(warnings.length));
}
function renderContacts() {
  const holder = $('contact-list'); holder.replaceChildren();
  const query = $('contact-search').value.trim().toLocaleLowerCase();
  const sources = list(local.state?.sources).filter(item => `${item.name} ${item.id}`.toLocaleLowerCase().includes(query));
  if (!sources.length) { holder.append(create('p','empty-note',query ? '没有找到匹配的会话。' : '尚无可用会话，读取本机记录后会显示在这里。')); return; }
  for (const source of sources) {
    const selected = source.id === local.source;
    const button = create('button',`contact-option${selected ? ' selected' : ''}`); button.type='button';
    button.dataset.test='contact-option'; button.dataset.source=source.id;
    const content = create('div'); content.append(create('div','contact-option-name',source.name || source.id));
    content.append(create('div','contact-option-meta',`${isGroup(source) ? '群聊' : '联系人'} · ${source.id}`));
    button.append(content,create('span','contact-option-mark',selected ? '当前' : '→'));
    button.addEventListener('click', async () => {
      await flushDrafts();
      const result = await action('select_conversation',source.id);
      if (result?.ok !== false && result !== null) closeDialog('contacts-dialog');
    });
    holder.append(button);
  }
}
function renderState(state) {
  if (!state || typeof state !== 'object') return;
  if ((state.account_fingerprint || 'current') === local.account && typeof state.revision === 'number' && typeof local.revision === 'number' && state.revision < local.revision) return;
  const nextSource = state.binding?.source_id || null;
  const changedSource = nextSource !== local.source || (state.account_fingerprint || 'current') !== local.account;
  if(changedSource){closeDialog('auto-dialog');autoPanel.key=null;autoPanel.busy=false;autoPanel.error='';}
  if (changedSource && memoryPanel.key) resetMemoryPanel();
  if (local.account && (state.account_fingerprint || 'current')!==local.account) resetStylePanel();
  local.state = state;
  $('author-workbench-menu-item').hidden=!state.workbench_available;
  if (changedSource) switchConversation(nextSource,state);
  const collapsed = Boolean(state.settings?.collapsed);
  $('app').hidden=collapsed; $('card-stage').hidden=collapsed; $('bookmark').hidden=!collapsed;
  text('contact-name',state.binding?.name || '选择会话');
  $('contact-name').title=state.binding?.name || '选择会话';
  const following = state.binding?.mode !== 'pinned';
  const windowInteraction = state.capabilities?.window_interaction !== false;
  text('follow-label',isDemo ? '示例' : !windowInteraction ? '手动' : following ? '跟随' : '固定');
  $('follow-button').disabled = !windowInteraction;
  $('follow-button').setAttribute('aria-pressed',String(following));
  $('follow-button').title = !windowInteraction ? state.capabilities.window_interaction_reason : following ? '点击固定当前会话' : '点击恢复自动跟随';
  text('snapshot-label',isDemo ? '虚构对话 · Gemini 示例' : state.context?.snapshot_at ? `截至 ${displayTime(state.context.snapshot_at,true)}` : '等待读取');
  const sync=state.sync?.source_id===nextSource ? state.sync : null;
  const syncStatus=sync?.status || (state.status?.capture==='error' ? 'error' : state.status?.capture==='running' ? 'updating' : 'idle');
  text('sync-label',isDemo ? '固定演示' : !nextSource ? '等待会话' : syncStatus==='retrying' ? '稍后自动重试' : syncStatus==='error' ? '同步异常' : syncStatus==='updating' ? '正在同步' : syncStatus==='checking' ? '检查中…' : collapsed ? '后台同步' : '自动同步');
  $('sync-label').title=isDemo ? '固定虚构示例，不连接微信或检查真实记录。' : `${sync?.mode==='background' || collapsed ? '收起后' : '展开时'}每 ${sync?.interval_seconds || (collapsed ? 10 : 5)} 秒检查本机记录；${sync?.last_checked_at ? `最后检查 ${displayTime(sync.last_checked_at,true)}；` : ''}微信尚未落盘的消息可能延迟。自动同步不调用模型。`;
  $('sync-label').classList.toggle('error',syncStatus==='error' || syncStatus==='retrying');
  const bindingReason = state.binding?.reason;
  $('binding-notice').hidden = !bindingReason || bindingReason === '请先打开需要分析的微信会话。' || Boolean(nextSource && state.binding?.status !== 'unmatched'); text('binding-notice',bindingReason || '');
  renderConversation(state,changedSource); renderDrafts(state,changedSource);renderAutomatic();
  const placing = state.placement?.status === 'queued';
  const running = state.status?.capture === 'running' || state.status?.ai === 'running' || placing;
  const error = state.status?.capture === 'error' || state.status?.ai === 'error' || state.placement?.status === 'blocked';
  local.busy = running;
  $('app').classList.toggle('working',placing || state.status?.ai==='running');
  $('status-line').classList.toggle('working',running); $('status-line').classList.toggle('error',error);
  $('bookmark-status').classList.toggle('working',running); $('bookmark-status').classList.toggle('error',error);
  const modelConnected = state.model?.connected;
  const placementMessage=state.placement?.status !== 'idle' ? state.placement?.message : '';
  const autoReason=state.automatic?.source_id===local.source && state.automatic?.account_fingerprint===local.account ? state.automatic?.reason : '';
  $('status-line').hidden=!(error || placing || state.status?.ai==='running' || modelConnected===false || autoReason);
  text('status-text',error ? placementMessage || state.status?.last_error || '暂未完成，请重试' : autoReason || (placing ? '正在填入微信…' : state.status?.ai==='running' ? state.status?.message || '正在斟酌…' : modelConnected===false ? '模型暂不可用' : ''));
  if (state.placement?.id && state.placement.id !== local.lastPlacement && ['filled','blocked'].includes(state.placement.status)) {
    local.lastPlacement=state.placement.id;
    toast(state.placement.message);
  }
  const hasSource=Boolean(nextSource);
  const canSummarize=hasSource && ['私聊','群聊','private','contact','friend','group'].includes(state.binding?.kind);
  $('today-menu-item').disabled=!canSummarize || running;
  const todayResult=state.today_summary;
  const hasToday=Boolean(todayResult && todayResult.source_id===nextSource && todayResult.account_fingerprint===local.account);
  $('today-open-menu-item').hidden=!hasToday;
  $('today-open-menu-item').disabled=!hasToday;
  text('today-summary-detail',hasToday ? `${todayResult.date} · ${todayResult.messages} 条可读消息` : '');
  $('suggest-button').disabled=!hasSource || running;
  $('refresh-button').disabled=state.status?.capture==='running';
  $('suggest-button').firstChild.textContent=placing ? '正在填入' : state.status?.ai==='running' ? '正在生成' : '生成建议';
  if ($('contacts-dialog').open) renderContacts();
  if ($('style-dialog').open) {
    const signature=JSON.stringify(state.style || {});
    if (signature!==stylePanel.signature) { stylePanel.signature=signature;void loadStyleProfile(); }
    renderStylePanel();
  }
  if ($('memory-dialog').open) {
    const signature=JSON.stringify(state.memory || {});
    if (signature!==memoryPanel.signature) {memoryPanel.signature=signature;void loadContactMemory();}
    renderMemoryPanel();
  }
  local.revision = state.revision;
}
function renderImagePreview(state) {
  const preview=state.image_preview;
  if (!preview) { closeDialog('image-dialog');$('image-preview').setAttribute('src','');return; }
  if (preview.request===closedImageRequest) return;
  showDialog('image-dialog');
  const image=preview.image;
  const valid=preview.status==='ready' && image?.source_id===state.binding?.source_id &&
    image?.version===state.images?.version && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(image?.data_url || '');
  $('image-preview').hidden=!valid;
  $('image-preview').setAttribute('src',valid ? image.data_url : '');
  text('image-status',preview.status==='loading' ? '正在本地解码…' : valid ? `${image.width} × ${image.height} · ${image.note}` : preview.error || '图片暂时无法打开');
}
$('image-close').addEventListener('click',()=>{closedImageRequest=local.state?.image_preview?.request;closeDialog('image-dialog');$('image-preview').setAttribute('src','');});
$('image-dialog').addEventListener('cancel',()=>{closedImageRequest=local.state?.image_preview?.request;});
async function poll() {
  if (local.polling) return;
  local.polling = true;
  try {
    const state = await invoke('get_state',local.revision);
    if(state?.unchanged) return;
    if (state && (local.state === null || state.revision !== local.revision || state.binding?.source_id !== local.source || (state.account_fingerprint || 'current') !== local.account)) renderState(state);
  } catch(error) {
    if (!local.state) { text('status-text',error.message || '等待连接桌面助手…'); $('status-line').classList.add('error'); $('status-line').hidden=false; }
  } finally { local.polling=false; }
}
async function flushDrafts() {
  clearTimeout(local.draftTimer);
  const source = local.source;
  const key = local.key;
  if (!source || !local.dirty.has(key)) return true;
  preserveConversationUI();
  const draft = {short:$('draft-short').value,full:local.fullDraft};
  return saveConversationDraft(key,source,local.account,draft);
}
function sameDraft(a,b) { return Boolean(a && b && a.short === b.short && a.full === b.full); }
function saveConversationDraft(key,source,account,draft) {
  const previous = local.saving.get(key);
  if (previous && sameDraft(previous.draft,draft)) return previous.promise;
  const job = {draft,promise:null};
  // Serialize each conversation's writes so an earlier slow save cannot land
  // after a newer edit and replace it on disk.
  job.promise = (previous?.promise || Promise.resolve(true)).then(async () => {
    try {
      await invoke('save_drafts',draft.short,draft.full,source,account);
      const current = local.ui.get(key);
      if (sameDraft(current,draft)) local.saved.set(key,draft);
      if (key === local.key && $('draft-short').value === draft.short && local.fullDraft === draft.full) $('draft-save-state').hidden=true;
      return true;
    } catch(error) {
      if (key === local.key) { text('draft-save-state','修改尚未保存，请重试。'); $('draft-save-state').hidden=false; }
      return false;
    }
  });
  local.saving.set(key,job);
  job.promise.then(() => { if (local.saving.get(key) === job) local.saving.delete(key); });
  return job.promise;
}
async function quitAssistant() {
  await pauseAutomatic();
  preserveConversationUI();
  clearTimeout(local.draftTimer);
  const pending=[];
  for (const key of local.dirty) {
    const buffer=local.ui.get(key);
    if (!buffer || sameDraft(local.saved.get(key),buffer)) continue;
    const [account,source]=JSON.parse(key);
    pending.push(saveConversationDraft(key,source,account,{short:buffer.short,full:buffer.full}));
  }
  await Promise.all(pending);
  const unsaved=[...local.dirty].some(key => !sameDraft(local.saved.get(key),local.ui.get(key)));
  if (unsaved) { toast('有草稿尚未保存，窗口已保留。请重试退出，或先复制草稿。'); return; }
  await action('quit');
}
function draftChanged() {
  if (!local.source) return;
  local.dirty.add(local.key); local.saved.delete(local.key); preserveConversationUI();
  $('draft-save-state').hidden=true;
  clearTimeout(local.draftTimer); local.draftTimer=setTimeout(flushDrafts,450);
}
async function startModelAction(method) {
  if (local.busy || !local.source) return;
  if (method === 'suggest_reply') method = 'take_suggestion';
  const key = local.key;
  const source = local.source;
  const args = method === 'take_suggestion' ? [$('goal-input').value,local.supplement,source,local.account] : method === 'summarize_today' ? [source,local.account] : [source];
  local.busy = true;
  $('suggest-button').disabled = true;
  try {
    if (!await flushDrafts()) throw new Error('草稿暂未保存，请先复制或再次编辑后重试。');
    if (key !== local.key) { toast('会话已切换，请在当前会话重新发起分析。'); return; }
    const result = await invoke(method,...args);
    if (result?.consent_required) {
      if (key !== local.key) { toast('会话已切换，请在当前会话重新发起分析。'); return; }
      presentConsent(result,{method,args,source,key});
    } else { if (result?.cached) toast(local.state?.capabilities?.window_interaction === false ? '沿用已有草稿，可复制后发送。' : '沿用已有草稿，正在放入微信输入框。'); await poll(); }
  } catch(error) { toast(error.message || '模型暂不可用，请稍后重试。'); }
  finally {
    local.busy = local.state?.status?.capture === 'running' || local.state?.status?.ai === 'running' || local.state?.placement?.status === 'queued';
    $('suggest-button').disabled = !local.source || local.busy;
  }
}
function presentConsent(result, pending) {
  const consent = result?.consent;
  if (!consent || typeof consent.token !== 'string' || !consent.token || typeof consent.model !== 'string' || !consent.model || typeof consent.base_url !== 'string' || !consent.base_url || typeof consent.source_name !== 'string' || !consent.source_name) {
    throw new Error('未收到完整的授权范围，请重新发起分析。');
  }
  if (pending.method==='summarize_today' && (typeof consent.scope!=='string' || !consent.scope)) throw new Error('未收到当天总结的授权范围，请重试。');
  local.consentAction={...pending,token:consent.token};
  text('consent-source',`「${consent.source_name}」`);
  text('consent-scope',pending.method==='summarize_today' ? consent.scope : '近 7 天、最多 80 条可读消息及回复要求，并参考本会话最多 12 条相关历史原文、8 条已发送改稿与口吻偏好。');
  text('consent-model',consent.model);
  text('consent-address',consent.base_url);
  $('consent-error').hidden=true;
  showDialog('consent-dialog');
}
async function allowConsent() {
  const pending = local.consentAction;
  if (!pending || pending.key !== local.key) { closeDialog('consent-dialog'); return; }
  $('consent-allow').disabled=true;
  try {
    // Fetch before granting so auto-follow cannot silently redirect this click.
    const current = await invoke('get_state');
    if (JSON.stringify([current.account_fingerprint || 'current',current.binding?.source_id]) !== pending.key) {
      renderState(current); closeDialog('consent-dialog'); toast('会话已切换，请重新发起分析。'); return;
    }
    await invoke('grant_consent',pending.source,pending.token);
    const result = await invoke(pending.method,...pending.args);
    if (result?.consent_required) {
      if (pending.key !== local.key) { closeDialog('consent-dialog'); return; }
      presentConsent(result,pending);
      text('consent-error','处理服务已更新，请核对上方的新服务后再确认。'); $('consent-error').hidden=false;
      return;
    }
    local.consentAction=null; closeDialog('consent-dialog');
    if (result?.cached) toast(local.state?.capabilities?.window_interaction === false ? '沿用已有草稿，可复制后发送。' : '沿用已有草稿，正在放入微信输入框。');
    await poll();
  } catch(error) { text('consent-error',error.message || '未能完成授权，请稍后重试。'); $('consent-error').hidden=false; }
  finally { $('consent-allow').disabled=false; }
}
async function putDraft(kind) {
  const key=local.key, source=local.source, account=local.account;
  const value=$(`draft-${kind}`).value;
  if (!value.trim()) { toast('这份草稿还是空的。'); return; }
  if (!await flushDrafts()) { toast('草稿暂未保存，请保留修改后重试。'); return; }
  if (key !== local.key || value !== $(`draft-${kind}`).value) { toast('会话或草稿已变化，请重新操作。'); return; }
  await action('put_draft',value,source,account);
}
$('choose-contact').addEventListener('click',() => { $('contact-search').value='';renderContacts();showDialog('contacts-dialog');$('contact-search').focus(); });
$('contact-search').addEventListener('input',renderContacts);
$('follow-button').addEventListener('click',async()=>{await flushDrafts();await action('set_following',local.state?.binding?.mode==='pinned');});
$('refresh-button').addEventListener('click',()=>action('refresh',local.source));
$('collapse-button').addEventListener('click',async()=>{if(await flushDrafts())await action('collapse');});
$('minimize-button').addEventListener('click',async()=>{if(await flushDrafts())await action('minimize');});
$('expand-button').addEventListener('click',()=>action('expand'));
$('quit-button').addEventListener('click',quitAssistant);
$('suggest-button').addEventListener('click',async()=>{await pauseAutomatic();startModelAction('suggest_reply');});
$('consent-allow').addEventListener('click',allowConsent);
$('consent-cancel').addEventListener('click',()=>{local.consentAction=null;closeDialog('consent-dialog');});
$('consent-dialog').addEventListener('cancel',()=>{local.consentAction=null;});
$('mode-suggest').addEventListener('click',chooseSuggestionMode);
$('mode-auto').addEventListener('click',openAutoPanel);
$('quick-scene').addEventListener('change',selectQuickScene);
$('goal-input').addEventListener('input',()=>{pauseAutomatic();preserveConversationUI();renderReplyMeta(local.state);});
$('draft-short').addEventListener('input',()=>{pauseAutomatic();draftChanged();});
$('copy-short').addEventListener('click',()=>{const value=$('draft-short').value;if(value.trim())action('copy_text',value);});
$('menu-button').addEventListener('click',()=>showDialog('menu-dialog'));
$('style-menu-item').addEventListener('click',openStylePanel);
$('memory-menu-item').addEventListener('click',openMemoryPanel);
$('auto-menu-item').addEventListener('click',openAutoPanel);
$('auto-toggle').addEventListener('click',toggleAutoChat);
$('auto-pause').addEventListener('click',pauseAutomatic);
$('memory-notes').addEventListener('input',()=>memoryChanged('notes'));
$('memory-enabled').addEventListener('change',()=>memoryChanged('enabled'));
$('memory-feedback').addEventListener('change',()=>memoryChanged('feedback_enabled'));
$('memory-save').addEventListener('click',saveContactMemory);
$('memory-clear').addEventListener('click',()=>mutateMemory('clear'));
$('workbench-menu-item').addEventListener('click',async()=>{closeDialog('menu-dialog');await action('open_dashboard');});
$('author-workbench-menu-item').addEventListener('click',async()=>{closeDialog('menu-dialog');await action('open_workbench');});
$('today-menu-item').addEventListener('click',()=>{closeDialog('menu-dialog');startModelAction('summarize_today');});
$('today-open-menu-item').addEventListener('click',async()=>{closeDialog('menu-dialog');await action('open_today_summary',local.source,local.account);});
$('style-notes').addEventListener('input',()=>styleChanged('notes'));
$('style-enabled').addEventListener('change',()=>styleChanged('enabled'));
$('style-save').addEventListener('click',saveStyleProfile);
$('style-learn').addEventListener('click',learnStyleProfile);
$('reply-scene').addEventListener('change',setReplyScene);
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>closeDialog(button.dataset.close)));

const demoFriend = {
  "context": {
    "source": {
      "id": "C_bbbf7a64",
      "name": "小林",
      "kind": "私聊"
    },
    "messages": [
      {
        "uid": "W_871e1e714f72d7d0d8c371202c27136c",
        "source_id": "C_bbbf7a64",
        "time": "2026-10-01T09:44:45+08:00",
        "sender": "我",
        "is_self": true,
        "kind": "text",
        "text": "行，今天先这样。"
      },
      {
        "uid": "W_d6cb2c0ef48c92455dbab55cb67bc4e1",
        "source_id": "C_bbbf7a64",
        "time": "2026-10-01T09:45:45+08:00",
        "sender": "小林",
        "is_self": false,
        "kind": "text",
        "text": "下班了吗"
      },
      {
        "uid": "W_170d7abb35ee8864f99ba191df5491b1",
        "source_id": "C_bbbf7a64",
        "time": "2026-10-01T09:46:45+08:00",
        "sender": "我",
        "is_self": true,
        "kind": "text",
        "text": "嗯，刚结束。"
      },
      {
        "uid": "W_4a6df882ed6766c6968a0118a0f7b2f3",
        "source_id": "C_bbbf7a64",
        "time": "2026-10-01T09:47:45+08:00",
        "sender": "小林",
        "is_self": false,
        "kind": "text",
        "text": "今天上班脑子完全不在线，人在工位魂在放假。"
      }
    ],
    "snapshot_at": "2026-10-01T09:48:45+08:00",
    "start": "2026-09-25T00:00:00+08:00",
    "end": "2026-10-01T09:48:45+08:00",
    "count": 4,
    "total": 4,
    "truncated": false,
    "context_hash": "2573a103b1e2ba4852c57fc2c06534c34a3c825cf286b7156e3625c87034a35d"
  },
  "reply": {
    "source_id": "C_bbbf7a64",
    "context_hash": "2573a103b1e2ba4852c57fc2c06534c34a3c825cf286b7156e3625c87034a35d",
    "generated_at": "2026-10-01T09:54:26+08:00",
    "facts": [
      {
        "text": "小林表示自己今天上班脑子不在线，人在工位魂已经放假。",
        "evidence_uids": [
          "W_4a6df882ed6766c6968a0118a0f7b2f3"
        ]
      }
    ],
    "inferences": [
      {
        "text": "推测小林是在轻松吐槽假期的上班状态，并非遇到严肃困难，适合顺着开玩笑接话。",
        "evidence_uids": [
          "W_4a6df882ed6766c6968a0118a0f7b2f3"
        ]
      }
    ],
    "questions": [],
    "drafts": {
      "short": "能把肉身按在工位上，已经是对工作最大的尊重了。",
      "full": "工位只是个肉身寄存点罢了，魂提前放假很合理，今天属于纯物理出勤。"
    },
    "supplement_used": false,
    "notes": "草稿依据本次选定会话上下文生成，未发送微信消息。",
    "input": {
      "goal": "朋友间日常闲聊，接他这句话。",
      "supplement": ""
    }
  },
  "goal": "朋友间日常闲聊，接他这句话。",
  "model": "gemini-3.8-flash-n"
};
let demoState=null;
const demoRecords=new Map();
let demoRevision=0;
const demoStyleProfile={revision:1,enabled:true,learned_at:'2026-10-01T09:48:45+08:00',sample_count:88,range:{start:'2026-09-25T00:00:00+08:00',end:'2026-10-01T09:48:45+08:00',snapshot_at:'2026-10-01T09:48:45+08:00'},profiles:{daily:{sample_count:52,traits:{sentence_length:'short',tone:'playful',humor:'light',emoji:'occasional',punctuation:'natural'}},work:{sample_count:24,traits:{sentence_length:'mixed',tone:'direct',humor:'none',emoji:'none',punctuation:'full'}},romance:{sample_count:12,traits:{sentence_length:'short',tone:'warm',humor:'light',emoji:'occasional',punctuation:'natural'}}},notes:'示例偏好：日常轻松接话，工作先说清重点。'};
async function demoAction(method,...args) {
  if (!demoState) {
    const fixture=structuredClone(demoFriend);
    const source=fixture.context.source;
    demoState={revision:++demoRevision,account_fingerprint:'synthetic-demo-account',binding:{source_id:source.id,name:source.name,kind:'contact',mode:'pinned',status:'ready',reason:''},sources:[{id:source.id,name:source.name,kind:'contact'},{id:'demo_group',name:'周末读书小组',kind:'group'}],context:fixture.context,summary:null,reply:fixture.reply,drafts:{...fixture.reply.drafts},status:{capture:'idle',ai:'idle',message:'虚构对话 · 实际 Gemini 输出示例',last_error:''},model:{model:`${fixture.model} · 已生成示例`,base_url:'演示不连接模型服务',consented:true,connected:true},settings:{startup:false,collapsed:false}};
    demoRecords.set(source.id,demoState);
  }
  demoState.style={status:'idle',message:'虚构口吻档案，仅供界面演示。',revision:demoStyleProfile.revision,sample_count:demoStyleProfile.sample_count,account_fingerprint:demoState.account_fingerprint};
  demoState.reply_scene=demoState.reply_scene || 'auto';
  if (method==='get_state') return structuredClone(demoState);
  if (method==='summarize_today' || method==='open_today_summary') return {ok:false,message:'请在桌面程序中总结当天聊天；虚构演示不读取消息或调用模型。'};
  if (method==='set_auto_chat' || method==='pause_auto_chat') return {ok:false,message:'演示不连接微信，不启用自动聊天或发送消息。'};
  const memoryMethods=['get_contact_memory','save_contact_memory','update_memory_item','clear_contact_memory'];
  if (memoryMethods.includes(method)) {
    if (!demoState.memoryData) demoState.memoryData={revision:0,settings_revision:0,enabled:true,feedback_enabled:true,notes:'',snapshot_at:demoState.context?.snapshot_at,messages:list(demoState.context?.messages).filter(row=>row.kind==='text'),pins:[],examples:[]};
    const value=demoState.memoryData;
    const publicMemory=()=>({ok:true,account_fingerprint:demoState.account_fingerprint,source_id:demoState.binding.source_id,source_name:demoState.binding.name,memory:structuredClone(value)});
    if (method==='get_contact_memory') {
      if (args[0]!==demoState.binding.source_id || args[1]!==demoState.account_fingerprint) return {ok:false,message:'演示会话已变化。'};
      return publicMemory();
    }
    const offset=method==='update_memory_item' ? 2 : method==='save_contact_memory' ? 1 : 0;
    if (args[offset]!==demoState.binding.source_id || args[offset+1]!==demoState.account_fingerprint || args[offset+2]!==value.settings_revision) return {ok:false,message:'演示会话或设置已变化。'};
    if (method==='save_contact_memory') Object.assign(value,args[0]);
    if (method==='clear_contact_memory') Object.assign(value,{messages:[],pins:[],examples:[]});
    if (method==='update_memory_item') {
      const [uid,mode]=args;
      value.pins=value.pins.filter(key=>key!==uid);
      if (mode==='pin') value.pins.push(uid);
      if (mode==='forget') value.messages=value.messages.filter(row=>row.uid!==uid);
      if (mode==='forget_example') value.examples=value.examples.filter(entry=>entry.message.uid!==uid);
    }
    value.revision++;value.settings_revision++;
    demoState.memory={source_id:demoState.binding.source_id,revision:value.revision,settings_revision:value.settings_revision,message_count:value.messages.length,example_count:value.examples.length,error:''};
    demoState.revision=++demoRevision;
    return publicMemory();
  }
  if (method==='get_style_profile') return {ok:true,account_fingerprint:demoState.account_fingerprint,profile:structuredClone(demoStyleProfile),scene:demoState.reply_scene,learning:{status:'idle',message:'虚构口吻档案，仅供界面演示。'}};
  if (method==='learn_style') return {ok:false,message:'演示不联网，不读取真实消息，也不会生成新的学习结果。'};
  if (method==='save_style_profile') {
    if (args[1]!==demoState.account_fingerprint || args[2]!==demoStyleProfile.revision) return {ok:false,message:'演示档案已变化，请重试。'};
    Object.assign(demoStyleProfile,args[0],{revision:demoStyleProfile.revision+1});
    for (const record of demoRecords.values()) record.style={...demoState.style,revision:demoStyleProfile.revision};
  }
  if (method==='set_reply_scene') {
    if (args[1]!==demoState.binding.source_id || args[2]!==demoState.account_fingerprint || !replyScenes[args[0]]) return {ok:false,message:'演示会话已变化，请重新选择。'};
    demoState.reply_scene=args[0];
  }
  if (method==='suggest_reply' || method==='take_suggestion' || method==='update_analysis') {
    toast('这是已生成的 Gemini 示例。演示不联网；可体验改写和复制。');
    return {ok:true};
  }
  if (method==='put_draft') return {ok:false,message:'演示不连接微信；桌面助手会把草稿放入微信输入框。'};
  if (method==='minimize') { toast('任务栏最小化请在桌面版体验。'); return {ok:true}; }
  if (method==='set_following') demoState.binding.mode=args[0] ? 'auto' : 'pinned';
  if (method==='select_conversation') {
    const source=demoState.sources.find(item => item.id===args[0]);
    if (source) {
      if (!demoRecords.has(source.id)) demoRecords.set(source.id,{...structuredClone(demoState),memoryData:null,memory:null,binding:{...source,source_id:source.id,mode:'pinned',status:'ready',reason:''},context:{messages:[],count:0,total:0,snapshot_at:null,context_hash:'empty-demo'},summary:null,reply:null,drafts:{short:'',full:''}});
      demoState=demoRecords.get(source.id);
      demoState.binding.mode='pinned';
    }
  }
  if (method==='collapse') demoState.settings.collapsed=true;
  if (method==='expand') demoState.settings.collapsed=false;
  if (method==='set_startup') demoState.settings.startup=args[0];
  if (method==='save_drafts') {
    const record=demoRecords.get(args[2] || demoState.binding.source_id);
    if (record) record.drafts={short:args[0],full:args[1]};
  }
  if (method==='copy_text') { if (navigator.clipboard) await navigator.clipboard.writeText(args[0]); else return {ok:false,message:'演示浏览器未开放剪贴板。'}; }
  if (method==='open_dashboard' || method==='quit') toast('演示模式：请在桌面助手中使用此功能。');
  demoState.revision=++demoRevision;
  return {ok:true};
}
window.addEventListener('pywebviewready',poll);
window.addEventListener('assistant-request-close',quitAssistant);
if (isDemo && !bridge() && document.body) document.body.classList.add('demo-preview');
poll();
setInterval(poll,700);
