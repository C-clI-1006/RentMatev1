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
    getAll(key){const v=this.values.get(key);return v===undefined?[]:Array.isArray(v)?v:[v];}
    [Symbol.iterator](){return this.values[Symbol.iterator]();}
  }
  const context=vm.createContext({document,api:async(path,options)=>{calls.push({path,options});return respond(path,options);},
    FormData:Data,crypto:{randomUUID},confirm:()=>true,setTimeout,URL,console});
  vm.runInContext(source+'\nglobalThis.ui={state,render,requestDetail,documentDetail,reportDetail,timeline,modelAction,documentCategory,documentReviewStatus,documentReviewForm,recordLinks,propertyTimeline,evidencePicker,selectedEvidence};',context);
  const ui=context.ui;
  Object.assign(ui.state,{token:'manager-P1',property:'P1',role:'manager',scheduler:'ops-P1',epoch:1});
  function switchSession() {
    const form=document.getElementById('sessionForm');
    for(const [key,value] of Object.entries({role:'tenant',property:'P2',token:'tenant-P2',scheduler:''})) form.elements[key].value=value;
    form.onsubmit({preventDefault(){},currentTarget:form});
  }
  return {ui,nodes,document,listeners,calls,switchSession};
}

test('Review Centre separates escaped handoff cases from reply approvals',async()=>{
  const h=harness(path=>path==='/api/reviews/handoffs'?[{case_id:'case-1',message_id:'message-1',status:'open',
    original_message:'<script>untrusted</script>',reason:'classifier unavailable'}]:[]);
  await h.ui.render('reviews');
  const html=h.nodes.get('content').innerHTML;
  assert.ok(html.includes('Human handoffs'));
  assert.ok(html.includes('&lt;script&gt;untrusted&lt;/script&gt;'));
  assert.ok(!html.includes('<script>untrusted'));
  assert.ok(html.includes('Resolve case') && html.includes('Dismiss case'));
  assert.ok(html.includes('does not approve or send a reply'));
  assert.ok(!html.includes('Approve exact version'));
});

