import { test } from 'node:test'
import assert from 'node:assert/strict'
import { reportDraftClient, createDraftSession, isWordExportCommand, reportMessageIntent } from '../src/reportDrafts.ts'

const chapter = text => ({id:'planning',title:'第四章 规划研判',paragraphs:[text],table_headers:[],table_rows:[]})
const draft = (version=1) => ({ report_id:'tongji',source_hash:'a'.repeat(64),version,title:'体检报告',mode:'report_facts',notice:'未配置模型',updated_at:'2026-10-27',facts:{score:55},chapters:[chapter('当前建议')] })
const response = (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}})

test('client reads source-bound drafts, sends expected version, streams chapters and exports actual Word',async()=>{
  const calls=[];const steps=[]
  const client=reportDraftClient('http://localhost:8769',async(url,init)=>{
    calls.push({path:url.pathname,method:init.method,body:init.body?JSON.parse(init.body):undefined})
    if(url.pathname.endsWith('/export'))return new Response(new Blob(['PK-word']),{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}})
    if(init.method==='GET')return response({detail:{code:'NO_DRAFT'}},404)
    return new Response([{type:'stage',message:'读取保存报告',stage:'source'},{type:'chapter',chapter:chapter('预览')},{type:'result',result:draft(2)}].map(x=>JSON.stringify(x)).join('\n'),{headers:{'Content-Type':'application/x-ndjson'}})
  })
  const signal=new AbortController().signal
  assert.equal(await client.get('tongji',signal),null)
  const edited=await client.edit('tongji',{action:'revise',instruction:'聚焦养老',expected_version:1},signal,e=>steps.push(e))
  assert.equal(edited.version,2)
  assert.equal(steps[1].chapter.paragraphs[0],'预览')
  const file=await client.export('tongji',2,signal)
  assert.equal(await file.text(),'PK-word')
  assert.deepEqual(calls.map(x=>x.method),['GET','POST','POST'])
  assert.deepEqual(calls[1].body,{action:'revise',instruction:'聚焦养老',expected_version:1})
  assert.deepEqual(calls[2].body,{expected_version:2})
})

test('two edits and export use the single confirmed version; failure keeps prior draft',async()=>{
  const versions=[];let fail=false;let last
  const session=createDraftSession({binding:()=>({id:'tongji',base:'http://local'}),update:s=>last=s,client:()=>({
    get:async()=>null,
    async edit(id,payload,signal,onEvent){
      versions.push(payload.expected_version)
      onEvent({type:'chapter',chapter:chapter('未保存预览')})
      if(fail)throw new Error('模型失败，旧版保留')
      return draft(payload.expected_version+1)
    },
    export:async(id,version)=>{versions.push(version);return new Blob(['word'])},
  })})
  await session.loadOrGenerate();assert.equal(last.current.version,1)
  await session.revise('第四章聚焦养老');await session.revise('改成内部汇报口吻')
  assert.equal(last.current.version,3)
  fail=true;await session.revise('进一步修改')
  assert.equal(last.current.version,3);assert.match(last.error,/模型失败/)
  assert.deepEqual(last.preview,[])
  assert.ok(await session.export())
  assert.deepEqual(versions,[0,1,2,3,3])
})

test('returning to the saved case restores its current draft without generating again',async()=>{
  let edits=0,last
  const session=createDraftSession({binding:()=>({id:'tongji',base:'http://local'}),update:s=>last=s,client:()=>({get:async()=>draft(3),edit:async()=>{edits++;return draft()},export:async()=>new Blob()})})
  await session.loadOrGenerate();assert.equal(last.current.version,3);assert.equal(edits,0)
})

test('switching report discards late results and isolates old draft state',async()=>{
  const binding={id:'tongji',base:'http://a'};let finish,last
  const session=createDraftSession({binding:()=>binding,update:s=>last=s,client:()=>({get:async()=>null,edit:()=>new Promise(r=>finish=r),export:async()=>new Blob()})})
  const pending=session.loadOrGenerate();await new Promise(r=>setTimeout(r,0))
  binding.id='other';session.reset();finish(draft());await pending
  assert.equal(last.current,null);assert.equal(last.busy,false);assert.deepEqual(last.preview,[])
})

