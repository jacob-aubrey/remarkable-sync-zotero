// Actual controller with disposable in-memory files and process pipes only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {TextEncoder} = require('node:util');
const source = fs.readFileSync('zotero-support/ui.js', 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 1));
async function until(test) { for(let n=0;n<2000;n++){if(test())return;await tick();} throw Error('Fixture deadline'); }
function pipe() {
  const values=[], readers=[];
  return {readString:()=>values.length?Promise.resolve(values.shift()):new Promise(resolve=>readers.push(resolve)),
    push:value=>readers.length?readers.shift()(value):values.push(value)};
}
function fixture() {
  const files=new Map(), stdout=pipe(), stderr=pipe(), requests=[];
  let exit, kills=0;
  const finished=new Promise(resolve=>exit=resolve);
  const child={pid:123,stdin:{write:async value=>requests.push(JSON.parse(value))},stdout,stderr,
    wait:()=>finished,kill:async()=>{kills++;stdout.push(null);stderr.push(null);exit({exitCode:777});}};
  const missing=()=>Object.assign(Error('missing'),{name:'NotFoundError'});
  const io={stat:async p=>{if(!files.has(p))throw missing();return {size:Buffer.byteLength(files.get(p))};},
    move:async(a,b)=>{if(!files.has(a))throw missing();files.set(b,files.get(a));files.delete(a);},
    writeUTF8:async(p,text,options={})=>files.set(p,(options.mode==='appendOrCreate'?(files.get(p)||''):'')+text)};
  const context=vm.createContext({setTimeout,clearTimeout,setInterval,clearInterval,TextEncoder,
    IOUtils:io,PathUtils:{join:(...v)=>v.join('/')},
    Zotero:{Prefs:{set(){},get:name=>name==='notable.developmentWorker'?' {"command":"fixture","arguments":[]}':false},
      Server:{init:async()=>{},port:23199}},ChromeUtils:{importESModule:()=>({Subprocess:{call:async()=>child}})}});
  vm.runInContext(source,context);const ui=context.NotableUI;
  ui.statePath='fixture';ui.stopped=false;ui.message=ui.receiveProgress=()=>{};
  const records=()=>[...files.values()].flatMap(text=>text.trim().split('\n').filter(Boolean).map(JSON.parse));
  return {ui,io,files,requests,stdout,stderr,records,child,get kills(){return kills;},
    end:code=>{stdout.push(null);stderr.push(null);exit({exitCode:code});}};
}
async function main(){
  {
    const f=fixture(), response=f.ui.rpc('sync');await until(()=>f.requests.length);
    const request=f.requests[0];assert.ok(request.trace_id);
    f.stderr.push("No module named 'httpx2'\nPDF_PRIVATE_TEXT PatientPrivateError token=credential https://private.invalid/signed\n");
    f.stdout.push(JSON.stringify({event:'progress',message:'Still working...'})+'\n');
    f.stdout.push(JSON.stringify({id:request.id,result:{complete:true}})+'\n');
    assert.equal((await response).complete,true);f.end(0);await tick();await f.ui.transportLog;
    const records=f.records();assert.ok(records.some(r=>r.event==='heartbeat_received'));
    assert.ok(records.some(r=>r.event==='rpc_finished'&&r.trace_id===request.trace_id));
    assert.ok(records.some(r=>r.event==='helper_exit'&&r.exit_code===0&&!r.parent_initiated));
    assert.ok(records.some(r=>r.event==='helper_stderr'&&r.missing_modules.includes('httpx2')));
    assert.ok(!JSON.stringify(records).includes('PDF_PRIVATE_TEXT'));
    assert.ok(!JSON.stringify(records).includes('PatientPrivateError'));
    assert.ok(!JSON.stringify(records).includes('credential'));
  }
  {
    const f=fixture();f.ui.requestTimeout=15;
    await assert.rejects(f.ui.rpc('sync'),/stopped responding/);await tick();await f.ui.transportLog;
    assert.equal(f.kills,1);const records=f.records();
    assert.ok(records.some(r=>r.event==='rpc_timeout'&&r.method==='sync'));
    assert.ok(records.some(r=>r.event==='parent_kill'&&r.reason==='inactivity_timeout'));
    assert.ok(records.some(r=>r.event==='helper_exit'&&r.parent_initiated));
  }
  {
    const f=fixture(), response=f.ui.rpc('sync');await until(()=>f.requests.length);
    f.stdout.push('PDF_PRIVATE_TEXT malformed\n');await assert.rejects(response,/invalid response/);
    await tick();await f.ui.transportLog;const records=f.records();
    assert.ok(records.some(r=>r.event==='invalid_response'));
    assert.ok(!JSON.stringify(records).includes('PDF_PRIVATE_TEXT'));assert.equal(f.kills,1);
  }
  {
    const f=fixture(), response=f.ui.rpc('sync');await until(()=>f.requests.length);f.end(3221225477);
    await assert.rejects(response,/helper stopped/);await tick();await f.ui.transportLog;
    assert.equal(f.kills,0);assert.ok(f.records().some(r=>r.event==='helper_exit'&&r.exit_code===3221225477&&!r.parent_initiated));
    assert.ok(f.records().some(r=>r.event==='stdout_eof'));
  }
  {
    const f=fixture();f.ui.transportMaxBytes=1500;
    for(let n=0;n<50;n++)await f.ui.recordTransport('bounded',{ordinal:n,message:'test'});
    assert.ok(f.files.size<=3);assert.ok([...f.files.values()].every(v=>Buffer.byteLength(v)<=1500));
    assert.ok(f.records().length>0);
    f.io.writeUTF8=async()=>{throw Error('disk full');};await f.ui.recordTransport('failed_write');
    const response=f.ui.rpc('pause_sync');await until(()=>f.requests.length);
    f.stdout.push(JSON.stringify({id:f.requests[0].id,result:{paused:true}})+'\n');assert.equal((await response).paused,true);
    f.end(0);await tick();await f.ui.transportLog;
  }
  console.log('PASS: correlation, normal completion, heartbeat, timeout/parent kill, native stderr privacy, malformed response, EOF/exit code, rotation and logging failure');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
