import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../../src/App.vue";
import type { AgentRun, ChatMessage } from "../../src/api/agent";
const timestamp="2026-09-27T00:00:00Z";
const conversation={conversation_id:"conv",title:"研究",created_at:timestamp,updated_at:timestamp,last_activity_preview:null};
function run(patch:Partial<AgentRun>={}):AgentRun {
  return {agent_run_id:"run",conversation_id:"conv",source_message_id:"user",source_answer_message_id:null,answer_root_message_id:null,goal:"研究",status:"SUCCEEDED",version:1,
    waiting_version:0,waiting:null,submission_id:"submission",question_message_id:null,final_message_id:"answer",stopped:false,
    pending_execution:null,executions:[],observations:[],error_message:null,outcome_unknown:false,attachments:[],result_attachments:[],created_at:timestamp,...patch};
}
function message(patch:Partial<ChatMessage>={}):ChatMessage {
  return {message_id:"answer",agent_run_id:"run",role:"ASSISTANT",phase:"answer",content_status:"complete",text:"已完成研究",sequence:2,
    created_at:timestamp,attachments:[],artifacts:[],answer_root_message_id:"answer",answer_version:1,version_count:1,...patch};
}
const response=(data:unknown,status=200)=>new Response(JSON.stringify({request_id:"req",data}),{status});
let runs:AgentRun[],history:ChatMessage[],gate:Promise<void>|null,fail:boolean;
const writes:Array<{url:string;body:Record<string,unknown>;key:string}>=[];
const wrappers:Array<ReturnType<typeof mount>>=[];
async function show(){const w=mount(App);wrappers.push(w);await flushPromises();return w;}
beforeEach(()=>{
  sessionStorage.clear();sessionStorage.setItem("materials-agent.selected-conversation.v1","conv");
  runs=[];history=[];gate=null;fail=false;writes.length=0;
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
    const url=String(input);
    if(url.endsWith('/process'))return response({epoch:'test',revision:0,segments:[],run:runs[0]??run()});
    if(url.endsWith('/ui-state'))return response({fence:{operation_id:null}});
    if(url.endsWith('/result-messages'))return response({items:[]});
    if(url.endsWith('/results/reconcile'))return response({messages:[],pending:false});
    if(init?.method==='POST'){
      const body=JSON.parse(String(init.body));writes.push({url,body,key:new Headers(init.headers).get('Idempotency-Key')??''});
      if(fail)throw new TypeError('network');
      if(url.endsWith('/conversations'))return response(conversation);
      if(url.endsWith('/messages')||url.endsWith('/regenerate')){
        runs=[run({status:gate?'RUNNING':'SUCCEEDED',final_message_id:gate?null:'answer'})];
        if(url.endsWith('/messages'))history.push(message({message_id:'user',role:'USER',phase:'user',text:String(body.content_text),sequence:1,answer_root_message_id:null,answer_version:null,version_count:0}));
        if(!gate)history.push(message());
        return response({agent_run:runs[0],submission_id:'submission',idempotency_replayed:false},202);
      }
      if(url.endsWith('/stop')){runs=[run({status:'TERMINATED',stopped:true,final_message_id:null,error_message:'已停止生成'})];return response({agent_run:runs[0],outcome:'stopped'});}

    }
    if(url.includes('/agent-runs'))return response({items:runs,next_cursor:null});
    if(url.endsWith('/messages'))return response({items:history,next_cursor:null});
    return response({items:[conversation],next_cursor:null});
  }));
});
afterEach(()=>{wrappers.splice(0).forEach(w=>w.unmount());vi.unstubAllGlobals();});
it('loads canonical messages without generation',async()=>{
  runs=[run()];history=[message()];const w=await show();
  expect(w.text()).toContain('已完成研究');expect(writes).toHaveLength(0);
  expect(w.find('[aria-label="重新生成回答"]').exists()).toBe(true);
});
it('accepts background work without advance and clears the acknowledged draft',async()=>{
  const w=await show();expect(w.get('main').classes()).toContain('app-main--empty');
  await w.get('textarea').setValue('1000 MPa 转 GPa');await w.get('form').trigger('submit');await flushPromises();
  expect(writes[0]?.body).toEqual({content_text:'1000 MPa 转 GPa',attachments:[]});
  expect(writes).toHaveLength(1);
  expect(writes.some(x=>x.url.endsWith('/advance'))).toBe(false);
  expect(w.text()).toContain('已完成研究');expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
});
it('automatically binds the ordinary reply to the current question',async()=>{
  runs=[run({status:'WAITING_FOR_USER',final_message_id:null,question_message_id:'question',waiting_version:3,waiting:{question:'温度？'}})];
  history=[message({message_id:'question',phase:'question',text:'温度？',answer_root_message_id:null,answer_version:null,version_count:0})];
  const w=await show();expect(w.text()).not.toContain('另起请求');expect(w.text()).not.toContain('取消这次请求');
  await w.get('textarea').setValue('1000 摄氏度');await w.get('form').trigger('submit');await flushPromises();
  expect(writes[0]?.body).toEqual({content_text:'1000 摄氏度',reply_to:{question_message_id:'question',waiting_version:3},attachments:[]});
  expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
});
it('stops through the backend, blocks duplicate sends and keeps published content',async()=>{
  let release=()=>{};gate=new Promise<void>(resolve=>{release=resolve;});
  history=[message({message_id:'previous',text:'原回答',sequence:0})];
  const w=await show();await w.get('textarea').setValue('研究目标');await w.get('form').trigger('submit');await flushPromises();
  expect(w.get('[aria-label="中止生成"]').attributes('disabled')).toBeUndefined();
  await w.get('form').trigger('submit');expect(writes.filter(x=>x.url.endsWith('/messages'))).toHaveLength(1);
  await w.get('[aria-label="中止生成"]').trigger('click');await flushPromises();
  expect(writes.some(x=>x.url.endsWith('/stop'))).toBe(true);
  expect(w.text()).toContain('原回答');expect(w.find('[aria-label="发送"]').exists()).toBe(true);
  expect(w.text()).not.toContain('尚未确认原提交');
  expect(w.find('[role="alert"]').exists()).toBe(false);
  expect(w.text()).not.toContain('计算仍在运行');
  release();await flushPromises();
});
it('regenerates the clicked message instead of a run-wide retry',async()=>{
  runs=[run()];history=[message({message_id:'clicked'})];const w=await show();
  await w.get('[aria-label="重新生成回答"]').trigger('click');await flushPromises();
  expect(writes[0]?.url).toBe('/api/v1/messages/clicked/regenerate');
});
it('keeps the idempotency key for uncertain acceptance',async()=>{
  fail=true;const w=await show();await w.get('textarea').setValue('研究');await w.get('form').trigger('submit');await flushPromises();
  expect(w.text()).toContain('尚未确认');const key=writes[0]?.key;
  fail=false;await w.findAll('button').find(b=>b.text()==='检查原提交')!.trigger('click');await flushPromises();
  expect(writes[1]?.key).toBe(key);expect(sessionStorage.getItem('materials-agent.pending-run.v3')).toBeNull();
});
it('renders model markup as ordinary text',async()=>{
  history=[message({text:'<img src=x onerror="alert(1)">'})];const w=await show();
  expect(w.findAll('img')).toHaveLength(0);expect(w.text()).toContain('onerror');
});

