import test from 'node:test';
import assert from 'node:assert/strict';
import {TraceModel} from '../../src/bot/web/static/trace-model.mjs';
let n=0;
const event=(type,payload={},run='r1',session='s1')=>({id:`e${++n}`,position:n,sequence:1,type,payload,run_id:run,session_id:session,timestamp:new Date(1000*n).toISOString()});

test('replayed events are idempotent; usage is cumulative and calls are isolated',()=>{
  const m=new TraceModel();const start=event('run.started',{prompt:'work'});m.apply(start);m.apply(start);
  m.apply(event('model.usage',{input_tokens:10,output_tokens:2}));m.apply(event('model.usage',{input_tokens:30,output_tokens:5}));
  m.apply(event('tool.requested',{tool_call_id:'same',name:'read_file',arguments:{path:'a'}}));
  m.apply(event('tool.result',{tool_call_id:'same',success:true,status:'completed'}));
  m.apply(event('tool.requested',{tool_call_id:'same',name:'read_file',arguments:{path:'b'}},'r2'));
  assert.equal(m.ordered('r1').filter(n=>n.kind==='user').length,1);
  assert.equal(m.runs.get('r1').usage.input_tokens,30);
  assert.equal(m.toolsFor('r1')[0].arguments.path,'a');assert.equal(m.toolsFor('r2')[0].arguments.path,'b');
  assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'completed');
});

test('background process completion is linked to later polling without duplicate calls',()=>{
  const m=new TraceModel();m.apply(event('run.started'));
  m.apply(event('tool.requested',{tool_call_id:'launch',name:'run_command'}));
  m.apply(event('tool.completed',{tool_call_id:'launch',success:true,status:'running'}));
  m.apply(event('tool.result',{tool_call_id:'launch',success:true,status:'running',process_id:'p1'}));
  assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'running');
  m.apply(event('tool.requested',{tool_call_id:'poll',name:'poll_process',arguments:{process_id:'p1'}}));
  m.apply(event('tool.result',{tool_call_id:'poll',success:true,status:'completed',process_id:'p1',process_status:'completed',returncode:0}));
  assert.equal(m.toolsFor('r1').length,2);assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'completed');
});

test('final text, stream text and supplemental acknowledgement are not duplicated',()=>{
  const m=new TraceModel();m.apply(event('run.started',{prompt:'work'}));
  m.apply(event('assistant.delta',{step:1,text:'hello'}));m.apply(event('assistant.message',{step:1,text:'hello'}));
  m.apply(event('run.steer.queued',{message_id:'msg',text:'more'}));m.apply(event('run.steered',{message_id:'msg',text:'more'}));
  m.apply(event('run.finished',{status:'completed',final_text:'hello'}));
  assert.equal(m.ordered('r1').filter(n=>n.kind==='assistant').length,1);
  assert.equal(m.ordered('r1').filter(n=>n.content==='more').length,1);
  assert.equal(m.ordered('r1').find(n=>n.message_id==='msg').delivery,'received');
});

test('approval denial, startup gaps, cancellation and unknown events remain visible',()=>{
  const m=new TraceModel();m.apply(event('run.started'));
  m.apply(event('tool.requested',{tool_call_id:'call',name:'run_command'}));
  m.apply(event('approval.requested',{tool_call_id:'call',approval_id:'approval'}));
  assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'awaiting_approval');
  m.apply(event('approval.resolved',{tool_call_id:'call',approval_id:'approval',approved:false}));
  m.apply(event('tool.result',{tool_call_id:'call',success:false,status:'failed'}));
  assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'denied');
  m.apply(event('tool.started',{tool_call_id:'pending',name:'read_file'}));
  m.apply(event('future.event',{anything:'<script>bad()</script>'}));
  m.apply(event('run.cancel.requested'));assert.equal(m.runs.get('r1').cancelling,true);
  m.apply(event('run.finished',{status:'cancelled'}));
  assert.equal(m.toolStatus(m.toolsFor('r1')[1]),'cancelled');
  assert.equal(m.runs.get('r1').cancelling,false);
  assert.ok(m.ordered('r1').some(n=>n.content==='future.event'));
});

test('long process streams are bounded and terminal metadata preserves retained output',()=>{
  const m=new TraceModel();m.apply(event('run.started'));
  m.apply(event('tool.result',{tool_call_id:'launch',name:'run_command',process_id:'p1',status:'running'}));
  for(let i=0;i<20;i++)m.apply(event('process.output',{process_id:'p1',stream:'stdout',data:'中'.repeat(8000)}));
  m.apply(event('process.output',{process_id:'p1',stream:'stderr',data:'warning'}));
  m.apply(event('process.updated',{process_id:'p1',status:'completed',stdout:'',stderr:'',elapsed_seconds:8,returncode:0}));
  const p=m.processFor(m.toolsFor('r1')[0]);
  assert.equal(p.stdout.length,64000);assert.equal(p.stderr,'warning');
  assert.equal(p.outputChars,160007);assert.equal(p.elapsed_seconds,8);
  assert.equal(m.toolStatus(m.toolsFor('r1')[0]),'completed');
});

test('child lifecycle on synthetic runs updates one parent timeline card',()=>{
  const m=new TraceModel();m.apply(event('run.started'));
  m.apply(event('subagent.queued',{task_id:'t1',child_session_id:'child',objective:'检查边界'}));
  m.apply(event('subagent.started',{task_id:'t1'},'subagent:t1'));
  m.apply(event('subagent.completed',{task_id:'t1',status:'completed'},'subagent:t1'));
  assert.equal(m.ordered('r1').filter(n=>n.kind==='child').length,1);
  assert.equal(m.children.get('t1').objective,'检查边界');
  assert.equal(m.children.get('t1').status,'completed');
});
