import {TraceModel,statusLabel,toolSummary} from './trace-model.mjs';
import {markdown} from './markdown.mjs';
import {UNKNOWN,breakdown,buildQuery,describeGaps,formatDuration,formatPercent,formatTimestamp,sortRuns,stopReasonLabel,stopReasonTone,summarizeReasons} from './analysis-model.mjs';

const $=id=>document.getElementById(id);
const el=(tag,className,text)=>{const n=document.createElement(tag);if(className)n.className=className;if(text!==undefined)n.textContent=text;return n;};
const button=(text,fn,className='')=>{const n=el('button',className,text);n.type='button';n.addEventListener('click',fn);return n;};
const uuid=()=>globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const storage={get(key){try{return localStorage.getItem(key);}catch{return null;}},set(key,value){try{localStorage.setItem(key,value);}catch{}}};
const terminal=new Set(['completed','failed','cancelled','failed','limit_reached','blocked']);
const entries=new Map();
let sessionId=null,runId=null,rootRunId=null,activeRunId=null,ws=null,subscriptionId=null,online=false;
let panel='activity',selectedTool=null,detailTab='output',detailData=null,fullOutput=false,detailVersion=0;
let following=true,outputFollowing=true,newEvents=0,visibleCount=150,scheduled=false,rendering=false;
let sessionRows=[],sessionLimit=50,runRows=[],runLimit=50,requestId=null,sending=false;
let refreshTimer=null,reconnectTimer=null,inspectorType='tool',previousFocus=null;
let approvalIds=new Set(),rawEvents=null,rawVersion=0,selecting=false;
const openDetails=new Set();
const drafts=new Map();
const selectedSkills=new Set();
function entry(){if(!entries.has(sessionId))entries.set(sessionId,{model:new TraceModel(),cursor:null,synced:false});return entries.get(sessionId);}
const model=()=>entry().model;
const current=()=>model().runs.get(runId);
const fmt=n=>n===null||n===undefined?'未记录':Number(n).toLocaleString('zh-CN');
const shortDate=value=>value?new Date(value).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
const seconds=(a,b)=>a&&b?Math.max(0,(new Date(b)-new Date(a))/1000):null;
const duration=n=>n===null?'未记录':n<60?`${n.toFixed(1)} s`:`${Math.floor(n/60)}m ${Math.floor(n%60)}s`;
function alert(message){$('alert').hidden=!message;$('alert').textContent=message||'';}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  const data=await response.json();
  if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));error.status=response.status;error.code=data.code;throw error;}
  return data;
}
const post=(path,data={})=>api(path,{method:'POST',body:JSON.stringify(data)});
function safe(action){return async()=>{try{await action();}catch(e){alert(e.message);}};}
function schedule(){if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;render();});}
function queueRefresh(){if(refreshTimer)return;refreshTimer=setTimeout(()=>{refreshTimer=null;refresh().catch(e=>alert(e.message));},350);}

