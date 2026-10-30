import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

const source=readFileSync(new URL('../app.js',import.meta.url),'utf8').replace(/^import .*?;\r?\n/,'');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const view=id=>({request_id:id,title:'Property-P1 '+id,status:'assessed',priority:'routine',version:2,events:[]});

function harness(respond) {
  class Node {
    innerHTML=''; textContent=''; dataset={}; disabled=false; listeners={}; children=[];
    elements=Object.fromEntries(['role','property','token','scheduler','operator_token','api_key','base_url','model'].map(name=>[name,{value:''}]));
    querySelectorAll(){return [];}
    addEventListener(type,callback){this.listeners[type]=callback;}
    append(node){this.children.push(node);}
    showModal(){} close(){} reset(){}
  }
  const nodes=new Map(); const listeners={}; const calls=[];
  const document={
    getElementById(id){if(!nodes.has(id)) nodes.set(id,new Node());return nodes.get(id);},
    querySelectorAll(){return [];},createElement(){return new Node();},
    addEventListener(type,callback){listeners[type]=callback;},
  };
  class Data {
    constructor(form){this.values=new Map(Object.entries(form?.fields||{}));}
    append(key,value){this.values.set(key,value);}
    [Symbol.iterator](){return this.values[Symbol.iterator]();}
  }
  const context=vm.createContext({document,api:async(path,options)=>{calls.push({path,options});return respond(path,options);},
    FormData:Data,crypto:{randomUUID},confirm:()=>true,setTimeout,URL,console});
  vm.runInContext(source+'\nglobalThis.ui={state,render,requestDetail,documentDetail,reportDetail,timeline,modelAction,documentCategory};',context);
  const ui=context.ui;
  Object.assign(ui.state,{token:'manager-P1',property:'P1',role:'manager',scheduler:'ops-P1',epoch:1});
  function switchSession() {
    const form=document.getElementById('sessionForm');
    for(const [key,value] of Object.entries({role:'tenant',property:'P2',token:'tenant-P2',scheduler:''})) form.elements[key].value=value;
    form.onsubmit({preventDefault(){},currentTarget:form});
  }
  return {ui,nodes,document,listeners,calls,switchSession};
}

for(const handler of ['requestDetail','documentDetail','reportDetail','timeline']) {
  test('delayed '+handler+' cannot restore P1 data after switching to P2 tenant',async()=>{
    const late=deferred();const h=harness(path=>path.includes('/P1-record')?late.promise:[]);
    const pending=h.ui[handler]('P1-record');
    const rejected=assert.rejects(pending,error=>error.stale===true);
    h.switchSession();await flush();late.resolve(view('P1-record'));await rejected;
    assert.equal(h.ui.state.role,'tenant');assert.equal(h.ui.state.property,'P2');
    assert.equal(h.ui.state.selectedRequest,null);assert.equal(h.ui.state.selectedDocument,null);assert.equal(h.ui.state.selectedReport,null);
    assert.equal(h.nodes.get('pageTitle').textContent,'Dashboard');
    assert.ok(!h.nodes.get('content').innerHTML.includes('P1-record'));
  });
}

test('new navigation in the same session invalidates an earlier detail response',async()=>{
  const late=deferred();const h=harness(path=>path.endsWith('/old')?late.promise:[]);
  const pending=h.ui.requestDetail('old');const rejected=assert.rejects(pending,error=>error.stale===true);
  await h.ui.render('documents');late.resolve(view('old'));await rejected;
  assert.equal(h.nodes.get('pageTitle').textContent,'Documents & Evidence');
  assert.equal(h.ui.state.selectedRequest,null);
});

test('a newer detail selection wins even within the same session',async()=>{
  const late=deferred();const h=harness(path=>path.endsWith('/old')?late.promise:view('current'));
  const pending=h.ui.requestDetail('old');const rejected=assert.rejects(pending,error=>error.stale===true);
  h.ui.state.epoch++;await h.ui.requestDetail('current');late.resolve(view('old'));await rejected;
  assert.equal(h.ui.state.selectedRequest.request_id,'current');
  assert.ok(h.nodes.get('content').innerHTML.includes('current'));
});

test('timeline cannot render stale evidence after its first request already completed',async()=>{
  const late=deferred();const h=harness(path=>path.includes('/maintenance-timeline?')?late.promise:path.endsWith('/P1-record')?view('P1-record'):[]);
  const pending=h.ui.timeline('P1-record');const rejected=assert.rejects(pending,error=>error.stale===true);
  await flush();h.switchSession();await flush();late.resolve([{event_id:'private-P1'}]);await rejected;
  assert.equal(h.ui.state.selectedRequest,null);
  assert.ok(!h.nodes.get('content').innerHTML.includes('private-P1'));
});

