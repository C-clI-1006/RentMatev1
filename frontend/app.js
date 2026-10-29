import {api} from './api.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const enc = encodeURIComponent;
const state = {role:'tenant', property:'', token:'', scheduler:'', page:'home', receipts:[], decisions:[], operations:[], generationKeys:{}, selectedRequest:null, selectedDocument:null, selectedReport:null, epoch:0};
const navigation = {
  tenant:[['home','⌂','Dashboard'],['messages','✉','Messages'],['maintenance','◇','Maintenance'],['documents','▤','Documents'],['notifications','◉','Notifications'],['guided','↗','Guided Workflows']],
  manager:[['home','⌂','Overview'],['messages','✉','Communications'],['maintenance','◇','Maintenance'],['lease','▦','Lease & Rent'],['documents','▤','Documents & Evidence'],['reviews','✓','Review Centre'],['reports','▥','Reports'],['activity','◷','Activity Timeline'],['guided','↗','Guided Workflows']]
};
const descriptions = {home:'Your property, with the important details in view.',messages:'Conversations, clear next steps and truthful reply status.',maintenance:'From first report to resolution, with a recorded history.',documents:'Original files, classification outcomes and human review.',notifications:'Replies delivered to your local tenant inbox.',lease:'Overdue rent and lease deadlines, through the reminder workflow.',reviews:'Your decision, on the exact version in front of you.',reports:'Structured property data and a separate narrative, month by month.',activity:'Persisted events for one selected maintenance request.',guided:'Four connected journeys. You choose every next action.'};
const badge = status => '<span class="badge '+(['delivered','approved','closed','accepted','complete','classified','saved'].includes(status)?'good':['failed','rejected','reject','cancelled'].includes(status)?'bad':['pending_review','under_review','partial_success','pending_retry','missing'].includes(status)?'warn':'')+'">'+esc(status ?? 'unavailable')+'</span>';
const date = value => value ? new Date(value).toLocaleString() : 'Unavailable';
const technical = data => '<details><summary>View technical details</summary><pre>'+esc(JSON.stringify(data,null,2))+'</pre></details>';
const empty = message => '<div class="empty">'+esc(message)+'</div>';
const notice = message => '<div class="notice">'+esc(message)+'</div>';
const button = (label, action, id='', secondary=true) => '<button class="'+(secondary?'secondary ':'')+'small" data-action="'+action+'" data-id="'+esc(id)+'">'+esc(label)+'</button>';
const request = (path, options={}) => api(path,{token:state.token,...options});
const ops = (path) => api(path,{token:state.scheduler,method:'POST'});
const propQuery = () => 'property_id='+enc(state.property);
const refs = value => value.split(',').map(s=>s.trim()).filter(Boolean);
function errorMessage(error) {
  const prefix = error.status === 409 ? 'This record changed or conflicts with an existing action. Refresh before trying again. ' : [401,403].includes(error.status) ? 'Your session is not authorized for this action. ' : error.retryable ? 'The service is temporarily unavailable. Read the result before retrying. ' : '';
  return prefix + error.message;
}
function flash(message,error=false) { $('feedback').innerHTML='<div class="notice '+(error?'error':'')+'">'+esc(message)+'</div>'; }
async function busy(control, work) {
  if(control.disabled) return;
  control.disabled=true; const original=control.textContent; control.textContent='Working…';
  try { await work(); } catch(error) { flash(errorMessage(error),true); }
  finally { control.disabled=false; control.textContent=original; }
}
function nav() {
  $('navigation').innerHTML=navigation[state.role].map(([id,icon,label])=>'<button data-page="'+id+'" class="'+(state.page===id?'active':'')+'"><span class="nav-icon">'+icon+'</span>'+label+'</button>').join('');
  $('roleIndicator').textContent=state.role==='tenant'?'Tenant portal':'Manager portal';
  $('propertyContext').textContent=state.property || 'Not connected';
}
async function render(page=state.page) {
  state.page=page; const epoch=++state.epoch; nav();
  const item=navigation[state.role].find(x=>x[0]===page);
  $('pageTitle').textContent=item?.[2] || 'Workspace'; $('pageSubtitle').textContent=descriptions[page] || '';
  $('feedback').innerHTML='';
  if(!state.token) { $('content').innerHTML='<section class="welcome"><span class="eyebrow">WELCOME TO RENTMATE</span><h2>Everything starts with your property.</h2><p>Connect your local demo session to manage requests, conversations, documents and the decisions that move them forward.</p>'+button('Connect workspace','session','',false)+'</section>'; return; }
  $('content').innerHTML='<div class="loading" role="status">Loading your workspace…</div>';
  try {
    const html=await ({home, messages, maintenance, documents, notifications, lease, reviews, reports, activity, guided}[page])();
    if(epoch===state.epoch) {
      $('content').innerHTML=html;
      if(page==='guided') document.querySelectorAll('.steps').forEach(list=>{
        const next=list.querySelector('li:not(.done)');
        if(next) { const label=document.createElement('small'); label.className='badge warn'; label.textContent='Current step'; next.append(label); }
      });
    }
  } catch(error) { if(epoch===state.epoch) { $('content').innerHTML=empty('This view could not be loaded. Refresh after checking your session.'); flash(errorMessage(error),true); } }
}
function maintenanceTable(items) {
  return items.length ? '<div class="table-wrap"><table><thead><tr><th>Request</th><th>Status</th><th>Priority</th><th>Updated</th><th></th></tr></thead><tbody>'+items.map(v=>'<tr><td><strong>'+esc(v.title)+'</strong><small>'+esc(v.location || 'Location not recorded')+'</small></td><td>'+badge(v.status)+'</td><td>'+badge(v.priority ?? 'awaiting assessment')+'</td><td>'+esc(date(v.updated_at))+'</td><td>'+button('Open','request',v.request_id)+'</td></tr>').join('')+'</tbody></table></div>' : empty('No maintenance requests yet.');
}
function documentTable(items) {
  return items.length ? '<div class="table-wrap"><table><thead><tr><th>Document</th><th>Machine state</th><th>Human category</th><th>Uploaded</th><th></th></tr></thead><tbody>'+items.map(d=>'<tr><td><strong>'+esc(d.original_filename)+'</strong><small>'+esc(d.document_id)+'</small></td><td>'+badge(d.classification)+'</td><td>'+esc(d.final_category || (d.review_required?'Review required':'Not reviewed'))+'</td><td>'+esc(date(d.uploaded_at))+'</td><td>'+button('Open','document',d.document_id)+'</td></tr>').join('')+'</tbody></table></div>' : empty('No documents have been uploaded for this context.');
}
async function home() {
  const sources=await Promise.allSettled([request('/api/maintenance/requests'),request('/api/evidence/documents?'+propQuery()),request(state.role==='manager'?'/api/reviews':'/api/notifications/inbox')]);
  const data=sources.map(r=>r.status==='fulfilled'?r.value:null);
  const failures=sources.map((r,i)=>r.status==='rejected'?notice(['Maintenance','Documents',state.role==='manager'?'Review queue':'Inbox'][i]+': '+errorMessage(r.reason)):'').join('');
  return '<section class="welcome"><span class="eyebrow">'+(state.role==='manager'?'YOUR PROPERTY AT A GLANCE':'YOUR HOME, LOOKED AFTER')+'</span><h2>'+(state.role==='manager'?'A little clarity for your day.':'What can we help you with?')+'</h2><p>'+(state.role==='manager'?'Review what needs your attention, follow repairs and keep your property records together.':'Send a message, report a repair or follow up on a request. Your documents and delivered replies are all here.')+'</p><div class="actions">'+button(state.role==='manager'?'Open Review Centre':'Write a message','navigate',state.role==='manager'?'reviews':'messages',false)+button('Guided Workflows','navigate','guided')+'</div></section>'+failures+'<div class="stats">'+['Maintenance requests','Stored documents',state.role==='manager'?'Pending decisions':'Delivered notifications'].map((label,i)=>'<div class="stat"><span>'+label+'</span><strong>'+(data[i]?.length ?? '—')+'</strong><span>Current authorized context</span></div>').join('')+'</div><section class="card"><div class="card-head"><h2>Recent maintenance</h2>'+button('View all','navigate','maintenance')+'</div>'+maintenanceTable((data[0]||[]).sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,4))+'</section><section class="card"><div class="card-head"><h2>Your documents</h2>'+button('View all','navigate','documents')+'</div>'+documentTable((data[1]||[]).slice(0,3))+'</section>';
}
async function messages() {
  if(state.role==='manager') {
    const queue=await request('/api/reviews');
    return notice('Communication history and classifier output do not have a read API. These are the actual pending message-origin drafts available for your review.')+reviewCards(queue.filter(q=>q.draft.message_id));
  }
  let html='<div class="grid"><section class="card"><h2>Send a message to your property team</h2><p class="muted">Describe your question or repair. RentMate will record it and determine the next step.</p><form id="messageForm"><label>Your message<textarea name="content" required maxlength="10000" placeholder="What would you like us to know?">There is a strong smell of gas in my kitchen.</textarea></label><button>Submit message</button></form><p class="hint">For immediate danger, contact emergency services directly. This prototype is not an emergency response service.</p></section><section class="card"><h2>What happens next?</h2><ol class="steps"><li><div>Your message is saved<small>A real message ID confirms intake.</small></div></li><li><div>A reply is prepared or a person takes over<small>Pending review means a manager still needs to decide.</small></div></li><li><div>Delivery is a separate step<small>Only delivered replies appear in Notifications.</small></div></li></ol>'+button('Open delivered replies','navigate','notifications')+'</section></div>';
  const receipts=state.receipts;
  let requests=[]; if(receipts.length) requests=await request('/api/maintenance/requests');
  html+='<section class="card"><h2>Submissions in this session</h2><p class="hint">Session receipts only. Message history is not available from the current API.</p>'+(receipts.length?receipts.map(r=>{
    const linked=requests.find(v=>v.message_id===r.message_id);
    return '<article class="inbox-item"><p>'+esc(r.content)+'</p><dl><dt>Message ID</dt><dd><code>'+esc(r.message_id)+'</code></dd><dt>Reply status</dt><dd>'+badge(r.reply_status)+'</dd><dt>Delivery status</dt><dd>'+badge(r.delivery_status)+'</dd><dt>Classification</dt><dd>Unavailable from the submission API</dd><dt>Emergency</dt><dd>'+(linked?esc(String(linked.emergency_flag))+' (Maintenance record)':'Unavailable')+'</dd><dt>Routing</dt><dd>'+(linked?'Maintenance · '+esc(linked.request_id):'Unavailable; no verified request link')+'</dd></dl>'+notice(r.reply_status==='pending_review'?'A manager must review this reply. It has not been sent.':r.reply_status==='auto_eligible'?'Eligible for automatic delivery. The submission response does not confirm queue acceptance or delivery.':'Human handling may be required. No delivery is confirmed.')+(linked?button('View maintenance request','request',linked.request_id):'')+technical({message_id:r.message_id,reply_status:r.reply_status,delivery_status:r.delivery_status})+'</article>';
  }).join(''):empty('Your submitted message receipts will appear here.'))+'</section>';
  return html;
}
async function maintenance() {
  const items=await request('/api/maintenance/requests');
  return (state.role==='tenant'?'<section class="card"><h2>Report a repair</h2><form id="maintenanceForm"><div class="form-row"><label>Title<input name="title" required placeholder="Kitchen tap is dripping"></label><label>Location<input name="location" placeholder="Kitchen"></label></div><label>Description<textarea name="description" required placeholder="Tell us what happened and when it started."></textarea></label><label>Existing document IDs (optional, comma separated)<input name="evidence_refs" placeholder="Upload a document first, then use its ID"></label><button>Submit maintenance request</button></form></section>':'')+'<section class="card"><div class="card-head"><h2>Maintenance requests</h2><span class="muted">'+items.length+' records</span></div>'+maintenanceTable(items)+'</section>';
}
async function requestDetail(id) {
  const v=await request('/api/maintenance/requests/'+enc(id)); state.selectedRequest=v;
  let html=button('← All requests','navigate','maintenance')+'<section class="card"><div class="card-head"><h2>'+esc(v.title)+'</h2>'+badge(v.status)+'</div><p>'+esc(v.description)+'</p><dl><dt>Location</dt><dd>'+esc(v.location || 'Not specified')+'</dd><dt>Priority</dt><dd>'+badge(v.priority ?? 'awaiting assessment')+'</dd><dt>Emergency flag</dt><dd>'+esc(String(v.emergency_flag))+'</dd><dt>Version</dt><dd>'+esc(v.version)+'</dd><dt>Request ID</dt><dd><code>'+esc(v.request_id)+'</code></dd></dl>';
  if(state.role==='manager') {
    // These are contract enum values, not an authorization decision. The server
    // remains authoritative for every transition and returns invalid/stale errors.
    html+='<div class="grid"><form id="statusForm"><h3>Update status</h3><label>Target status<select name="target_status">'+['approved','in_progress','awaiting_confirmation','closed','cancelled'].map(s=>'<option>'+s+'</option>').join('')+'</select></label><label>Reason<textarea name="reason" required></textarea></label><label>Evidence document IDs<input name="evidence_refs" placeholder="Required to close; comma separated"></label><button>Record status change</button><p class="hint">The backend enforces valid transitions and version '+esc(v.version)+'. Invalid changes are rejected.</p></form><form id="assessmentForm"><h3>Manual assessment</h3><label>Priority<select name="priority"><option>routine</option><option>urgent</option><option>emergency</option></select></label><label>Rationale<textarea name="rationale" required></textarea></label><button>Record assessment</button><p class="hint">Available for submitted, under_review or assessed requests. The server validates eligibility.</p></form></div>';
  }
  html+=technical(v)+'</section><section class="card"><h2>Request history</h2>'+events(v.events)+'</section><section class="card"><h2>Evidence references</h2>'+(v.events?.some(e=>e.evidence_refs?.length)?v.events.filter(e=>e.evidence_refs?.length).map(e=>'<p>'+esc(e.to_status)+': '+e.evidence_refs.map(id=>button('Open document '+id,'document',id)).join(' ')+'</p>').join(''):empty('No document references recorded for this request.'))+button('Inspect contextual timeline','timeline',v.request_id)+'</section>';
  $('content').innerHTML=html; $('pageTitle').textContent='Maintenance detail';
}
function events(items) {
  return items?.length?'<ul class="timeline">'+items.map(e=>'<li><strong>'+esc(e.from_status || 'New request')+' → '+esc(e.to_status)+'</strong><time>'+esc(date(e.occurred_at))+' · '+esc(e.actor)+'</time><p>'+esc(e.reason || 'Request registered')+'</p><small>Maintenance · '+esc(e.event_id)+'</small>'+(e.evidence_refs?.length?'<p>Evidence: '+e.evidence_refs.map(esc).join(', ')+'</p>':'')+'</li>').join('')+'</ul>':empty('No persisted events available.');
}
async function documents() {
  const docs=await request('/api/evidence/documents?'+propQuery());
  return '<section class="card"><h2>Upload a document</h2><p class="muted">Keep the original file and its classification outcome together. Upload success and classification success are separate.</p><form id="uploadForm"><label>Document<input name="file" type="file" required accept=".pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx,.txt"></label><p class="hint">PDF, image, Office and text files are supported. Classification may need manual review.</p><button>Upload document</button></form></section><section class="card"><h2>Property documents</h2>'+documentTable(docs)+'</section>';
}
async function documentDetail(id) {
  const d=await request('/api/evidence/documents/'+enc(id)+'?'+propQuery()); state.selectedDocument=d;
  $('content').innerHTML=button('← All documents','navigate','documents')+'<section class="card"><h2>'+esc(d.original_filename)+'</h2><dl><dt>Document ID</dt><dd><code>'+esc(d.document_id)+'</code></dd><dt>Upload</dt><dd>'+badge(d.upload_status)+'</dd><dt>Classification state</dt><dd>'+badge(d.classification)+'</dd><dt>Review required</dt><dd>'+esc(d.review_required===null?'Not recorded':String(d.review_required))+'</dd><dt>Review reason</dt><dd>'+esc(d.review_reason || d.review_code || 'Not recorded')+'</dd><dt>Final category</dt><dd>'+esc(d.final_category || 'Not reviewed')+'</dd><dt>Association</dt><dd>'+badge(d.association)+'</dd><dt>Uploaded</dt><dd>'+esc(date(d.uploaded_at))+'</dd><dt>Reviewed by</dt><dd>'+esc(d.reviewed_by || 'Not reviewed')+'</dd></dl>'+button('Download original','download',id)+button('Extract text','extract',id)+notice('The API exposes classification state and the human-selected final category. It does not expose a verifiable model source or raw suggested category.')+(state.role==='manager'?'<form id="evidenceReviewForm"><h3>Classification review</h3><label>Review action<select name="status"><option value="select_category">Select category</option><option value="confirm_suggested">Confirm stored suggestion</option></select></label><label>Category (for select category)<select name="category">'+['lease_document','invoice','inspection_record','maintenance_photo','communication_record'].map(c=>'<option>'+c+'</option>').join('')+'</select></label><button>Save classification review</button></form>':'')+technical(d)+'<div id="extractionResult"></div></section>';
  $('pageTitle').textContent='Document detail';
}
function reviewCards(queue) {
  return queue.length?queue.map(q=>'<section class="card"><div class="card-head"><h2>Reply awaiting your decision</h2>'+badge(q.draft.status)+'</div><p class="hint">Draft '+esc(q.draft.draft_id)+' · Version '+esc(q.draft.version)+'</p><h3>Original message</h3><p>'+esc(q.original_message || 'No original message attached; inspect source facts below.')+'</p><p><strong>Review reason:</strong> '+esc(q.reason)+'</p><form class="reviewForm" data-id="'+esc(q.draft.draft_id)+'" data-version="'+esc(q.draft.version)+'"><label>Final reply<textarea name="final_content" required>'+esc(q.draft.content)+'</textarea></label><label>Decision reason<input name="reason" placeholder="Required when rejecting"></label><div class="actions"><button name="decision" value="approved">Approve exact version</button><button name="decision" value="rejected" class="danger">Reject reply</button></div></form>'+technical({draft:q.draft,source_facts:q.source_facts,property_id:q.property_id})+'</section>').join(''):empty('No pending reply drafts. Approved and rejected drafts leave this queue.');
}
async function reviews() { return notice('Shared reply approval is separate from document classification review. Approval queues a reply; delivery happens through the dispatcher.')+reviewCards(await request('/api/reviews'))+decisionReceipts(); }
function decisionReceipts() { return state.decisions.length?'<section class="card"><h2>Decisions in this session</h2>'+state.decisions.map(d=>'<p>'+badge(d.decision)+' · <code>'+esc(d.draft_id)+'</code></p>'+technical(d)).join('')+'</section>':''; }
function operations(kind='all') {
  return '<section class="card"><h2>Local operations</h2><p class="muted">Explicit scheduler actions for this demo. A separate operations credential is required.</p><div class="actions">'+(kind!=='dispatch'?button('Run rent & lease scan','scan','',false):'')+(kind!=='scan'?button('Handoff maintenance updates','handoff')+button('Dispatch queued notifications','dispatch','',false):'')+'</div><p class="hint">Dispatch delivers to the local tenant inbox, not external email or SMS. Scans never run on page load.</p>'+state.operations.map(o=>'<h3>'+esc(o.name)+'</h3>'+technical(o.result)).join('')+'</section>';
}
async function notifications() {
  const items=await request('/api/notifications/inbox');
  return '<section class="card"><div class="card-head"><h2>Delivered replies</h2><span>'+items.length+' notifications</span></div>'+notice('These records confirm delivery to this prototype’s local inbox.')+(items.length?items.slice().reverse().map(n=>'<article class="inbox-item">'+badge('delivered')+'<p>'+esc(n.content)+'</p><small class="muted">'+esc(date(n.delivered_at))+'</small>'+technical(n)+'</article>').join(''):empty('No delivered notifications yet. A pending or queued reply will not appear here.'))+'</section>'+operations('dispatch');
}
async function lease() {
  const q=await request('/api/reviews'); const reminders=q.filter(r=>['rent_reminder','lease_deadline'].includes(r.draft.source_kind));
  return notice('General lease and rent ledger APIs are not exposed. This view uses persisted reminder drafts from the authorized scheduled scan. It does not calculate balances or deadlines in the browser.')+operations('scan')+'<section class="card"><h2>Reminder drafts awaiting review</h2><p class="muted">Open Review Centre to approve or reject. Delivery remains a separate action.</p>'+button('Open Review Centre','navigate','reviews')+'</section>'+reviewCards(reminders)+operations('dispatch');
}
function reportPeriod() { const now=new Date(); return {year:now.getFullYear(),month:now.getMonth()+1}; }
async function reports() {
  const p=state.period || reportPeriod(); state.period=p;
  const history=await request('/api/manager/monthly-reports?'+propQuery()+'&year='+p.year+'&month='+p.month);
  return '<section class="card"><h2>Create a monthly property report</h2><form id="reportForm"><div class="form-row"><label>Authorized property<input value="'+esc(state.property)+'" readonly></label><label>Reporting month<input name="period" type="month" value="'+p.year+'-'+String(p.month).padStart(2,'0')+'" required></label></div><label>Generation reason<input name="reason" placeholder="Monthly property review"></label><div class="actions"><button name="action" value="generate">Generate report</button><button name="action" value="history" class="secondary">Load history</button><button name="action" value="regenerate" class="secondary">New generation</button></div></form><p class="hint">Generate reuses this session’s key for the selected month. New generation deliberately creates another report version. Data reflects the latest state at reading time.</p></section><section class="card"><h2>Report history · '+p.year+'-'+String(p.month).padStart(2,'0')+'</h2>'+(history.length?'<div class="table-wrap"><table><thead><tr><th>Generation</th><th>Outcome</th><th>Narrative</th><th>Created</th><th></th></tr></thead><tbody>'+history.map(r=>'<tr><td>'+r.generation+'</td><td>'+badge(r.outcome)+'</td><td>'+badge(r.narrative.source)+'</td><td>'+esc(date(r.created_at))+'</td><td>'+button('Open report','report',r.report_id)+'</td></tr>').join('')+'</tbody></table></div>':empty('No stored reports for this month.'))+'</section>';
}
function structured(value) {
  if(value===null || value===undefined) return '<span class="muted">Not available</span>';
  if(Array.isArray(value)) return value.length?value.map(v=>'<div class="fact">'+structured(v)+'</div>').join(''):'<p class="muted">No records</p>';
  if(typeof value==='object') return '<dl>'+Object.entries(value).map(([k,v])=>'<dt>'+esc(k.replaceAll('_',' '))+'</dt><dd>'+structured(v)+'</dd>').join('')+'</dl>';
  return esc(String(value));
}
async function reportDetail(id) {
  const r=await request('/api/manager/monthly-reports/'+enc(id)+'?'+propQuery()); state.selectedReport=r;
  $('content').innerHTML=button('← Report history','navigate','reports')+'<section class="card"><div class="card-head"><h2>Property report · '+r.period.year+'-'+String(r.period.month).padStart(2,'0')+'</h2>'+badge(r.outcome)+'</div><p class="muted">Generation '+r.generation+' · Data read '+esc(date(r.data_as_of))+'</p><div class="report-sections">'+['maintenance','rent','lease','open_tasks','risks'].map(k=>'<article class="report-section"><h3>'+esc(k.replaceAll('_',' ').toUpperCase())+'</h3>'+structured(r[k])+'</article>').join('')+'</div></section><section class="card"><h2>Narrative</h2>'+badge(r.narrative.source)+'<p>'+esc(r.narrative.text || 'No narrative was generated. The structured report is preserved above.')+'</p><p class="hint">Source is the persisted backend narrative.source value. Structured data above is collected independently.</p>'+technical(r)+'</section>';
  $('pageTitle').textContent='Monthly report';
}
async function activity() {
  const items=await request('/api/maintenance/requests');
  return '<section class="card"><h2>Follow a maintenance workflow</h2><p class="muted">Select a request to inspect its persisted Maintenance events and Evidence handoff timeline. No global event feed is available.</p><form id="timelineForm"><label>Maintenance request<select name="request_id" required>'+items.map(v=>'<option value="'+esc(v.request_id)+'">'+esc(v.title)+' · '+esc(v.status)+'</option>').join('')+'</select></label><button '+(!items.length?'disabled':'')+'>Open timeline</button></form></section><div id="timelineContent"></div>';
}
async function timeline(id) {
  const v=await request('/api/maintenance/requests/'+enc(id)); state.selectedRequest=v;
  const result=await Promise.allSettled([request('/api/evidence/maintenance-timeline?request_id='+enc(id)+'&'+propQuery())]);
  const evidence=result[0].status==='fulfilled'?'<h3>Evidence · persisted handoff records</h3>'+structured(result[0].value)+technical(result[0].value):notice('Evidence timeline unavailable: '+errorMessage(result[0].reason));
  const html='<section class="card"><h2>'+esc(v.title)+'</h2><p><code>'+esc(id)+'</code></p><h3>Maintenance · request event history</h3>'+events(v.events)+evidence+technical({request_id:v.request_id,message_id:v.message_id})+'</section>';
  if($('timelineContent')) $('timelineContent').innerHTML=html; else $('content').innerHTML=html;
}
async function guided() {
  const results=await Promise.allSettled([request('/api/maintenance/requests'),request('/api/evidence/documents?'+propQuery()),state.role==='manager'?request('/api/reviews'):request('/api/notifications/inbox')]);
  const values=results.map(r=>r.status==='fulfilled'?r.value:[]); const selected=state.selectedRequest && values[0].find(v=>v.request_id===state.selectedRequest.request_id); const doc=state.selectedDocument && values[1].find(d=>d.document_id===state.selectedDocument.document_id);
  const step=(title,description,done=false)=>'<li class="'+(done?'done':'')+'"><div>'+esc(title)+'<small>'+esc(description)+'</small><span class="badge '+(done?'good':'')+'">'+(done?'Completed':'Pending')+'</span></div></li>';
  return notice('Steps are marked complete only for a selected, persisted record. General counts do not prove a relationship between records. Switch sessions as the workflow changes role.')+'<div class="grid"><section class="card"><span class="eyebrow">WORKFLOW A</span><h2>Maintenance, from issue to resolution</h2><ol class="steps">'+step('Tenant registers a repair',selected?'Selected request: '+selected.request_id:'Submit a repair, then open that exact request.',!!selected)+step('Manager assesses and updates',selected?'Current backend status: '+selected.status:'Use the exact request version in Maintenance.',!!selected?.events?.some(e=>e.actor.startsWith('manager:')))+step('Evidence accompanies the request',selected?.events?.flatMap(e=>e.evidence_refs||[]).join(', ') || 'Upload a document and pass its ID when recording a transition.',!!selected?.events?.some(e=>e.evidence_refs?.length))+step('Handoff, review and delivery','Use Local operations; inspect actual tenant inbox event IDs.')+step('Inspect the timeline','Open the selected request’s Maintenance and Evidence history.')+'</ol>'+button('Open Maintenance','navigate','maintenance')+button('Operations','navigate',state.role==='manager'?'lease':'notifications')+'</section><section class="card"><span class="eyebrow">WORKFLOW B</span><h2>Lease & rent reminders</h2><ol class="steps">'+step('Authorized operations scan','Run the scan explicitly with a scheduler credential.')+step('Manager reviews a reminder','Inspect its actual source_facts and exact draft version.')+step('Dispatch approved content','Operations returns delivered / retry / failed counts.')+step('Tenant reads the reply','Only a persisted inbox record confirms local delivery.')+'</ol>'+button(state.role==='manager'?'Open Lease & Rent':'Open Notifications','navigate',state.role==='manager'?'lease':'notifications')+'</section><section class="card"><span class="eyebrow">WORKFLOW C</span><h2>Documents & evidence</h2><ol class="steps">'+step('Upload the original',doc?'Selected document: '+doc.document_id:'Upload a file and open its stored record.',!!doc)+step('Inspect machine classification',doc?'Actual classification state: '+doc.classification:'Failure leaves the original file saved.',!!doc && ['classified','failed'].includes(doc.classification))+step('Manager settles the category',doc?.final_category || 'Confirm a valid suggestion or select a business category.',!!doc?.final_category)+step('Inspect references and timeline','Associate an existing document ID through a maintenance transition.')+'</ol>'+button('Open Documents','navigate','documents')+'</section><section class="card"><span class="eyebrow">WORKFLOW D</span><h2>Monthly property reporting</h2><ol class="steps">'+step('Collect real source records','Maintenance, lease and rent are read by the backend.')+step('Generate a month',state.selectedReport?'Opened report: '+state.selectedReport.report_id:'Manager generates a report for the session property.',!!state.selectedReport)+step('Inspect structure and narrative separately',state.selectedReport?'Outcome: '+state.selectedReport.outcome:'Missing narrative does not erase structured data.',!!state.selectedReport)+step('Reopen stored history','History reads do not regenerate a report.')+'</ol>'+(state.role==='manager'?button('Open Reports','navigate','reports'):notice('Switch to a manager session to generate reports.'))+'</section></div>'+results.filter(r=>r.status==='rejected').map(r=>notice(errorMessage(r.reason))).join('');
}