async function fetchSessions(){
  sessionRows=await pages('/api/sessions',sessionLimit);
  renderSessions();
}
async function pages(path,count){
  const rows=[];
  for(let offset=0;offset<count;offset+=500){
    const limit=Math.min(500,count-offset);
    const page=await api(`${path}?limit=${limit}&offset=${offset}`);rows.push(...page);
    if(page.length<limit)break;
  }
  return rows;
}
function renderSessions(){
  $('sessions').replaceChildren(...sessionRows.map(row=>{
    const b=button('',safe(()=>selectSession(row.id)),'session');b.setAttribute('aria-pressed',String(row.id===sessionId));
    b.append(el('span','session-title',row.title||'新会话'));
    const meta=el('span','session-meta');meta.append(el('span','',row.active_run_id?'执行中':statusLabel(row.status||'unknown')),el('span','',shortDate(row.updated_at)));b.append(meta);return b;
  }));
  $('moreSessions').hidden=sessionRows.length<sessionLimit;
}
async function selectSession(id){
  if(sessionId)drafts.set(sessionId,$('prompt').value);
  sessionId=id;storage.set('bot.session',id);runId=null;rootRunId=null;activeRunId=null;visibleCount=150;following=true;
  selectedTool=null;detailVersion++;rawVersion++;$('inspector').hidden=true;runRows=[];runLimit=50;approvalIds=new Set();
  $('prompt').value=drafts.get(id)||'';
  $('sidebar').classList.remove('open');$('navToggle').setAttribute('aria-expanded','false');
  alert('');subscribe();await refresh();renderSessions();
}
function subscribe(reset=false){
  if(!sessionId||ws?.readyState!==WebSocket.OPEN)return;
  if(reset)entries.set(sessionId,{model:new TraceModel(),cursor:null,synced:false});
  subscriptionId=uuid();entry().synced=false;
  ws.send(JSON.stringify({type:'subscribe',session_id:sessionId,subscription_id:subscriptionId,cursor:entry().cursor}));
}
function connect(){
  clearTimeout(reconnectTimer);
  const socket=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws`);ws=socket;
  socket.onopen=()=>{online=true;$('connection').textContent='实时已连接';$('connection').className='connection online';subscribe();renderComposer();};
  socket.onmessage=event=>{
    try{
      const message=JSON.parse(event.data);
      if(message.type==='error'){
        if(message.code==='cursor_expired'){subscribe(true);return;}
        alert(message.message);return;
      }
      if(message.subscription_id!==subscriptionId)return;
      if(message.type==='snapshot'){
        message.events.forEach(ingest);schedule();
      }else if(message.type==='snapshot_complete'){
        entry().cursor=message.cursor;entry().synced=true;queueRefresh();
      }else if(message.type==='event'){
        if(message.event.type==='resync_required'){subscribe();return;}
        ingest(message.event);
        if(entry().synced&&message.event.cursor)entry().cursor=message.event.cursor;
        schedule();
      }
    }catch(e){alert(`事件读取失败：${e.message}`);}
  };
  socket.onclose=()=>{if(socket!==ws)return;online=false;$('connection').textContent='连接已断开 · 正在恢复';$('connection').className='connection offline';renderComposer();reconnectTimer=setTimeout(connect,2000);};
  socket.onerror=()=>socket.close();
}
function ingest(event){
  if(!model().apply(event))return;
  if(event.type==='run.started'&&event.session_id===sessionId){
    activeRunId=event.run_id;
    if(!runId||runId===event.run_id){runId=event.run_id;rootRunId=runId;}
  }
  if(!following)newEvents++;
  if(['run.started','run.finished','run.artifacts.updated','approval.requested','approval.resolved'].includes(event.type)||event.type.startsWith('subagent.'))queueRefresh();
  if(event.type==='run.finished'&&event.run_id===activeRunId)activeRunId=null;
}
async function refresh(){
  if(!sessionId)return;
  const selected=sessionId;
  const [rows,sessions,approvals]=await Promise.all([pages(`/api/sessions/${selected}/runs`,runLimit),pages('/api/sessions',sessionLimit),api(`/api/sessions/${selected}/approvals`)]);
  if(sessionId!==selected)return;
  runRows=rows;sessionRows=sessions;
  approvalIds=new Set(approvals.map(a=>a.approval_id));
  rows.forEach(row=>model().hydrate(row));
  const active=sessions.find(row=>row.id===selected)?.active_run_id;
  activeRunId=active||rows.find(r=>r.controllable)?.id||null;
  if(!runId){runId=activeRunId||rows[0]?.id||null;rootRunId=runId;}
  renderRunPicker();renderSessions();schedule();
  if(panel!=='activity')await renderSecondary();
  if(selectedTool&&inspectorType==='tool'&&detailTab==='output'&&!fullOutput)await loadToolDetail();
}
function renderRunPicker(){
  const options=[];
  if(!runRows.length)options.push(el('option','','新任务'));
  if(runId&&!runRows.some(r=>r.id===runId)){const option=el('option','','子任务 / '+runId.slice(0,8));option.value=runId;options.push(option);}
  for(const row of runRows){const option=el('option','',`${shortDate(row.started_at)} · ${statusLabel(row.status)} · ${(row.prompt||'任务').slice(0,24)}`);option.value=row.id;options.push(option);}
  if(runRows.length===runLimit){const option=el('option','','加载更早的任务…');option.value='more';options.push(option);}
  $('runPicker').replaceChildren(...options);$('runPicker').value=runId||'';
}
function render(){renderMeta();renderPlan();renderActivity();renderComposer();if(selectedTool&&inspectorType==='tool')renderToolDetail();}
function renderMeta(){
  const run=current();$('taskTitle').textContent=run?.prompt||'开始一个任务';
  if(!run){$('runMeta').replaceChildren();return;}
  const state=run.cancelling?'cancelling':run.status;
  const parts=[el('span',`status-chip ${state}`,statusLabel(state))];
  if(run.started_at)parts.push(el('span','',`${terminal.has(run.status)?'耗时':'已运行'} ${duration(seconds(run.started_at,run.completed_at||new Date().toISOString()))}`));
  parts.push(el('span','',`输入 ${fmt(run.usage.input_tokens)} / 输出 ${fmt(run.usage.output_tokens)} tokens`));
  parts.push(el('span','',run.usage.cost_usd===null?'费用未记录':`$${Number(run.usage.cost_usd).toFixed(4)}`));
  if(run.unknownAttempts?.size)parts.push(el('span','muted',`${run.unknownAttempts.size} 次请求用量未确认，费用仅为已知部分`));
  if(runId!==rootRunId&&rootRunId)parts.push(button('返回主任务',()=>selectRun(rootRunId),'quiet'));
  if(run.status==='running'&&!run.live&&!activeRunId)parts.push(el('span','muted','历史运行状态，执行端未确认'));
  $('runMeta').replaceChildren(...parts);
}
function renderPlan(){
  const plan=current()?.plan;const items=Array.isArray(plan?.items)?plan.items:Array.isArray(plan?.plan)?plan.plan:[];
  $('planPanel').hidden=!items.length;
  $('planSummary').textContent=`任务计划 · ${items.filter(i=>i.status==='completed').length}/${items.length} 完成`;
  $('planExplanation').textContent=plan?.explanation||'';
  $('planItems').replaceChildren(...items.map(item=>{const li=el('li',item.status);li.append(el('span','',item.content||item.step),el('span','',({completed:'已完成',in_progress:'进行中',pending:'待开始'})[item.status]||item.status));return li;}));
}
function diagnostic(node){
  const details=el('details','system-message');details.open=openDetails.has(node.event.id);
  details.append(el('summary','',node.content),el('pre','',JSON.stringify(node.event.payload,null,2)));
  details.addEventListener('toggle',()=>details.open?openDetails.add(node.event.id):openDetails.delete(node.event.id));return details;
}
function renderActivity(){
  const scroller=$('activityScroller');const scrollTop=scroller.scrollTop;
  const focusedKey=document.activeElement?.dataset?.toolKey;
  const nodes=model().ordered(runId).filter(n=>n.kind!=='system'||!['model.response','context.packed','memory.routing.decided'].includes(n.event.type));
  const shown=nodes.slice(-visibleCount);
  rendering=true;
  $('activity').replaceChildren(...shown.map(node=>{
    if(node.kind==='tool')return renderToolCard(model().tools.get(node.toolKey));
    if(node.kind==='child'){
      const child=model().children.get(node.taskId);
      return button(`子任务：${child.objective||child.agent||'任务'} · ${statusLabel(child.status)}`,safe(async()=>{
        const selected=runId;const children=await api(`/api/runs/${selected}/children`);if(runId!==selected)return;
        const task=children.find(c=>c.id===node.taskId);const run=task?.runs[0];
        if(!run){alert('子任务尚未开始执行。');return;}
        model().hydrate(run);runId=run.id;renderRunPicker();render();
      }),'child-card');
    }
    if(node.kind==='system')return diagnostic(node);
    const row=el('article',`message ${node.kind}${node.discarded?' discarded':''}`);
    const author=el('div','author',node.kind==='user'?'你':'Bot');
    if(node.discarded)author.append(el('span','','该响应已重试'));
    if(node.delivery)author.append(el('span','delivery',node.delivery==='received'?'已接收':'已发送 · 等待 Bot 接收'));
    row.append(author);
    if(node.kind==='assistant')row.append(markdown(node.content));else row.append(el('p','',node.content));
    if(node.reasoning){const details=el('details');details.open=openDetails.has(node.event.id);details.append(el('summary','','模型返回的推理记录'),el('pre','',node.reasoning));details.addEventListener('toggle',()=>details.open?openDetails.add(node.event.id):openDetails.delete(node.event.id));row.append(details);}
    return row;
  }));
  $('moreActivity').hidden=nodes.length<=visibleCount;$('emptyState').hidden=nodes.length>0;
  if(following){scroller.scrollTop=scroller.scrollHeight;newEvents=0;}else scroller.scrollTop=scrollTop;
  $('followLatest').hidden=following;$('followLatest').textContent=newEvents?`${newEvents} 条新动态 · 回到最新`:'回到最新';
  if(focusedKey)[...document.querySelectorAll('.tool-button')].find(b=>b.dataset.toolKey===focusedKey)?.focus({preventScroll:true});
  rendering=false;
}
function renderToolCard(tool){
  const status=model().toolStatus(tool);
  const card=el('article','tool-card');
  const b=button('',()=>inspectTool(tool.key),'tool-button');b.setAttribute('aria-pressed',String(selectedTool===tool.key&&!$('inspector').hidden));
  b.dataset.toolKey=tool.key;
  const left=el('span');left.append(el('span','tool-title',toolSummary(tool)),el('span','tool-name',tool.name));
  const right=el('span',`tool-state ${status}`,tool.completed_at&&tool.status==='running'&&status==='running'?'进程运行中':statusLabel(status));
  right.append(el('span','tool-name',duration(seconds(tool.started_at,tool.completed_at||new Date().toISOString()))));b.append(left,right);card.append(b);
  const excerpt=(tool.stderr||tool.stdout||tool.error||tool.output).replace(/\[完整内容已持久化；context_ref=[\s\S]*$/, '').trim();
  if(excerpt)card.append(el('div','tool-excerpt',String(excerpt).trim().split('\n').slice(-2).join('\n').slice(-240)));
  if(status==='awaiting_approval'){
    const controls=el('div','approval-actions');controls.append(el('div','',tool.reason||'需要确认这项操作'));
    if(approvalIds.has(tool.approval_id)){const buttons=el('div','buttons');buttons.append(button('允许一次',safe(()=>approve(tool,true)),'primary'),button('拒绝',safe(()=>approve(tool,false)),'danger'));controls.append(buttons);}else controls.append(el('span','muted','审批当前不可处理，正在等待执行端确认；历史请求可能已失效。'));
    card.append(controls);
  }
  return card;
}
async function approve(tool,approved){
  await post(`/api/approvals/${encodeURIComponent(tool.approval_id)}`,{session_id:tool.session_id,run_id:tool.run_id,approved});
  $('sendStatus').textContent='审批响应已发送，等待执行端确认';
}
function renderComposer(){
  const active=activeRunId&&model().runs.get(activeRunId);const running=!!activeRunId;
  const stopping=active?.cancelling;
  $('promptLabel').textContent=running?(runId===activeRunId?'运行中也可以补充要求':'将补充当前会话中正在执行的任务'):'描述任务';
  $('send').textContent=sending?'发送中…':running?'发送补充':'开始任务';
  $('prompt').disabled=selecting;
  $('send').disabled=sending||selecting||!online||stopping||!sessionId;
  $('stop').hidden=!running;$('stop').disabled=stopping||!online||active?.status==='starting';$('stop').textContent=stopping?'正在停止…':'停止任务';
  $('compact').disabled=!sessionId||!online;
}
async function sendPrompt(){
  if(sending)return;const text=$('prompt').value.trim();if(!text)return;
  const sendingSession=sessionId;requestId ||= uuid();sending=true;renderComposer();alert('');
  try{
    if(activeRunId){await post(`/api/runs/${activeRunId}/steer`,{text,message_id:requestId});$('sendStatus').textContent='已发送，等待 Bot 接收';}
    else{
      const result=await post(`/api/sessions/${sessionId}/runs`,{prompt:text,skills:[...selectedSkills],request_id:requestId});
      if(sessionId===sendingSession){activeRunId=result.run_id;runId=result.run_id;rootRunId=runId;const run=model().run(runId,sessionId);run.prompt=text;if(run.status==='unknown')run.status='starting';following=true;panel='activity';switchPanel('activity');}
      $('sendStatus').textContent='任务已提交';
    }
    if(sessionId===sendingSession&&$('prompt').value.trim()===text)$('prompt').value='';
    drafts.set(sendingSession,'');requestId=null;queueRefresh();
  }catch(e){alert(e.message);$('sendStatus').textContent='发送未确认，可重试；相同请求不会重复启动任务。';}
  finally{sending=false;renderComposer();}
}
async function stopRun(){
  if(!activeRunId)return;const target=activeRunId;
  await post(`/api/runs/${target}/cancel`);
  const run=model().runs.get(target);if(run)run.cancelling=true;
  $('sendStatus').textContent='正在停止当前任务、所属进程及子任务';renderMeta();renderComposer();
}
function openInspector(kind,title){
  detailVersion++;rawVersion++;
  previousFocus=document.activeElement;$('inspector').hidden=false;inspectorType=kind;
  $('detailKind').textContent={tool:'工具详情',artifact:'文件产物',context:'上下文版本'}[kind]||'执行详情';
  $('detailTitle').textContent=title;
  $('detailTabs').hidden=kind!=='tool';
  $('closeInspector').focus();
}
function closeInspector(){
  const key=selectedTool;$('inspector').hidden=true;selectedTool=null;detailVersion++;rawVersion++;renderActivity();
  const target=[...document.querySelectorAll('.tool-button')].find(b=>b.dataset.toolKey===key);
  (target||(previousFocus?.isConnected?previousFocus:$('prompt'))).focus({preventScroll:true});
}
async function inspectTool(key){
  selectedTool=key;detailTab='output';detailData=null;fullOutput=false;outputFollowing=true;rawEvents=null;
  const tool=model().tools.get(key);openInspector('tool',tool.name);renderToolDetail();renderActivity();
  try{await loadToolDetail();}catch(e){$('detailActions').replaceChildren(el('span','output-warning',e.message));}
}
async function loadToolDetail(){
  const key=selectedTool;if(!key)return;const tool=model().tools.get(key);if(!tool)return;
  const version=++detailVersion;
  const data=await api(`/api/runs/${encodeURIComponent(tool.run_id)}/tools/${encodeURIComponent(tool.id)}`);
  if(selectedTool!==key||version!==detailVersion||inspectorType!=='tool')return;
  detailData=data;renderToolDetail();
}
async function loadRawEvents(more=false){
  const tool=model().tools.get(selectedTool);if(!tool)return;
  const key=selectedTool,version=++rawVersion;
  const params=new URLSearchParams({limit:'50'});
  if(more&&rawEvents){params.set('cursor',rawEvents.cursor);params.set('through',rawEvents.through);}
  const data=await api(`/api/runs/${tool.run_id}/tools/${encodeURIComponent(tool.id)}/events?${params}`);
  if(key!==selectedTool||version!==rawVersion||inspectorType!=='tool')return;
  rawEvents={...data,events:more?[...rawEvents.events,...data.events]:data.events};renderToolDetail();
}
function renderToolDetail(){
  const tool=model().tools.get(selectedTool);if(!tool)return;
  const body=$('detailBody');const top=body.scrollTop;
  $('detailTitle').textContent=tool.name;
  const process=model().processFor(tool);
  const meta=[statusLabel(model().toolStatus(tool)),`调用 ${duration(seconds(tool.started_at,tool.completed_at))}`,tool.id];
  if(tool.started_at&&tool.requested_at)meta.push(`开始前等待 ${duration(seconds(tool.requested_at,tool.started_at))}`);
  if((process?.returncode??tool.returncode)!==undefined)meta.push(`退出码 ${process?.returncode??tool.returncode}`);
  if(process){meta.push(`进程 ${process.id}`);meta.push(`进程总耗时 ${duration(process.elapsed_seconds??null)}`);}
  $('detailMeta').replaceChildren(...meta.map(text=>el('span','',text)));
  document.querySelectorAll('[data-detail]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.detail===detailTab)));
  const parts=[];const actions=[];
  if(detailTab==='arguments')parts.push(el('pre','',JSON.stringify(tool.arguments,null,2)));
  else if(detailTab==='events'){
    parts.push(el('p','muted','持久化调用事件 · 包含流式输出碎片'),el('pre','',JSON.stringify(rawEvents?.events||detailData?.events||tool.events,null,2)));
    actions.push(button('重新读取事件',safe(()=>loadRawEvents()),'quiet'));
    if(rawEvents?.has_more)actions.push(button('加载更多事件',safe(()=>loadRawEvents(true)),'quiet'));
  }
  else{
    if(tool.error)parts.push(el('p','output-warning',tool.error));
    if(detailData?.source_truncated||tool.truncated)parts.push(el('p','output-warning','源输出已截断；留存全文也可能不包含最初的全部输出。'));
    if(fullOutput&&detailData){parts.push(el('p','output-label',detailData.full_output_available?'留存输出':'仅有输出摘要'),el('pre','',detailData.output.content||'没有记录输出'));}
    else if(process?.stdout||process?.stderr){
      parts.push(el('p','output-label','进程输出 · 独立于 Bot 轮询持续更新'));
      if(process.outputChars>process.stdout.length+process.stderr.length)parts.push(el('p','output-warning','当前只展示最近的进程日志；更早记录可在上下文页的完整事件中分页读取。'));
      for(const stream of ['stdout','stderr'])if(process[stream])parts.push(el('p','output-label',stream),el('pre','',process[stream]));
    }else if(tool.stdout||tool.stderr){
      for(const stream of ['stdout','stderr'])if(tool[stream])parts.push(el('p','output-label',stream),el('pre','',tool[stream]));
      if(tool.outputChars>tool.stdout.length+tool.stderr.length)parts.unshift(el('p','output-warning','当前展示最近输出，可查看留存全文。'));
    }else parts.push(el('pre','',tool.output||detailData?.output?.content||(tool.completed_at?'该调用没有记录文本输出':'等待工具输出…')));
    if(tool.completed_at&&!fullOutput)actions.push(button('查看留存全文',safe(async()=>{if(!detailData)await loadToolDetail();fullOutput=true;renderToolDetail();}),'quiet'));
    if(fullOutput&&detailData&&!detailData.output.eof)actions.push(button('加载更多输出',safe(async()=>{
      const key=selectedTool,version=detailVersion,offset=detailData.output.next_offset;const data=await api(`/api/runs/${tool.run_id}/tools/${encodeURIComponent(tool.id)}?offset=${offset}`);
      if(selectedTool!==key||version!==detailVersion||detailData.output.next_offset!==offset)return;detailData.output={...data.output,content:detailData.output.content+data.output.content};renderToolDetail();
    }),'quiet'));
    if(process){
      const links=el('div');links.append(el('p','output-label',`同一进程的 ${process.calls.length} 次调用`));
      for(const call of process.calls)links.append(button(`${call.name} · ${call.id}`,()=>inspectTool(call.key),'quiet'));
      parts.push(links);
    }
  }
  if(!outputFollowing&&detailTab==='output')actions.push(button('回到最新输出',()=>{outputFollowing=true;renderToolDetail();},'quiet'));
  body.replaceChildren(...parts);$('detailActions').replaceChildren(...actions);
  if(outputFollowing&&detailTab==='output')body.scrollTop=body.scrollHeight;else body.scrollTop=top;
}
async function selectRun(id){
  runId=id;visibleCount=150;following=true;selectedTool=null;$('inspector').hidden=true;
  if(runRows.some(r=>r.id===id))rootRunId=id;
  renderRunPicker();render();if(panel!=='activity')await renderSecondary();
}
async function switchPanel(value){
  panel=value;
  document.querySelectorAll('[data-panel]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.panel===panel)));
  for(const name of ['activity','artifacts','children','context','analysis'])$(name+'Panel').hidden=name!==panel;
  if(panel!=='activity')try{await renderSecondary();}catch(e){alert(e.message);}
}
async function renderSecondary(){
  const selected=runId;const targetPanel=panel;if(!selected){$(panel+'Panel').replaceChildren(el('p','muted','开始任务后可在这里查看。'));return;}
  const target=$(targetPanel+'Panel');
  if(targetPanel==='artifacts'){
    const data=await api(`/api/runs/${selected}/artifacts`);if(runId!==selected||panel!==targetPanel)return;
    const parts=[el('p','muted','运行期间的工作区变化 · 基于真实文件快照')];
    if(!data.available)parts.push(el('p','muted','这次历史运行没有文件快照。'));
    else if(!data.artifacts.length)parts.push(el('p','muted','暂未发现文件变化。'));
    for(const artifact of data.artifacts){const b=button('',safe(()=>inspectArtifact(selected,artifact.id)),'artifact-row');b.append(el('span','artifact-change',({added:'新增',modified:'修改',deleted:'删除'})[artifact.change]),document.createTextNode(artifact.path),el('small','',`${fmt((artifact.after||artifact.before).size)} bytes${(artifact.after||artifact.before).redacted?' · 已脱敏':''}`));parts.push(b);}
    if(data.warnings.length){const details=el('details');details.append(el('summary','output-warning',`有 ${data.warnings.length} 项未完整记录`),el('pre','',data.warnings.join('\n')));parts.push(details);}
    target.replaceChildren(...parts);
  }else if(targetPanel==='children'){
    const children=await api(`/api/runs/${selected}/children`);if(runId!==selected||panel!==targetPanel)return;
    const parts=[el('p','muted','子任务的职责、状态与执行时间；点击可查看独立轨迹。')];
    const start=Math.min(...children.map(c=>Date.parse(c.started_at||c.created_at)));
    const end=Math.max(...children.map(c=>Date.parse(c.completed_at||new Date().toISOString())));
    if(children.length)parts.push(el('p','muted',`共同时间轴：${shortDate(new Date(start).toISOString())} → ${shortDate(new Date(end).toISOString())} · 总跨度 ${duration((end-start)/1000)}`));
    for(const child of children){
      child.runs.forEach(r=>model().hydrate(r));
      const b=button('',safe(async()=>{const id=child.runs[0]?.id;if(id){runId=id;renderRunPicker();await switchPanel('activity');render();}else alert('子任务尚未开始执行。');}),'child-card');
      b.append(el('h3','',child.objective||child.agent_name),el('small','',`${child.agent_name} · ${statusLabel(child.status)} · ${duration(seconds(child.started_at,child.completed_at||new Date().toISOString()))}`));
      if(child.constraints?.length)b.append(el('p','muted',`约束：${child.constraints.join('；')}`));
      if(child.acceptance_criteria?.length)b.append(el('p','muted',`完成条件：${child.acceptance_criteria.join('；')}`));
      const lane=el('div','lane');const bar=el('span');const a=Date.parse(child.started_at||child.created_at);const z=Date.parse(child.completed_at||new Date().toISOString());bar.style.left=`${Math.max(0,(a-start)/Math.max(1,end-start)*100)}%`;bar.style.width=`${Math.max(.5,(z-a)/Math.max(1,end-start)*100)}%`;lane.setAttribute('aria-label',`${shortDate(child.started_at)} 至 ${child.completed_at?shortDate(child.completed_at):'现在'}`);lane.append(bar);b.append(lane);parts.push(b);
    }
    if(!children.length)parts.push(el('p','muted','本任务尚未创建子任务。'));
    target.replaceChildren(...parts);
  }else if(targetPanel==='context'){
    const data=await api(`/api/runs/${selected}/context`);if(runId!==selected||panel!==targetPanel)return;
    const parts=[el('p','muted','本任务累计用量包含上下文整理，不包含子任务；只展示实际记录的信息。')];
    parts.push(button('查看完整事件日志',safe(()=>inspectRunEvents(selected)),'quiet'),el('p','muted','下方为最近 200 条上下文记录，更早记录可在完整事件中读取。'));
    const metrics=el('div','metric-grid');const usage=current().usage;
    for(const [label,value]of [['输入 tokens',fmt(usage.input_tokens)],['输出 tokens',fmt(usage.output_tokens)],['上下文版本',data.compactions.length]]){const metric=el('div','metric');metric.append(el('span','',label),el('strong','',value));metrics.append(metric);}parts.push(metrics);
    for(const record of data.compactions)parts.push(button(`${shortDate(record.created_at)} · ${record.status} · 查看摘要与原文`,safe(()=>inspectCompaction(data.session_id,record.id)),'artifact-row'));
    for(const event of data.events){const details=el('details','context-event');details.append(el('summary','',`${event.type} · ${shortDate(event.timestamp)}`),el('pre','',JSON.stringify(event.payload,null,2)));parts.push(details);}
    if(!data.events.length)parts.push(el('p','muted','没有记录上下文或记忆事件。'));
    target.replaceChildren(...parts);
  }else if(targetPanel==='analysis'){
    await renderAnalysis(target);
  }
}

// --- historical analysis console ------------------------------------------

const analysisState={runs:[],summary:null,methodology:null,selected:null,detail:null,compare:[],loading:false};

function analysisFilters(){
  return {
    status:$('filterStatus').value,
    stop_reason:$('filterReason').value,
    sort:$('filterSort').value,
    order:$('filterOrder').value,
    search:$('filterSearch').value.trim(),
    gap_threshold_seconds:$('filterGap').value||120,
  };
}

async function loadAnalysis(){
  const filters=analysisFilters();
  const query=buildQuery({...filters,limit:500});
  const data=await api(`/api/analysis/runs?${query}`);
  analysisState.runs=data.runs;
  analysisState.summary=data.summary;
  analysisState.methodology=data.methodology;
  return data;
}

function renderMethodology(){
  const body=$('analysisMethodBody');
  const entries=Object.entries(analysisState.methodology||{});
  body.replaceChildren(...entries.map(([key,text])=>{
    const wrap=el('div');
    wrap.append(el('dt','',key),el('dd','',text));
    return wrap;
  }));
}

function renderSummary(){
  const summary=analysisState.summary||{};
  const metrics=[
    ['运行总数',fmt(summary.runs)],
    ['净耗时合计',formatDuration(summary.net_seconds?.sum)],
    ['净耗时均值',formatDuration(summary.net_seconds?.mean)],
    ['审批等待合计',formatDuration(summary.approval_seconds?.sum)],
    ['耗时未知',fmt(summary.duration_unknown)],
    ['未配对审批',fmt(summary.unpaired_approvals)],
  ];
  $('analysisSummary').replaceChildren(...metrics.map(([label,value])=>{
    const metric=el('div','metric');
    metric.append(el('span','',label),el('strong','',value));
    return metric;
  }));

  const reasons=summarizeReasons(summary);
  $('analysisReasons').replaceChildren(...reasons.map(item=>{
    const chip=el('span',`reason-chip ${item.tone}`);
    chip.append(el('span','dot'),document.createTextNode(item.label),el('span','count','',`${item.count} · ${formatPercent(item.count,summary.runs)}`));
    return chip;
  }));

  // Populate the stop-reason filter from observed data, preserving selection.
  const select=$('filterReason');
  const current=select.value;
  const options=[el('option','','全部')];
  options[0].value='';
  for(const item of reasons){const option=el('option','',`${item.label} (${item.count})`);option.value=item.reason;options.push(option);}
  select.replaceChildren(...options);
  select.value=current;
}

function renderCompare(){
  const panel=$('analysisCompare');
  // analysisState.compare holds run ids; resolve them against the loaded rows.
  const ids=analysisState.compare;
  const byId=id=>analysisState.runs.find(run=>run.run_id===id);
  const left=byId(ids[0]),right=byId(ids[1]);
  if(!left||!right){panel.hidden=true;panel.replaceChildren();return;}
  panel.hidden=false;
  const rows=[
    ['总耗时',formatDuration(left.total_seconds),formatDuration(right.total_seconds),left.total_seconds,right.total_seconds,formatDuration],
    ['净耗时',formatDuration(left.net_seconds),formatDuration(right.net_seconds),left.net_seconds,right.net_seconds,formatDuration],
    ['审批等待',formatDuration(left.approval_seconds),formatDuration(right.approval_seconds),left.approval_seconds,right.approval_seconds,formatDuration],
    ['事件数',fmt(left.event_count),fmt(right.event_count),left.event_count,right.event_count,fmt],
  ];
  const grid=el('div','compare-grid');
  for(const [label,a,b,av,bv,render] of rows){
    const cell=el('div','compare-cell');
    const delta=av===null||av===undefined||bv===null||bv===undefined?UNKNOWN:render(Math.abs(bv-av));
    cell.append(el('span','',label),el('strong','',`${a} → ${b}`),el('div','delta',`差值 ${delta}`));
    grid.append(cell);
  }
  const head=el('div');
  head.append(el('h3','','运行对比'),el('p','muted',`${left.prompt||left.run_id} ↔ ${right.prompt||right.run_id}`));
  const actions=el('div','analysis-actions');
  actions.append(button('清除对比',()=>{analysisState.compare=[];renderCompare();},'quiet'));
  panel.replaceChildren(head,actions,grid);
}

function renderTable(){
  const table=$('analysisTable');
  if(!analysisState.runs.length){table.replaceChildren(el('p','muted','没有符合条件的运行。'));return;}
  const rows=analysisState.runs.map(run=>{
    const row=el('button','analysis-row');
    row.type='button';
    row.setAttribute('aria-pressed',String(analysisState.selected===run.run_id));
    if(analysisState.selected===run.run_id)row.classList.add('selected');
    row.addEventListener('click',safe(()=>selectAnalysisRun(run.run_id)));

    const title=el('div','cell');
    title.append(el('div','title',run.prompt||'(无任务描述)'));
    title.append(el('div','sub',`${formatTimestamp(run.started_at)} · ${run.session_id.slice(0,8)}`));
    row.append(title);

    const cells=[
      ['总耗时',run.total_seconds===null?UNKNOWN:formatDuration(run.total_seconds),run.total_seconds===null],
      ['审批等待',formatDuration(run.approval_seconds),false],
      ['净耗时',run.net_seconds===null?UNKNOWN:formatDuration(run.net_seconds),run.net_seconds===null],
      ['事件数',fmt(run.event_count),false],
    ];
    for(const [label,value,unknown] of cells){
      const cell=el('div','cell');
      cell.append(el('span','',label),el('strong',unknown?'unknown':'',value));
      row.append(cell);
    }

    const reason=el('div','cell');
    reason.append(el('span','','停止原因'),el('strong','',stopReasonLabel(run.stop_reason)));
    if(run.unpaired_approvals)reason.append(el('div','sub',`${run.unpaired_approvals} 次审批未配对`));
    if(run.gaps?.length)reason.append(el('div','sub',`${run.gaps.length} 段未知空档`));
    row.append(reason);

    const pick=el('label','pick');
    const check=el('input');
    check.type='checkbox';
    check.checked=analysisState.compare.includes(run.run_id);
    check.addEventListener('click',event=>event.stopPropagation());
    check.addEventListener('change',()=>toggleCompare(run.run_id,check.checked));
    pick.append(check,document.createTextNode('对比'));
    row.append(pick);
    return row;
  });
  table.replaceChildren(...rows);
}

function toggleCompare(runId,checked){
  const list=analysisState.compare.filter(id=>id!==runId);
  if(checked)list.push(runId);
  analysisState.compare=list.slice(-2);
  renderCompare();
}

async function selectAnalysisRun(runId){
  analysisState.selected=runId;
  const gap=$('filterGap').value||120;
  analysisState.detail=await api(`/api/analysis/runs/${runId}?gap_threshold_seconds=${encodeURIComponent(gap)}`);
  renderTable();
  renderDetail();
}

function renderDetail(){
  const panel=$('analysisDetail');
  const run=analysisState.detail;
  if(!run){panel.hidden=true;panel.replaceChildren();return;}
  panel.hidden=false;
  const parts=[];
  const head=el('div');
  head.append(el('h3','',run.prompt||'(无任务描述)'),el('p','muted',`运行 ${run.run_id} · ${formatTimestamp(run.started_at)} → ${run.completed_at?formatTimestamp(run.completed_at):'未结束'}`));
  parts.push(head);

  const parts2=breakdown(run);
  if(parts2.known){
    const bar=el('div','breakdown-bar');
    const total=Math.max(parts2.total,0.001);
    const netShare=Math.max(0,parts2.net)/total*100;
    const approvalShare=Math.max(0,parts2.approval)/total*100;
    const net=el('span','net');net.style.width=`${netShare}%`;net.textContent=netShare>12?`净耗时 ${formatDuration(parts2.net)}`:'';
    const approval=el('span','approval');approval.style.width=`${approvalShare}%`;approval.textContent=approvalShare>12?`审批 ${formatDuration(parts2.approval)}`:'';
    bar.append(net,approval);
    parts.push(bar);
    const legend=el('div','breakdown-legend');
    legend.append(el('span','net','',`净耗时 ${formatDuration(parts2.net)}`),el('span','approval','',`审批等待 ${formatDuration(parts2.approval)}`),el('span','',`总耗时 ${formatDuration(parts2.total)}`));
    parts.push(legend);
  }else{
    parts.push(el('p','muted','这次运行缺少结束时间，总耗时与净耗时记为未知。'));
  }

  const metrics=el('div','metric-grid');
  for(const [label,value] of [
    ['停止原因',stopReasonLabel(run.stop_reason)],
    ['状态',statusLabel(run.status)],
    ['审批区间',fmt(run.approval_intervals.length)],
    ['未配对审批',fmt(run.unpaired_approvals)],
    ['重复审批事件',fmt(run.duplicate_approvals)],
    ['事件数',fmt(run.event_count)],
  ]){const metric=el('div','metric');metric.append(el('span','',label),el('strong','',value));metrics.append(metric);}
  parts.push(metrics);

  if(run.approval_waits.length){
    const details=el('details','method-panel');
    details.append(el('summary','',`审批等待明细（${run.approval_waits.length}）`));
    const list=el('div','method-body');
    for(const wait of run.approval_waits){
      const line=el('div');
      line.append(el('dt','',wait.tool_name||wait.approval_id));
      line.append(el('dd','',wait.paired
        ? `${formatTimestamp(wait.requested_at)} → ${formatTimestamp(wait.resolved_at)} · ${formatDuration(wait.seconds)} · ${wait.approved?'已批准':'已拒绝'}`
        : `${formatTimestamp(wait.requested_at)} · 未配对，等待时长未知`));
      list.append(line);
    }
    details.append(list);
    parts.push(details);
  }

  const gaps=describeGaps(run);
  if(gaps.length){
    const wrap=el('div');
    wrap.append(el('h4','','长时间无事件区间（未知）'));
    const list=el('div','gap-list');
    for(const gap of gaps){
      const row=el('div','gap-row');
      row.append(el('span','',`${gap.label} · ${gap.duration}`),el('span','unknown-tag','未知 · 未扣除'));
      list.append(row);
    }
    wrap.append(list);
    parts.push(wrap);
  }

  if(run.notes?.length){
    const notes=el('ul','analysis-notes');
    for(const note of run.notes)notes.append(el('li','',note));
    parts.push(notes);
  }

  const actions=el('div','analysis-actions');
  actions.append(button('查看执行轨迹',safe(async()=>{
    // The console spans sessions, so the target run may live in another one.
    // Switching sessions reloads runRows; only then can the trace render.
    if(run.session_id&&run.session_id!==sessionId){
      await selectSession(run.session_id);
      runId=run.run_id;rootRunId=run.run_id;
      renderRunPicker();
    }else{
      runId=run.run_id;
      if(runRows.some(r=>r.id===run.run_id))rootRunId=run.run_id;
      renderRunPicker();
    }
    await switchPanel('activity');
    render();
  }),'primary'));
  actions.append(button('关闭详情',()=>{analysisState.selected=null;analysisState.detail=null;renderTable();renderDetail();},'quiet'));
  parts.push(actions);

  panel.replaceChildren(...parts);
}

async function renderAnalysis(target){
  if(!analysisState.runs.length&&!analysisState.loading){
    analysisState.loading=true;
    try{await loadAnalysis();}finally{analysisState.loading=false;}
  }
  renderMethodology();
  renderSummary();
  renderCompare();
  renderTable();
  renderDetail();
  if(!target.childElementCount)target.append(el('p','muted','正在加载历史运行…'));
}

async function refreshAnalysis(){
  try{
    await loadAnalysis();
    if(analysisState.selected&&!analysisState.runs.some(r=>r.run_id===analysisState.selected)){
      analysisState.selected=null;analysisState.detail=null;
    }
    renderSummary();renderCompare();renderTable();renderDetail();
  }catch(e){alert(e.message);}
}

function exportAnalysis(){
  const filters=analysisFilters();
  const query=buildQuery({...filters,limit:100000});
  const link=document.createElement('a');
  link.href=`/api/analysis/export?${query}`;
  link.download='';
  document.body.append(link);
  link.click();
  link.remove();
}
async function inspectRunEvents(id){
  openInspector('context','完整事件日志');selectedTool=null;
  const version=detailVersion;let cursor=null,through=null;
  $('detailMeta').replaceChildren(el('span','','按持久化顺序分页 · 本任务，不含子任务'));
  $('detailBody').replaceChildren();
  const more=button('加载更多事件',safe(load),'quiet');$('detailActions').replaceChildren(more);
  async function load(){
    more.disabled=true;
    try{
      const params=new URLSearchParams({limit:'50',children:'false'});
      if(cursor)params.set('cursor',cursor);if(through)params.set('through',through);
      const data=await api(`/api/runs/${id}/events?${params}`);if(version!==detailVersion)return;
      cursor=data.cursor;through=data.through;more.hidden=!data.has_more;
      $('detailBody').append(el('pre','',JSON.stringify(data.events,null,2)));
    }finally{more.disabled=false;}
  }
  await load();
}
async function inspectArtifact(run,artifactId){
  openInspector('artifact','读取文件…');selectedTool=null;
  const version=detailVersion;
  const data=await api(`/api/runs/${run}/artifacts/${artifactId}`);
  if(version!==detailVersion)return;
  $('detailTitle').textContent=data.path;$('detailMeta').replaceChildren(el('span','',({added:'新增文件',modified:'修改文件',deleted:'删除文件'})[data.change]));
  const parts=[];const actions=[];
  if(data.diff!==null){parts.push(el('p','muted','运行前后文件快照的差异'),el('pre','',data.diff||'内容相同'));if(data.diff_truncated)parts.push(el('p','output-warning','差异过长，当前展示部分内容。'));}
  else if(data.after&&['image/png','image/jpeg','image/gif','image/webp'].includes(data.after.media_type)){const img=el('img');img.alt=data.path;img.src=`/api/runs/${run}/artifacts/${artifactId}/content`;parts.push(img);}
  else parts.push(el('p','muted','二进制文件，可下载查看。'));
  for(const side of ['before','after'])if(data[side]){
    const a=el('a','',side==='after'?'下载当前快照':'下载修改前快照');a.href=`/api/runs/${run}/artifacts/${artifactId}/content?side=${side}&download=true`;a.setAttribute('download','');actions.push(a);
    if(data[side].text)actions.push(button(side==='after'?'预览当前内容':'预览修改前内容',safe(async()=>{const response=await fetch(`/api/runs/${run}/artifacts/${artifactId}/content?side=${side}`);if(!response.ok)throw new Error('文件内容读取失败');const text=await response.text();if(version===detailVersion)$('detailBody').replaceChildren(el('pre','',text));}),'quiet'));
  }
  if(data.after?.redacted||data.before?.redacted)parts.push(el('p','output-warning','展示与下载内容已脱敏。'));
  $('detailBody').replaceChildren(...parts);$('detailActions').replaceChildren(...actions);
}
async function inspectCompaction(sid,id){
  openInspector('context','压缩摘要与来源');selectedTool=null;
  const version=detailVersion;
  const data=await api(`/api/sessions/${sid}/compactions/${id}`);
  if(version!==detailVersion)return;
  $('detailMeta').replaceChildren(el('span','',id));
  const summary=data.compaction.summary||data.compaction.summary_text||data.compaction;
  $('detailBody').replaceChildren(el('h3','','保存的摘要'),el('pre','',typeof summary==='string'?summary:JSON.stringify(summary,null,2)),el('h3','','压缩前原文'),el('pre','',JSON.stringify(data.messages,null,2)));
  let cursor=data.next_position;
  const more=button('加载更多原文',safe(async()=>{more.disabled=true;try{const page=await api(`/api/sessions/${sid}/compactions/${id}?after=${cursor}`);if(version!==detailVersion)return;cursor=page.next_position;$('detailBody').append(el('pre','',JSON.stringify(page.messages,null,2)));more.hidden=page.eof;}finally{more.disabled=false;}}),'quiet');more.hidden=data.eof;$('detailActions').replaceChildren(more);
}

$('newSession').addEventListener('click',safe(async()=>{selecting=true;renderComposer();try{const result=await post('/api/sessions');await selectSession(result.session_id);}finally{selecting=false;renderComposer();$('prompt').focus();}}));
$('moreSessions').addEventListener('click',safe(async()=>{sessionLimit+=50;await fetchSessions();}));
$('runPicker').addEventListener('change',safe(async()=>{if($('runPicker').value==='more'){runLimit+=50;await refresh();}else await selectRun($('runPicker').value);}));
$('send').addEventListener('click',sendPrompt);$('stop').addEventListener('click',safe(stopRun));
$('prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('send').disabled)sendPrompt();}});
$('prompt').addEventListener('input',()=>{drafts.set(sessionId,$('prompt').value);requestId=null;});
$('compact').addEventListener('click',safe(async()=>{$('compact').disabled=true;$('sendStatus').textContent='正在整理上下文…';try{await post(`/api/sessions/${sessionId}/compact`);$('sendStatus').textContent='上下文整理请求已处理';}finally{renderComposer();queueRefresh();}}));
$('activityScroller').addEventListener('scroll',()=>{if(rendering)return;const s=$('activityScroller');following=s.scrollHeight-s.clientHeight-s.scrollTop<70;$('followLatest').hidden=following;});
$('followLatest').addEventListener('click',()=>{following=true;newEvents=0;renderActivity();});
$('moreActivity').addEventListener('click',()=>{visibleCount+=150;following=false;renderActivity();});
$('detailBody').addEventListener('scroll',()=>{const s=$('detailBody');outputFollowing=s.scrollHeight-s.clientHeight-s.scrollTop<50;});
$('closeInspector').addEventListener('click',closeInspector);
$('navToggle').addEventListener('click',()=>{const open=$('sidebar').classList.toggle('open');$('navToggle').setAttribute('aria-expanded',String(open));});
$('theme').value=storage.get('bot.theme')||'auto';
function theme(){document.documentElement.dataset.theme=$('theme').value;storage.set('bot.theme',$('theme').value);}
$('theme').addEventListener('change',theme);theme();
for(const b of document.querySelectorAll('[data-panel]'))b.addEventListener('click',()=>switchPanel(b.dataset.panel));
$('analysisRefresh').addEventListener('click',safe(refreshAnalysis));
$('analysisExport').addEventListener('click',exportAnalysis);
for(const id of ['filterStatus','filterReason','filterSort','filterOrder','filterGap'])$(id).addEventListener('change',safe(refreshAnalysis));
let searchTimer=null;
$('filterSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>safe(refreshAnalysis)(),300);});
for(const b of document.querySelectorAll('[data-detail]'))b.addEventListener('click',()=>{detailTab=b.dataset.detail;outputFollowing=false;renderToolDetail();if(detailTab==='events'&&!rawEvents)safe(()=>loadRawEvents())();});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){if(!$('inspector').hidden)closeInspector();$('sidebar').classList.remove('open');$('navToggle').setAttribute('aria-expanded','false');}});
setInterval(()=>{if(sessionId){renderMeta();if(activeRunId)queueRefresh();}},2000);
async function init(){
  try{
    const [status,skills]=await Promise.all([api('/api/status'),api('/api/skills')]);
    $('modelName').textContent=status.model;$('workspacePath').textContent=status.workspace;
    $('skillsList').replaceChildren(...skills.map(skill=>{const label=el('label');const check=el('input');check.type='checkbox';check.addEventListener('change',()=>check.checked?selectedSkills.add(skill.name):selectedSkills.delete(skill.name));label.append(check,document.createTextNode(skill.name));return label;}));
    if(!skills.length)$('skillsList').append(el('span','muted','没有可用 Skills'));
    await fetchSessions();const saved=storage.get('bot.session');sessionId=sessionRows.some(s=>s.id===saved)?saved:sessionRows[0]?.id;
    if(!sessionId)sessionId=(await post('/api/sessions')).session_id;
    connect();await selectSession(sessionId);
  }catch(e){alert(`工作台加载失败：${e.message}`);$('connection').textContent='加载失败';}
}
init();