const forms=[
  ['messageForm',{content:'P1 message'}],
  ['maintenanceForm',{title:'P1 repair',description:'P1',location:'Kitchen',evidence_refs:''}],
  ['uploadForm',{}],
  ['statusForm',{target_status:'approved',reason:'P1 approval',evidence_refs:''}],
  ['assessmentForm',{priority:'routine',rationale:'P1 assessment'}],
  ['evidenceReviewForm',{status:'select_category',category:'invoice'}],
  ['reviewForm',{final_content:'P1 final content',reason:'P1 decision'}],
  ['reportForm',{period:'2026-10',reason:'P1 monthly report'}],
];
for(const [id,fields] of forms) {
  test('delayed '+id+' result cannot mutate or navigate the new session',async()=>{
    const late=deferred();const h=harness((path,options)=>options.method==='POST'?late.promise:[]);
    h.ui.state.selectedRequest=view('P1-request');h.ui.state.selectedDocument={document_id:'P1-document'};
    const control={value:id==='reportForm'?'generate':'approved',disabled:false};let resets=0;
    const form={id,dataset:{id:'P1-draft',version:'1'},fields,elements:{file:{files:['test-file']}},
      classList:{contains:name=>id==='reviewForm'&&name==='reviewForm'},querySelectorAll:()=>[control],reset(){resets++;}};
    const pending=h.listeners.submit({target:form,submitter:control,preventDefault(){}});
    h.switchSession();await flush();
    late.resolve({status:'registered',request_id:'P1-request',document:{document_id:'P1-document'},
      report:{report_id:'P1-report'},message_id:'P1-message',draft_id:'P1-draft',reply_status:'pending_review',delivery_status:'not_dispatched'});
    await pending;await flush();
    assert.equal(h.ui.state.receipts.length,0);assert.equal(h.ui.state.decisions.length,0);
    assert.equal(h.ui.state.selectedRequest,null);assert.equal(h.ui.state.selectedDocument,null);assert.equal(h.ui.state.selectedReport,null);
    assert.equal(h.nodes.get('pageTitle').textContent,'Dashboard');assert.equal(resets,0);
    assert.equal(h.nodes.get('feedback').innerHTML,'');
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
    assert.ok(!h.calls.some(c=>c.path.includes('P1-request')&&c.options.method!=='POST'));
  });
}

test('old-session request errors do not replace the new session feedback',async()=>{
  const late=deferred();const h=harness(()=>late.promise);
  const b={disabled:false,textContent:'Open',dataset:{action:'request',id:'private-P1'}};
  const pending=h.listeners.click({target:{closest:selector=>selector==='[data-action]'?b:null}});
  // Use a direct context change here: the new page may already be displayed.
  Object.assign(h.ui.state,{token:'tenant-P2',property:'P2',role:'tenant',epoch:h.ui.state.epoch+1});
  h.nodes.get('feedback').innerHTML='New-session feedback';
  late.reject(new Error('P1 secret failure'));await pending;
  assert.equal(h.nodes.get('feedback').innerHTML,'New-session feedback');
});

test('scheduler callbacks do not repopulate operations in another session',async()=>{
  const late=deferred();const h=harness((path,options)=>options.method==='POST'?late.promise:[]);
  const b={disabled:false,textContent:'Scan',dataset:{action:'scan',id:''}};
  const pending=h.listeners.click({target:{closest:selector=>selector==='[data-action]'?b:null}});
  h.switchSession();await flush();late.resolve({drafts_created:2});await pending;
  assert.equal(h.ui.state.operations.length,0);assert.equal(h.nodes.get('feedback').innerHTML,'');
});

test('closing and reopening model settings discards an earlier response',async()=>{
  const late=deferred();const h=harness(()=>late.promise);
  h.nodes.get('modelSettingsButton').onclick();
  const pending=h.ui.modelAction(null,'load');
  h.nodes.get('modelSettingsDialog').listeners.close();
  h.nodes.get('modelSettingsButton').onclick();
  late.resolve({base_url:'https://old.example/v1',model:'old-model',configured:true,api_key_configured:true,source:'runtime'});
  await pending;
  assert.equal(h.nodes.get('modelSettingsForm').elements.base_url.value,'');
  assert.ok(!h.nodes.get('modelSettingsResult').innerHTML.includes('old-model'));
});

test('a current detail response still renders normally',async()=>{
  const h=harness(()=>view('current'));
  await h.ui.requestDetail('current');
  assert.equal(h.ui.state.selectedRequest.request_id,'current');
  assert.ok(h.nodes.get('content').innerHTML.includes('Property-P1 current'));
});

test('a current message completion can refresh and show its truthful receipt',async()=>{
  const h=harness((path,options)=>options.method==='POST'?{message_id:'current-message',reply_status:'pending_review',delivery_status:'not_dispatched'}:[]);
  Object.assign(h.ui.state,{role:'tenant',token:'tenant-P1'});
  const control={disabled:false};const form={id:'messageForm',dataset:{},fields:{content:'Current message'},
    classList:{contains:()=>false},querySelectorAll:()=>[control],reset(){}};
  await h.listeners.submit({target:form,submitter:control,preventDefault(){}});
  assert.equal(h.ui.state.receipts.length,1);
  assert.ok(h.nodes.get('feedback').innerHTML.includes('not_dispatched'));
  assert.equal(h.nodes.get('pageTitle').textContent,'Messages');
});

test('a current operations callback still refreshes and reports real counts',async()=>{
  const h=harness((path,options)=>options.method==='POST'?{drafts_created:2}:[]);
  const b={disabled:false,textContent:'Scan',dataset:{action:'scan',id:''}};
  await h.listeners.click({target:{closest:selector=>selector==='[data-action]'?b:null}});
  assert.equal(h.ui.state.operations.length,1);
  assert.ok(h.nodes.get('feedback').innerHTML.includes('drafts_created'));
});


test('automatic category is visible before optional manager review',()=>{
 const {ui}=harness(()=>[]);
 assert.equal(ui.documentCategory({classification:'classified',review_required:false,machine_category:'invoice',final_category:null}), 'invoice');
 assert.equal(ui.documentCategory({classification:'classified',review_required:false,machine_category:'invoice',final_category:'lease_document'}), 'lease_document');
 assert.equal(ui.documentCategory({classification:'failed',review_required:true,machine_category:null}), 'Manual review required');
 assert.equal(ui.documentCategory({classification:'classified',review_required:null,machine_category:'invoice'}), 'Not available');
});