function openSession() {
  const f=$('sessionForm'); f.elements.role.value=state.role; f.elements.property.value=state.property; f.elements.token.value=state.token; f.elements.scheduler.value=state.scheduler; $('sessionDialog').showModal();
}
const modelResult=(message,error=false)=>$('modelSettingsResult').innerHTML='<div class="notice '+(error?'error':'')+'">'+esc(message)+'</div>';
function showModelStatus(result) {
  const f=$('modelSettingsForm'); f.elements.base_url.value=result.base_url; f.elements.model.value=result.model;
  modelResult('Configuration '+(result.configured?'ready':'incomplete')+' · API key '+(result.api_key_configured?'configured':'not configured')+' · Source: '+result.source+'. Live inference has not been tested by this status read.');
}
async function modelAction(control, action) {
  const f=$('modelSettingsForm'); if(f.dataset.busy==='true') return;
  const controls=Array.from(f.querySelectorAll('button')); controls.forEach(b=>b.disabled=true); f.dataset.busy='true';
  try {
    const token=f.elements.operator_token.value.trim();
    if(action==='load') showModelStatus(await api('/api/ops/llm/settings',{token}));
    if(action==='save') {
      const result=await api('/api/ops/llm/settings',{token,method:'PUT',body:{base_url:f.elements.base_url.value.trim(),model:f.elements.model.value.trim(),api_key:f.elements.api_key.value || null}});
      showModelStatus(result); modelResult('Settings applied to this server process. Connection not tested. Use Test model connection to make a real API call.');
    }
    if(action==='test') {
      modelResult('Calling the configured model. This may take up to the server timeout…');
      const result=await api('/api/ops/llm/test',{token,method:'POST'});
      modelResult('Real model returned a validated response. Source: '+result.source+' · Classification: '+result.category+'. This verifies the connection, not every domain workflow.');
    }
    if(action==='restore') {
      if(!confirm('Restore the running server’s startup environment settings?')) return;
      showModelStatus(await api('/api/ops/llm/settings',{token,method:'DELETE'}));
    }
  } catch(error) { modelResult(errorMessage(error),true); }
  finally { f.elements.api_key.value=''; f.dataset.busy='false'; controls.forEach(b=>b.disabled=false); }
}
$('modelSettingsButton').onclick=()=>{
  const f=$('modelSettingsForm'); f.elements.operator_token.value=state.scheduler; f.elements.api_key.value='';
  modelResult('Enter the local operations token and load current settings, or enter your API address, key and model.'); $('modelSettingsDialog').showModal();
};
$('loadModelSettings').onclick=e=>modelAction(e.currentTarget,'load');
$('testModelSettings').onclick=e=>modelAction(e.currentTarget,'test');
$('restoreModelSettings').onclick=e=>modelAction(e.currentTarget,'restore');
$('closeModelSettings').onclick=()=>{ $('modelSettingsForm').reset(); $('modelSettingsDialog').close(); };
$('modelSettingsDialog').addEventListener('close',()=>{ $('modelSettingsForm').elements.api_key.value=''; $('modelSettingsForm').elements.operator_token.value=''; });
$('modelSettingsForm').onsubmit=e=>{ e.preventDefault(); modelAction(e.submitter,'save'); };
$('sessionButton').onclick=openSession; $('cancelSession').onclick=()=>$('sessionDialog').close(); $('refreshButton').onclick=()=>render();
$('sessionForm').onsubmit=e=>{
  e.preventDefault(); const f=e.currentTarget; const changed=state.token!==f.elements.token.value.trim() || state.property!==f.elements.property.value.trim() || state.role!==f.elements.role.value;
  if(changed) { state.receipts=[]; state.decisions=[]; state.selectedRequest=null; state.selectedDocument=null; state.selectedReport=null; state.generationKeys={}; state.operations=[]; }
  state.role=f.elements.role.value; state.property=f.elements.property.value.trim(); state.token=f.elements.token.value.trim(); state.scheduler=f.elements.scheduler.value.trim(); $('sessionDialog').close(); render('home');
};
document.addEventListener('click', async e=>{
  const navButton=e.target.closest('[data-page]'); if(navButton) { render(navButton.dataset.page); return; }
  const b=e.target.closest('[data-action]'); if(!b) return; const {action,id}=b.dataset;
  if(action==='session') return openSession();
  if(action==='navigate') return render(id);
  await busy(b,async()=>{
    if(action==='request') await requestDetail(id);
    if(action==='document') await documentDetail(id);
    if(action==='report') await reportDetail(id);
    if(action==='timeline') await timeline(id);
    if(action==='extract') { const data=await request('/api/evidence/documents/'+enc(id)+'/text?'+propQuery()); $('extractionResult').innerHTML='<h3>Extraction result</h3>'+structured(data); }
    if(action==='download') {
      const blob=await request('/api/evidence/documents/'+enc(id)+'/download?'+propQuery(),{binary:true}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=id+'.'+state.selectedDocument.file_type; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    if(['scan','handoff','dispatch'].includes(action)) {
      if(!confirm('Run this local operations action using the separate scheduler credential?')) return;
      const paths={scan:'/api/ops/rent-alerts/run',handoff:'/api/ops/communication/maintenance-events/run',dispatch:'/api/ops/notifications/dispatch'};
      const data=await ops(paths[action]); state.operations.unshift({name:action,result:data}); state.operations=state.operations.slice(0,5); await render(); flash(action+' completed. Actual result: '+JSON.stringify(data));
    }
  });
});
document.addEventListener('submit',async e=>{
  const f=e.target; if(f.id==='sessionForm') return;
  if(!['messageForm','maintenanceForm','uploadForm','statusForm','assessmentForm','evidenceReviewForm','reportForm','timelineForm'].includes(f.id) && !f.classList.contains('reviewForm')) return;
  e.preventDefault(); const submitter=e.submitter || f.querySelector('button'); if(f.dataset.busy==='true') return;
  const data=Object.fromEntries(new FormData(f)); const selectedDecision=e.submitter?.value;
  f.dataset.busy='true'; const controls=Array.from(f.querySelectorAll('button')); controls.forEach(b=>b.disabled=true);
  try {
    if(f.id==='messageForm') {
      const r=await request('/api/communication/messages',{method:'POST',body:{content:data.content}}); state.receipts.unshift({...r,content:data.content}); f.reset(); await render('messages'); flash('Message recorded. Reply: '+r.reply_status+' · Delivery: '+r.delivery_status+'.');
    }
    if(f.id==='maintenanceForm') {
      f.dataset.key ||= crypto.randomUUID(); const r=await request('/api/maintenance/requests',{method:'POST',headers:{'Idempotency-Key':f.dataset.key},body:{title:data.title,description:data.description,location:data.location || null,evidence_refs:refs(data.evidence_refs)}});
      if(r.status!=='registered') throw new Error('Request was not registered: '+JSON.stringify(r)); f.reset(); await requestDetail(r.request_id); flash('Maintenance request registered: '+r.request_id);
    }
    if(f.id==='uploadForm') {
      const form=new FormData(); form.append('file',f.elements.file.files[0]); form.append('property_id',state.property); const r=await request('/api/evidence/documents',{method:'POST',body:form});
      if(r.status!=='accepted') throw new Error('Upload rejected: '+JSON.stringify(r)); f.reset(); await documentDetail(r.document.document_id); flash('Original saved. Classification state: '+r.document.classification+'.');
    }
    if(f.id==='statusForm' || f.id==='assessmentForm') {
      if(!confirm('Record this manager action on version '+state.selectedRequest.version+'?')) return;
      const v=state.selectedRequest; const assessment=f.id==='assessmentForm'; const body=assessment?{priority:data.priority,rationale:data.rationale,expected_version:v.version}:{target_status:data.target_status,reason:data.reason,evidence_refs:refs(data.evidence_refs),expected_version:v.version};
      const r=await request('/api/maintenance/requests/'+enc(v.request_id)+(assessment?'/assessment':'/status'),{method:'POST',body}); await requestDetail(v.request_id); flash('Manager action recorded: '+(r.persisted_status || r.status));
    }
    if(f.id==='evidenceReviewForm') {
      if(!confirm('Save this document classification decision?')) return;
      const body={status:data.status,document_id:state.selectedDocument.document_id,property_id:state.property}; if(data.status==='select_category') body.category=data.category;
      const r=await request('/api/evidence/reviews',{method:'POST',body}); await documentDetail(r.document_id); flash('Classification reviewed: '+r.final_category);
    }
    if(f.classList.contains('reviewForm')) {
      if(selectedDecision==='rejected' && !data.reason.trim()) throw new Error('Enter a reason to reject this reply.');
      if(!confirm((selectedDecision==='approved'?'Approve':'Reject')+' this exact draft version?')) return;
      const r=await request('/api/reviews/'+enc(f.dataset.id)+'/decision',{method:'POST',body:{expected_version:Number(f.dataset.version),decision:selectedDecision,final_content:selectedDecision==='approved'?data.final_content:null,reason:data.reason || null}}); state.decisions.unshift(r); await render(); flash('Decision recorded: '+selectedDecision+'. Approval queues a reply; delivery is separate.');
    }
    if(f.id==='reportForm') {
      const [year,month]=data.period.split('-').map(Number); state.period={year,month};
      if(selectedDecision==='history') { await render('reports'); return; }
      const key=state.property+':'+data.period;
      if(selectedDecision==='regenerate') { if(!confirm('Create a new report generation for this month?')) return; delete state.generationKeys[key]; }
      state.generationKeys[key] ||= crypto.randomUUID();
      const r=await request('/api/manager/monthly-reports/generate',{method:'POST',body:{property_id:state.property,year,month,idempotency_key:state.generationKeys[key],reason:data.reason || null}});
      const record=r.report || r; await reportDetail(record.report_id); flash(r.reused?'Existing generation reused.':'Report generation saved.');
    }
    if(f.id==='timelineForm') await timeline(data.request_id);
  } catch(error) { flash(errorMessage(error),true); }
  finally { f.dataset.busy='false'; controls.forEach(b=>b.disabled=false); }
});
render();