test('handoff submission is single-flight and uses the scoped resolution API',async()=>{
  const late=deferred(); const h=harness((path,options)=>options.method==='POST'?late.promise:[]);
  const control={value:'resolved',disabled:false};
  const form={id:'',dataset:{id:'case-1'},fields:{resolution:'Handled offline'},
    classList:{contains:name=>name==='handoffForm'},querySelectorAll:()=>[control]};
  const event={target:form,submitter:control,preventDefault(){}};
  const pending=h.listeners.submit(event);
  await h.listeners.submit(event);
  assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
  const call=h.calls.find(c=>c.options.method==='POST');
  assert.equal(call.path,'/api/reviews/handoffs/case-1/resolution');
  assert.equal(call.options.body.outcome,'resolved');
  assert.equal(call.options.body.resolution,'Handled offline');
  late.resolve({status:'resolved'});await pending;
  assert.ok(h.nodes.get('feedback').innerHTML.includes('No reply was approved or sent'));
});

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
  ['evidenceLinkForm',{target:JSON.stringify({target_type:'lease',target_id:'P1-lease'})}],
  ['reviewForm',{final_content:'P1 final content',reason:'P1 decision'}],
  ['handoffForm',{resolution:'P1 handled offline'}],
  ['reportForm',{period:'2026-10',reason:'P1 monthly report'}],
];
for(const [id,fields] of forms) {
  test('delayed '+id+' result cannot mutate or navigate the new session',async()=>{
    const late=deferred();const h=harness((path,options)=>options.method==='POST'?late.promise:[]);
    h.ui.state.selectedRequest=view('P1-request');h.ui.state.selectedDocument={document_id:'P1-document'};
    const control={value:id==='reportForm'?'generate':id==='handoffForm'?'resolved':'approved',disabled:false};let resets=0;
    const form={id,dataset:{id:'P1-draft',version:'1'},fields,elements:{file:{files:['test-file']}},
      classList:{contains:name=>['reviewForm','handoffForm'].includes(id)&&name===id},querySelector:()=>null,querySelectorAll:()=>[control],reset(){resets++;}};
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


test('accepted automatic results require no manager confirmation',async()=>{
 const doc={document_id:'invoice-1',original_filename:'invoice.pdf',classification:'classified',review_required:false,machine_category:'invoice',final_category:null};
 const h=harness(()=>doc);
 assert.equal(h.ui.documentReviewStatus(doc),'Not required');
 await h.ui.documentDetail(doc.document_id);
 const html=h.nodes.get('content').innerHTML;
 assert.ok(html.includes('<dt>Category</dt><dd>invoice</dd>'));
 assert.ok(html.includes('<dt>Manager review</dt><dd>Not required</dd>'));
 assert.ok(html.includes('<details id="classificationCorrection"><summary>Correct classification (optional)</summary>'));
 assert.ok(!html.includes('id="classificationCorrection" open'));
 assert.ok(!html.includes('Confirm stored suggestion'));
});

test('uncertain and failed results show required review without optional collapse',()=>{
 const {ui}=harness(()=>[]);
 for(const doc of [
  {classification:'failed',review_required:true,machine_category:null},
  {classification:'classified',review_required:true,machine_category:'invoice'},
  {classification:'classified',review_required:null,machine_category:'invoice'}
 ]) {
  assert.equal(ui.documentReviewStatus(doc),'Review required');
  const form=ui.documentReviewForm(doc);
  assert.ok(form.includes('Manual review required'));
  assert.ok(form.includes('Save classification review'));
  assert.ok(!form.includes('<details'));
 }
});

test('human corrections remain reviewed while pending outcomes do not claim success',()=>{
 const {ui}=harness(()=>[]);
 assert.equal(ui.documentReviewStatus({classification:'failed',review_required:false,final_category:'invoice',reviewed_by:'manager'}),'Reviewed');
 assert.equal(ui.documentReviewStatus({classification:'pending',review_required:null}),'Pending classification');
 assert.ok(!ui.documentReviewForm({classification:'pending',review_required:null}).includes('<form'));
});

test('guided workflow completes automatic classification without a manager decision',async()=>{
 const doc={document_id:'invoice-guided',classification:'classified',review_required:false,machine_category:'invoice',final_category:null};
 const h=harness(path=>path.includes('/api/evidence/documents')?[doc]:[]);
 h.ui.state.selectedDocument=doc;
 await h.ui.render('guided');
 const html=h.nodes.get('content').innerHTML;
 assert.ok(html.includes('<li class="done"><div>Accept classification or resolve review'));
 assert.ok(html.includes('invoice · Not required'));
 assert.ok(!html.includes('Manager settles the category'));
});

test('property evidence history remains visible without a Maintenance API response',async()=>{
  const h=harness(path=>path.includes('/evidence/timeline')?[{kind:'uploaded',filename:'<img src=x>',occurred_at:'2026-10-30T00:00:00Z',document_id:'doc-1',actor:'manager:P1'}]:Promise.reject(new Error('Maintenance unavailable')));
  await h.ui.render('activity');
  const html=h.nodes.get('content').innerHTML;
  assert.ok(html.includes('Property evidence timeline') && html.includes('&lt;img src=x&gt;'));
  assert.ok(html.includes('Open document'));
  assert.ok(!html.includes('<img src=x>'));
});

test('record choices and link history are escaped and remain scoped',async()=>{
  const h=harness(path=>path.includes('/link-targets')?[{target_type:'lease',target_id:'lease-1',label:'<script>label</script>'}]:[]);
  h.ui.state.selectedDocument={document_id:'doc-1',classification:'classified',machine_category:'invoice',review_required:false};
  await h.ui.recordLinks('doc-1');
  const html=h.nodes.get('documentLinks').innerHTML;
  assert.ok(html.includes('Unlinked') && html.includes('Link document'));
  assert.ok(html.includes('&lt;script&gt;label&lt;/script&gt;'));
  assert.ok(!html.includes('<script>label</script>'));
  assert.ok(h.calls.every(c=>c.path.includes('property_id=P1')));
});

test('a delayed link panel cannot show old-property record choices after session change',async()=>{
  const late=deferred();const h=harness(path=>path.includes('/evidence/')?late.promise:[]);
  h.ui.state.selectedDocument={document_id:'doc-1'};
  const pending=h.ui.recordLinks('doc-1');const rejected=assert.rejects(pending,e=>e.stale===true);
  h.switchSession();await flush();late.resolve([]);await rejected;
  assert.ok(!h.nodes.has('documentLinks'));
  assert.equal(h.ui.state.selectedDocument,null);
});

test('uncertain documents cannot display the manual association form',async()=>{
  const h=harness(()=>[]);
  h.ui.state.selectedDocument={document_id:'doc-1',classification:'failed',review_required:true};
  await h.ui.recordLinks('doc-1');
  assert.ok(h.nodes.get('documentLinks').innerHTML.includes('Complete classification review'));
  assert.ok(!h.nodes.get('documentLinks').innerHTML.includes('evidenceLinkForm'));
});

test('evidence choices show escaped file names and preserve previous references',()=>{
 const h=harness(()=>[]);
 const html=h.ui.evidencePicker({documents:[{document_id:'file-1',original_filename:'<img src=x>.pdf',classification:'classified',review_required:false,machine_category:'invoice',uploaded_at:'2026-10-31T00:00:00Z'}]},['file-1','missing-file']);
 assert.ok(html.includes('&lt;img src=x&gt;.pdf') && !html.includes('<img src=x>'));
 assert.ok(html.includes('value="file-1" checked'));
 assert.ok(html.includes('value="missing-file" checked'));
 assert.ok(html.includes('Previously referenced document (unavailable)'));
 assert.ok(!html.includes('input name="evidence_refs" placeholder'));
});

test('tenant intake submits every selected document ID and no document metadata',async()=>{
 const h=harness((path,o)=>o.method==='POST'?{status:'registered',request_id:'new-request'}:path.includes('/requests/new-request')?view('new-request'):[]);
 Object.assign(h.ui.state,{role:'tenant',token:'tenant-P1'});
 const control={disabled:false};const form={id:'maintenanceForm',dataset:{},fields:{title:'Repair',description:'Loose handle',location:'Kitchen',evidence_refs:['one','two','one']},
  querySelector:()=>null,classList:{contains:()=>false},querySelectorAll:()=>[control],reset(){}};
 await h.listeners.submit({target:form,submitter:control,preventDefault(){}});
 const sent=h.calls.find(c=>c.options.method==='POST');
 assert.deepEqual(Array.from(sent.options.body.evidence_refs),['one','two']);
 assert.equal(sent.options.token,'tenant-P1');
 assert.deepEqual(Object.keys(sent.options.body).sort(),['description','evidence_refs','location','title']);
});

test('manager status sends selected documents with the displayed expected version',async()=>{
 const h=harness((path,o)=>o.method==='POST'?{persisted_status:'approved'}:path.includes('/requests/current')?view('current'):[]);
 h.ui.state.selectedRequest=view('current');
 const control={disabled:false};const form={id:'statusForm',dataset:{},fields:{target_status:'approved',reason:'Reviewed',evidence_refs:['inspection','invoice']},
  querySelector:()=>null,classList:{contains:()=>false},querySelectorAll:()=>[control]};
 await h.listeners.submit({target:form,submitter:control,preventDefault(){}});
 const sent=h.calls.find(c=>c.options.method==='POST');
 assert.deepEqual(Array.from(sent.options.body.evidence_refs),['inspection','invoice']);
 assert.equal(sent.options.body.expected_version,2);
 assert.equal(sent.options.token,'manager-P1');
});

test('failed document loading disables intake instead of silently omitting references',async()=>{
 const h=harness(path=>path.includes('/evidence/documents')?Promise.reject(new Error('Unavailable')):[]);
 Object.assign(h.ui.state,{role:'tenant',token:'tenant-P1'});
 await h.ui.render('maintenance');
 const html=h.nodes.get('content').innerHTML;
 assert.ok(html.includes('data-evidence-unavailable') && html.includes('<button disabled>Submit maintenance request'));
 assert.throws(()=>h.ui.selectedEvidence({querySelector:()=>({})}),/could not be loaded/);
 assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);
});

test('detail waits for scoped documents and keeps latest event references checked',async()=>{
 const h=harness(path=>path.includes('/evidence/documents')?[{document_id:'file-1',upload_status:'saved',original_filename:'invoice.pdf'}]:{...view('current'),events:[{evidence_refs:['old-ref']},{evidence_refs:['file-1']}]});
 await h.ui.requestDetail('current');
 const html=h.nodes.get('content').innerHTML;
 assert.ok(html.includes('value="file-1" checked'));
 assert.ok(!html.includes('value="old-ref" checked'));
 assert.ok(h.calls.some(c=>c.path==='/api/evidence/documents?property_id=P1'));
});

test('late document choices cannot restore an old manager request after switching session',async()=>{
 const late=deferred();const h=harness(path=>path.includes('/evidence/documents?property_id=P1')?late.promise:path.includes('/requests/P1-record')?view('P1-record'):[]);
 const pending=h.ui.requestDetail('P1-record');const rejected=assert.rejects(pending,e=>e.stale===true);
 h.switchSession();await flush();late.resolve([{document_id:'private-file',upload_status:'saved',original_filename:'PRIVATE.pdf'}]);await rejected;
 assert.equal(h.ui.state.selectedRequest,null);
 assert.ok(!h.nodes.get('content').innerHTML.includes('PRIVATE.pdf'));
});