it('requires the pending tool confirmation before accepting another message',async()=>{
  runs=[run({status:'WAITING_FOR_CONFIRMATION',final_message_id:null,pending_execution:{invocation_run_id:'inv',
    tool_name:'materials_ml_train_tabular_regression',status:'PENDING_CONFIRMATION',confirmation:[],
    confirmation_version:'v',confirmation_expires_at:null,retryable:false}})];
  const w=await show();
  expect(w.get('[aria-label="发送"]').attributes('disabled')).toBeDefined();
  expect(w.findAll('button').find(b=>b.text()==='确认执行')!.attributes('disabled')).toBeUndefined();
  await w.get('form').trigger('submit');expect(writes).toHaveLength(0);
});

it('places regeneration progress beside its original answer',async()=>{
  history=[message({text:'原回答',sequence:2}),message({message_id:'later',agent_run_id:'later-run',role:'USER',phase:'user',
    text:'后续问题',sequence:3,answer_root_message_id:null,answer_version:null,version_count:0})];
  runs=[run({agent_run_id:'regeneration',status:'RUNNING',final_message_id:null,answer_root_message_id:'answer'})];
  const w=await show();const text=w.get('main').text();
  expect(text.indexOf('原回答')).toBeLessThan(text.indexOf('正在处理你的请求'));
  expect(text.indexOf('正在处理你的请求')).toBeLessThan(text.indexOf('后续问题'));
});