test('server conflicts remain visible and non-docx downloads are rejected',async()=>{
  const client=reportDraftClient('http://localhost:8769',async()=>response({detail:{code:'VERSION_CONFLICT',message:'草稿版本已更新'}},409))
  await assert.rejects(client.edit('tongji',{action:'revise',instruction:'改写',expected_version:1},new AbortController().signal,()=>{}),/草稿版本已更新/)
  const broken=reportDraftClient('http://localhost:8769',async()=>response({not:'Word'}))
  await assert.rejects(broken.export('tongji',1,new AbortController().signal),/Word/)
  assert.equal(isWordExportCommand('请导出 Word'),true)
  assert.equal(isWordExportCommand('导出word文档'),true)
  assert.equal(isWordExportCommand('把结论写得适合Word'),false)
})


test('explicit model generation upgrades a saved facts template using its actual version',async()=>{
  let last;const calls=[]
  const session=createDraftSession({binding:()=>({id:'tongji',base:'http://local'}),update:s=>last=s,client:()=>({
    get:async()=>{calls.push('get');return draft(1)},
    edit:async(id,body)=>{calls.push(body);return {...draft(2),mode:'model'}},export:async()=>new Blob(),
  })})
  await session.loadOrGenerate();assert.equal(last.current.mode,'report_facts')
  await session.generateModel()
  assert.equal(last.current.mode,'model');assert.equal(last.current.version,2)
  assert.deepEqual(calls,['get',{action:'generate',expected_version:1}])
})

test('one composer distinguishes questions, real edit requests and export without a mode toggle',()=>{
  for(const text of ['刚才删掉了哪一章？','养老设施应该增加什么？','这个分数怎么理解','第四章为什么这样写？','为什么没有给我修改Word？','有哪些改善建议？','你建议怎么改？']) assert.equal(reportMessageIntent(text),'question',text)
  for(const text of ['恢复第三章','把第三章去掉','帮我把第三张去掉','请把第四章重写成养老专项调研清单','把结论改成内部汇报口吻','请修改这个word，增加养老设施调研建议','第四章写详细一点','能把报告缩短一些吗？','把刚才的建议写进报告']) assert.equal(reportMessageIntent(text),'revise',text)
  assert.equal(reportMessageIntent('再详细一点','report'),'revise')
  assert.equal(reportMessageIntent('再详细一点','question'),'question')
  assert.equal(reportMessageIntent('生成一份新的报告'),'generate')
  assert.equal(reportMessageIntent('请导出Word'),'export')
  assert.equal(reportMessageIntent('导出前先把第四章写详细一点'),'revise')
})

test('report snapshots keep their own export version after later edits',async()=>{
  let saved=draft(7);let last;const exported=[]
  const session=createDraftSession({binding:()=>({id:'tongji',base:'http://local'}),update:s=>last=s,client:()=>({
    get:async()=>saved, edit:async(id,body)=>{saved={...draft(body.expected_version+1),chapters:[chapter(body.instruction)]};return saved},
    export:async(id,version)=>{exported.push(version);return new Blob(['word'])},
  })})
  await session.loadOrGenerate();const first=last.current
  await session.revise('补充访谈清单');assert.equal(last.current.version,8)
  assert.equal(first.chapters[0].paragraphs[0],'当前建议')
  await session.export(first.version)
  assert.deepEqual(exported,[7])
})

test('page identity stays on reads, edits and downloads, while a fresh page uses another identity',async()=>{
  const calls=[]
  const fetcher=async(url,init)=>{
    calls.push({query:url.searchParams.get('session_id'),body:init.body?JSON.parse(init.body):null})
    if(url.pathname.endsWith('/export'))return new Response('word',{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}})
    if(init.method==='GET')return response(draft())
    return new Response(JSON.stringify({type:'result',result:draft(2)})+'\n',{headers:{'Content-Type':'application/x-ndjson'}})
  }
  const signal=new AbortController().signal
  const first=reportDraftClient('http://localhost:8769',fetcher,'page-a')
  await first.get('tongji',signal)
  await first.edit('tongji',{action:'revise',instruction:'去掉第三章',expected_version:1},signal,()=>{})
  await first.export('tongji',2,signal)
  await reportDraftClient('http://localhost:8769',fetcher,'page-b').get('tongji',signal)
  assert.deepEqual(calls.map(call=>call.query??call.body?.session_id),['page-a','page-a','page-a','page-b'])
})
