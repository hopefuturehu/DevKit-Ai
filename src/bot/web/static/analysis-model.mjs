// Historical run analysis console: cross-session list, net duration, stop reasons.
// Pure helpers are exported so they can be unit-tested with node --test.

export const UNKNOWN = '未知';

export function formatDuration(seconds){
  if(seconds===null||seconds===undefined||Number.isNaN(seconds))return UNKNOWN;
  const value=Math.max(0,Number(seconds));
  if(value<60)return `${value.toFixed(1)} s`;
  const minutes=Math.floor(value/60),rest=Math.round(value%60);
  if(minutes<60)return `${minutes}m ${rest}s`;
  const hours=Math.floor(minutes/60);
  return `${hours}h ${minutes%60}m`;
}

export function formatPercent(part,total){
  if(!total||part===null||part===undefined)return UNKNOWN;
  return `${((part/total)*100).toFixed(1)}%`;
}

export function formatTimestamp(value){
  if(!value)return UNKNOWN;
  const date=new Date(value);
  if(Number.isNaN(date.getTime()))return UNKNOWN;
  return date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// Stop reasons are reason codes from the event log; label the common ones and
// fall back to the raw code so nothing is silently hidden.
const STOP_LABELS={
  completed:'正常完成',cancelled:'用户停止',failed:'运行失败',blocked:'被阻塞',
  limit_reached:'达到上限',max_steps:'达到步数上限',runtime_timeout:'运行超时',
  runtime_error:'运行时错误',approval_denied:'审批被拒绝',unknown:'未知',
};
export function stopReasonLabel(reason){
  if(!reason)return STOP_LABELS.unknown;
  return STOP_LABELS[reason]||reason;
}

export function stopReasonTone(reason){
  if(reason==='completed')return 'ok';
  if(reason==='unknown')return 'muted';
  if(reason==='cancelled')return 'warn';
  return 'bad';
}

// The breakdown must always add up to the total, so the residual is computed
// rather than assumed: total - approval union - net.
export function breakdown(run){
  const total=run.total_seconds;
  const approval=run.approval_seconds??0;
  const net=run.net_seconds;
  if(total===null||total===undefined)return {known:false,total:null,approval,net:null};
  return {known:true,total,approval,net:net??Math.max(0,total-approval)};
}

export function buildQuery(filters){
  const params=new URLSearchParams();
  for(const [key,value] of Object.entries(filters||{})){
    if(value===null||value===undefined||value==='')continue;
    params.set(key,value);
  }
  return params.toString();
}

// Client-side guard mirroring the server contract: unknown durations always sort
// last so they never look like the fastest or slowest run.
export function sortRuns(runs,sort,order){
  const key={net:'net_seconds',total:'total_seconds',approval:'approval_seconds',started:'started_at',events:'event_count'}[sort]||'started_at';
  const direction=order==='asc'?1:-1;
  return [...runs].sort((a,b)=>{
    const left=a[key],right=b[key];
    const leftUnknown=left===null||left===undefined;
    const rightUnknown=right===null||right===undefined;
    if(leftUnknown&&rightUnknown)return 0;
    if(leftUnknown)return 1;
    if(rightUnknown)return -1;
    if(typeof left==='string'||typeof right==='string')return String(left).localeCompare(String(right))*direction;
    return (left-right)*direction;
  });
}

export function summarizeReasons(summary){
  const reasons=summary?.stop_reasons||{};
  const total=Object.values(reasons).reduce((sum,value)=>sum+value,0);
  return Object.entries(reasons).map(([reason,count])=>({
    reason,count,label:stopReasonLabel(reason),tone:stopReasonTone(reason),
    share:total?count/total:0,
  }));
}

export function describeGaps(run){
  const gaps=run.gaps||[];
  if(!gaps.length)return [];
  return gaps.map(gap=>({
    ...gap,
    label:`${formatTimestamp(gap.start)} → ${formatTimestamp(gap.end)}`,
    duration:formatDuration(gap.seconds),
  }));
}
