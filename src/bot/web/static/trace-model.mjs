// One projection for live events and historical replay. No network or DOM state.
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'blocked', 'limit_reached']);
export const toolKey = (event) => [event.session_id, event.run_id, event.payload?.tool_call_id].join('/');
export const statusLabel = (status) => ({running:'运行中', starting:'正在启动', completed:'已完成', failed:'失败', cancelled:'已停止', cancelling:'正在停止', blocked:'等待处理', limit_reached:'已达限制', requested:'等待执行', awaiting_approval:'等待批准', denied:'已拒绝', timed_out:'超时', unknown:'状态未确认', queued:'排队中', waiting_parent:'等待主任务', waiting_approval:'等待批准', interrupted:'已中断'})[status] || status || '未记录';
const appendTail = (old, text, limit = 64000) => (old + text).slice(-limit);
const known = (value) => value !== undefined && value !== null;

export class TraceModel {
  constructor() { this.ids = new Set(); this.runs = new Map(); this.tools = new Map(); this.children = new Map(); this.processes = new Map(); this.lastPosition = 0; }
  run(id, sessionId) {
    if (!this.runs.has(id)) this.runs.set(id, {id, session_id:sessionId, status:'unknown', nodes:[], messages:new Map(), attempts:new Map(), approvals:new Map(), diagnostics:[], usage:{input_tokens:null,output_tokens:null,cost_usd:null}, plan:null, started_at:null, completed_at:null, prompt:'', cancelling:false, artifactsVersion:0});
    return this.runs.get(id);
  }
  hydrate(summary) {
    const run = this.run(summary.id, summary.session_id);
    for (const field of ['prompt','started_at','completed_at','controllable','live']) if (known(summary[field])) run[field] = summary[field];
    if (run.status === 'unknown' || TERMINAL.has(summary.status)) run.status = summary.status;
    for (const field of Object.keys(run.usage)) if (known(summary[field])) run.usage[field] = summary[field];
    if (summary.cancelling) run.cancelling = true;
    return run;
  }
  apply(event) {
    if (!event?.id || this.ids.has(event.id)) return false;
    this.ids.add(event.id);
    this.lastPosition = Math.max(this.lastPosition, event.position || 0);
    const run = this.run(event.run_id, event.session_id);
    const p = event.payload || {};
    const order = event.position || this.ids.size;
    const node = (kind, content, extra={}) => { const n={kind,content,order,event,...extra}; run.nodes.push(n); return n; };
    const messageKey = `${p.step ?? 0}/${p.phase || 'main'}/${run.attempts.get(p.step ?? 0) || 0}`;
    const assistant = () => {
      if (!run.messages.has(messageKey)) run.messages.set(messageKey, node('assistant','',{reasoning:'',step:p.step}));
      return run.messages.get(messageKey);
    };
    switch (event.type) {
      case 'run.started':
        run.status='running'; run.started_at=event.timestamp; run.prompt=p.prompt || '';
        node('user', run.prompt); break;
      case 'assistant.delta': assistant().content += p.text || ''; break;
      case 'assistant.reasoning.delta': assistant().reasoning += p.text || ''; break;
      case 'assistant.message':
        if (known(p.text)) assistant().content=p.text;
        assistant().finished=true; break;
      case 'model.request.retry':
        if (run.messages.has(messageKey)) run.messages.get(messageKey).discarded=true;
        run.attempts.set(p.step ?? 0, (run.attempts.get(p.step ?? 0)||0)+1);
        node('system','模型请求正在重试',{diagnostic:true}); break;
      case 'model.usage':
        for (const k of Object.keys(run.usage)) if (known(p[k])) run.usage[k]=p[k];
        break;
      case 'plan.updated': run.plan=p; break;
      case 'run.steer.queued':
        node('user',p.text||'',{message_id:p.message_id,delivery:'queued'}); break;
      case 'run.steered': {
        const queued=run.nodes.find(n => n.message_id && n.message_id===p.message_id);
        if (queued) queued.delivery='received'; else node('user',p.text||'',{delivery:'received'});
        break;
      }
      case 'run.cancel.requested': run.cancelling=true; break;
      case 'run.completed':
      case 'run.failed':
      case 'run.cancelled':
      case 'run.blocked':
      case 'run.limit_reached':
      case 'run.finished': {
        run.status=p.status || event.type.replace('run.','');
        run.cancelling=false;
        run.completed_at=event.timestamp;
        run.error=p.error || null;
        for (const k of Object.keys(run.usage)) if (known(p[k])) run.usage[k]=p[k];
        if (p.final_text) {
          const present=run.nodes.some(n=>n.kind==='assistant' && n.content===p.final_text && !n.discarded);
          if (!present) node('assistant',p.final_text,{finished:true,reasoning:''});
        }
        break;
      }
      case 'run.artifacts.updated': run.artifactsVersion++; break;
      case 'process.output':
      case 'process.updated': {
        const key=`${event.run_id}/${p.process_id}`;
        const process=this.processes.get(key)||{id:p.process_id,stdout:'',stderr:'',status:'unknown'};
        if(event.type==='process.updated'){Object.assign(process,Object.fromEntries(Object.entries(p).filter(([key])=>!['stdout','stderr'].includes(key))));process.observed_at=event.timestamp;}
        else {const stream=p.stream==='stderr'?'stderr':'stdout';process[stream]=appendTail(process[stream],String(p.data||''));process.outputChars=(process.outputChars||0)+String(p.data||'').length;}
        this.processes.set(key,process);break;
      }
      default:
        if (event.type.startsWith('tool.') || event.type.startsWith('approval.')) {
          if (!p.tool_call_id) { node('system',event.type,{diagnostic:true}); break; }
          const key=toolKey(event);
          if (!this.tools.has(key)) {
            const tool={key,run_id:event.run_id,session_id:event.session_id,id:p.tool_call_id,name:p.name||'工具',arguments:{},status:'unknown',stdout:'',stderr:'',output:'',outputChars:0,events:[],order,requested_at:null,started_at:null,completed_at:null};
            this.tools.set(key,tool); node('tool','',{toolKey:key});
          }
          const tool=this.tools.get(key);
          if (p.name) tool.name=p.name;
          tool.events.push(event); if(tool.events.length>100) tool.events.shift();
          if (event.type==='tool.requested') { tool.arguments=p.arguments||{}; tool.requested_at=event.timestamp; if(!tool.completed_at)tool.status='requested'; }
          if (event.type==='tool.started') { tool.started_at=event.timestamp; tool.status='running'; }
          if (event.type==='tool.output') {
            const stream=p.stream==='stderr'?'stderr':'stdout';
            const text=String(p.data||''); tool[stream]=appendTail(tool[stream],text); tool.outputChars+=text.length;
          }
          if (event.type==='tool.completed' || event.type==='tool.result') {
            tool.completed_at=event.timestamp;
            Object.assign(tool, Object.fromEntries(Object.entries(p).filter(([,v])=>known(v))));
            tool.status=p.status || (p.success===true?'completed':p.success===false?'failed':'unknown');
            if(p.output_excerpt)tool.output=p.output_excerpt;
            if(tool.denied)tool.status='denied';
          }
          if (event.type==='approval.requested') {
            tool.status='awaiting_approval'; tool.approval_id=p.approval_id; tool.reason=p.reason;
            run.approvals.set(p.approval_id||event.id,{...p,run_id:event.run_id,session_id:event.session_id,pending:true});
          }
          if (event.type==='approval.resolved') {
            for(const approval of run.approvals.values()) if(approval.tool_call_id===tool.id)approval.pending=false;
            tool.denied=p.approved===false; tool.status=tool.denied?'denied':'requested';
          }
        } else if (event.type.startsWith('subagent.')) {
          const key=p.task_id || event.run_id;
          const child=this.children.get(key)||{id:key,session_id:event.session_id};
          if(!child.parent_run_id&&!event.run_id.startsWith('subagent:')){child.parent_run_id=event.run_id;node('child','',{taskId:key});}
          const states={started:'running',resumed:'running'};const state=event.type.replace('subagent.','');
          Object.assign(child,p,{status:p.status||states[state]||state,updated_at:event.timestamp});
          this.children.set(key,child);
        } else {
          const labels={'context.compaction.started':'正在整理上下文','context.compaction.completed':'上下文整理完成','context.compaction.failed':'上下文整理失败','memory.extraction.started':'正在整理长期记忆','memory.routing.decided':'已检查相关记忆','run.stall_warning':'任务暂时没有新进展','run.recovery_started':'正在恢复执行','run.finalizing':'正在整理结果','skill.activated':`已使用 Skill：${p.name||''}`};
          node('system',labels[event.type]||event.type,{diagnostic:true});
        }
    }
    if (event.type.startsWith('context.') || event.type.startsWith('memory.') || event.type==='model.response') {
      run.diagnostics.push(event); if(run.diagnostics.length>300)run.diagnostics.shift();
    }
    return true;
  }
  ordered(runId) { return [...(this.runs.get(runId)?.nodes || [])].sort((a,b)=>a.order-b.order); }
  toolsFor(runId) { return [...this.tools.values()].filter(t=>t.run_id===runId).sort((a,b)=>a.order-b.order); }
  processFor(tool) {
    const processId=tool.process_id || tool.arguments?.process_id;
    if(!processId)return null;
    const calls=this.toolsFor(tool.run_id).filter(t=>(t.process_id||t.arguments?.process_id)===processId);
    const latest=calls.at(-1);
    const observed=this.processes.get(`${tool.run_id}/${processId}`);
    const result={id:processId,status:latest?.process_status||latest?.status||'unknown',returncode:latest?.returncode,...observed,calls};
    if(result.status==='running'&&known(result.elapsed_seconds)&&result.observed_at)result.elapsed_seconds+=Math.max(0,(Date.now()-Date.parse(result.observed_at))/1000);
    return result;
  }
  toolStatus(tool) {
    const process=this.processFor(tool);
    if(process && tool.status==='running') return process.status;
    const run=this.runs.get(tool.run_id);
    if(!tool.completed_at && run && TERMINAL.has(run.status))return run.status==='cancelled'?'cancelled':'unknown';
    return tool.status;
  }
}

export function toolSummary(tool) {
  const a=tool.arguments || {};
  if(tool.name==='read_file')return `读取 ${a.path||'文件'}${a.start_line?` · ${a.start_line}–${a.end_line||'末尾'} 行`:''}`;
  if(tool.name==='apply_patch')return `修改 ${a.path||'文件'}`;
  if(tool.name==='search_text')return `搜索 ${a.query||''}`;
  if(tool.name==='run_command')return (a.argv||[]).join(' ') || '运行命令';
  if(tool.name==='run_shell')return a.command || a.script || '运行 Shell';
  if(tool.name==='poll_process')return `等待进程 ${a.process_id||''}`;
  return tool.name;
}
