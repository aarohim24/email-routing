import {createNestablePublicClientApplication, InteractionRequiredAuthError} from '@azure/msal-browser';
import {actionPayload, currentMessageId, Selection, labels} from './model.mjs';

const $ = id => document.getElementById(id);
const selection = new Selection();
let config, msal, token = '', ticket = null, routing, busy = false, queueBefore = null, historyBefore = null;
let queueState = 'AWAITING_APPROVAL';
let profile = null, currentView = 'current', lastRevision = null, polling = false, pollTimer = null;
const message = (text, error = false) => { $('notice').textContent = text; $('notice').className = error ? 'notice error' : 'notice'; };
function element(tag, text, parent, className = '') {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  if (parent) parent.append(node);
  return node;
}
function button(text, handler, parent, secondary = false) {
  const node = element('button', text, parent, secondary ? 'secondary' : '');
  node.type = 'button'; node.disabled = busy; node.addEventListener('click', handler); return node;
}
async function accessToken(interactive = false) {
  const loginHint = Office.context.mailbox.userProfile.emailAddress;
  const account = msal.getAllAccounts().find(a => a.username.toLowerCase() === loginHint.toLowerCase());
  const request = {scopes: [config.scope], loginHint, ...(account ? {account} : {})};
  const result = interactive ? await msal.acquireTokenPopup(request) : await msal.acquireTokenSilent(request);
  return result.accessToken;
}
async function api(path, options = {}) {
  try { token = await accessToken(); }
  catch (error) { $('signin').hidden = false; throw new Error('Microsoft sign-in needs attention. Select Sign in with Microsoft to continue.'); }
  const response = await fetch(path, {...options, credentials: 'omit', cache: 'no-store',
    headers: {'Authorization': `Bearer ${token}`, ...(options.body ? {'Content-Type': 'application/json'} : {})}});
  const data = await response.json();
  if (response.status === 401) $('signin').hidden = false;
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function showView(view) {
  currentView = view;
  for (const name of ['current', 'queue', 'settings', 'metrics']) $(name).hidden = name !== view;
  for (const node of document.querySelectorAll('nav button')) node.setAttribute('aria-pressed', String(node.dataset.view === view));
}
function clearTicket() {
  ticket = null; $('ticket').replaceChildren(); $('history').replaceChildren(); $('historyMore').hidden = true; historyBefore = null;
}
async function selectCurrent() {
  const generation = selection.next(); clearTicket(); showView('current');
  const mid = currentMessageId(Office);
  if (!mid) { message('Select one received email to view its concern. Pending Approvals is available without a selected email.'); return; }
  message('Finding the concern for this email…');
  try {
    const data = await api('/api/context?' + new URLSearchParams({message_id: mid}));
    if (!selection.isCurrent(generation)) return;
    ticket = data; renderTicket(); message(''); await loadHistory(generation, true);
  } catch (error) { if (selection.isCurrent(generation)) message(error.message, true); }
}
async function openTicket(id) {
  const generation = selection.next(); clearTicket(); showView('current'); message('Loading ticket…');
  try {
    const data = await api(`/api/concerns/${id}`);
    if (!selection.isCurrent(generation)) return;
    ticket = data; renderTicket(); message(''); await loadHistory(generation, true);
  } catch (error) { if (selection.isCurrent(generation)) message(error.message, true); }
}
function renderTicket() {
  const root = $('ticket'); root.replaceChildren();
  if (!ticket) return;
  element('div', `TICKET #${ticket.id}`, root, 'eyebrow');
  element('h2', ticket.subject || '(No subject)', root);
  element('span', ticket.status.replaceAll('_', ' '), root, 'badge');
  const dl = element('dl', undefined, root);
  for (const [name, value] of [['Detected concern', ticket.category.replaceAll('_', ' ')], ['Proposed authority', ticket.authority?.name || 'Unassigned'], ['Authority email', ticket.authority?.email || 'Not configured — forwarding blocked'], ['From', ticket.sender], ['Received', new Date(ticket.received_at).toLocaleString()], ['Alumnus name', ticket.alumnus_name || 'Not provided'], ['Enrolment number', ticket.enrolment_number || 'Not provided'], ['Alumni number', ticket.alumni_number || 'Not provided'], ['Requested document', ticket.requested_document || 'Needs review'], ['Department', ticket.authority?.department || 'Not institutionally verified'], ['Authority due', ticket.due_at ? new Date(ticket.due_at).toLocaleString() : ticket.status === 'FORWARDED' ? 'Missing — Admin must set a due date' : 'Set after approved forwarding'], ['Classifier', `Rule-based baseline · ${ticket.classifier_version || 'rules-v1'}`]]) {
    element('dt', name, dl); element('dd', value, dl);
  }
  const details = element('details', undefined, root); element('summary', 'Original email context', details); element('pre', ticket.body, details);
  if (ticket.actions.length) {
    const group = element('div', undefined, root, 'actions');
    for (const command of ticket.actions) button(labels[command], () => showAction(command), group, command !== 'approve');
  } else element('p', ticket.deliveries.some(j => ['PENDING', 'RUNNING', 'UNCERTAIN'].includes(j.state)) ? 'Delivery pending or uncertain. Refresh for updates; reconcile uncertain sends before changing this ticket.' : 'No actions are available in this state.', root, 'muted');
  element('div', undefined, root).id = 'actionForm';
  if (ticket.reminder_blocked) element('p', ticket.reminder_blocked, root, 'notice error');
  if (ticket.escalation_blocked_reason) element('p', ticket.escalation_blocked_reason, root, 'notice error');
  if (profile?.is_admin) {
    if (['AWAITING_APPROVAL','NEEDS_REVIEW','FORWARDED'].includes(ticket.status)) button('Set due date', showDueDate, root, true);
    button('Label pilot outcome', showPilotLabel, root, true);
  }
  if (ticket.deliveries.length) {
    element('h3', 'Delivery tracking', root);
    for (const job of ticket.deliveries) {
      const box = element('div', undefined, root, 'delivery');
      element('strong', `${job.kind} · ${job.state}${job.dry_run ? ' · SIMULATED' : ''}`, box);
      element('p', job.recipient, box);
      if (job.error) element('p', job.error, box, 'error');
      if (job.draft_id) element('pre', `Draft: ${job.draft_id}`, box);
      if (profile?.is_admin && job.state === 'UNCERTAIN') button('Reconcile delivery', () => showReconciliation(job), box, true);
    }
  }
}
function reasonField(parent) {
  const label = element('label', 'Reason / evidence', parent); label.htmlFor = 'reason';
  const input = element('textarea', undefined, parent); input.id = 'reason'; input.maxLength = 3000; input.required = true; return input;
}
function showAction(command) {
  if (busy || !ticket) return;
  const snapshot = ticket, generation = selection.generation;
  const root = $('actionForm'); root.replaceChildren(); element('h3', labels[command], root);
  if (command === 'approve') element('p', `Forward ticket #${snapshot.id} to ${snapshot.authority?.name} (${snapshot.authority?.email})? ${config.dryRun ? 'This is a simulation.' : 'This will queue a real email.'}`, root);
  if (command === 'addressed') element('p', 'Record that the authority addressed the concern, then queue a resolution follow-up to the original sender.', root);
  let category;
  if (command === 'route') {
    const label = element('label', 'Concern category', root); label.htmlFor = 'category';
    category = element('select', undefined, root); category.id = 'category';
    for (const value of routing.categories) { const option = element('option', value.replaceAll('_', ' '), category); option.value = value; }
    if (routing.categories.includes(snapshot.category)) category.value = snapshot.category;
  }
  let closure;
  if (command === 'close') {
    element('label', 'Closure outcome', root).htmlFor = 'closureOutcome';
    closure = element('select', undefined, root); closure.id = 'closureOutcome';
    for (const [value, text] of [['administrative','Administrative closure (duplicate / withdrawn / out of scope)'],['resolved','Verified fulfilled (record evidence below)']]) { const option = element('option', text, closure); option.value = value; }
  }
  const reason = reasonField(root);
  button('Confirm ' + labels[command].toLowerCase(), async () => {
    if (busy || !selection.isCurrent(generation)) return;
    try {
      const payload = actionPayload(snapshot, command, reason.value, category?.value);
      if (command === 'close') payload.closure_outcome = closure.value;
      await mutate(snapshot.id, payload, generation);
    } catch (error) { message(error.message, true); }
  }, root);
  button('Cancel', () => root.replaceChildren(), root, true);
}
function showReconciliation(job) {
  if (busy) return;
  const id = ticket.id, generation = selection.generation, root = $('actionForm'); root.replaceChildren();
  element('h3', 'Verify delivery before retrying', root);
  element('p', 'Inspect Microsoft 365 Sent Items and the saved draft. Record the evidence. Never choose an outcome based on a guess.', root);
  const reason = reasonField(root);
  for (const [text, sent] of [['Verified sent', true], ['Verified not sent', false]]) button(text, async () => {
    if (!reason.value.trim()) { message('Evidence is required.', true); return; }
    await mutate(id, {action: 'reconcile', job_id: job.id, sent, reason: reason.value}, generation);
  }, root, true);
}
async function mutate(id, payload, generation, operation = 'actions') {
  if (busy || !selection.isCurrent(generation)) return;
  busy = true; document.querySelectorAll('button').forEach(n => n.disabled = true);
  message('Recording decision…');
  try {
    const data = await api(`/api/concerns/${id}/${operation}`, {method: 'POST', body: JSON.stringify(payload)});
    if (selection.isCurrent(generation)) { ticket = data; renderTicket(); message('Decision recorded. Refresh to see background delivery progress.'); await loadHistory(generation, true); }
  } catch (error) { if (selection.isCurrent(generation)) message(error.message + ' Refresh before retrying.', true); }
  finally { busy = false; document.querySelectorAll('button').forEach(n => n.disabled = false); }
}
async function loadHistory(generation = selection.generation, reset = false) {
  if (!ticket) return;
  const id = ticket.id;
  try {
    const data = await api(`/api/concerns/${id}/history` + (!reset && historyBefore ? `?before=${historyBefore}` : ''));
    if (!selection.isCurrent(generation) || ticket?.id !== id) return;
    if (reset) $('history').replaceChildren();
    for (const event of data.items) {
      const box = element('li', undefined, $('history'));
      element('strong', event.event.replaceAll('_', ' '), box);
      element('p', `${new Date(event.created_at).toLocaleString()} · ${event.actor}`, box, 'muted');
      element('pre', JSON.stringify(event.details, null, 2), box);
    }
    historyBefore = data.next_before; $('historyMore').hidden = !historyBefore;
  } catch (error) { if (selection.isCurrent(generation)) message(error.message, true); }
}
async function loadQueue(reset = true) {
  const generation = selection.next(); clearTicket(); showView('queue');
  if (reset) { queueBefore = null; $('queueItems').replaceChildren(); }
  message('Loading review queue…');
  try {
    const data = await api('/api/concerns?' + new URLSearchParams({status: queueState, ...(queueBefore ? {before: queueBefore} : {})}));
    if (!selection.isCurrent(generation)) return;
    for (const item of data.items) {
      const box = element('li', undefined, $('queueItems'));
      button(`#${item.id} · ${item.subject || '(No subject)'}`, () => openTicket(item.id), box, true);
      element('p', `${item.category.replaceAll('_', ' ')} · ${item.authority?.name || 'Unassigned'} · ${item.sender}`, box, 'muted');
      if (!item.actions.length) element('p', 'Delivery pending or awaiting reconciliation', box, 'muted');
    }
    queueBefore = data.next_before; $('queueMore').hidden = !queueBefore;
    message(data.items.length ? '' : 'No concerns in this queue.');
  } catch (error) { if (selection.isCurrent(generation)) message(error.message, true); }
}
async function loadSettings() {
  const generation = selection.next(); clearTicket(); showView('settings'); $('authorityForms').replaceChildren();
  try {
    const [routes, health] = await Promise.all([api('/api/routing'), api('/api/health')]);
    if (!selection.isCurrent(generation)) return;
    routing = routes;
    for (const authority of routes.authorities) {
      const box = element('section', undefined, $('authorityForms'));
      element('h3', authority.name, box);
      element('p', routes.routes.filter(r => r.authority_id === authority.id).map(r => r.category.replaceAll('_', ' ')).join(' · '), box, 'muted');
      const label = element('label', 'Verified authority email (blank disables routing)', box); label.htmlFor = 'email-' + authority.id;
      const input = element('input', undefined, box); input.type = 'email'; input.id = label.htmlFor; input.value = authority.email || '';
      const departmentLabel = element('label', 'Verified department (optional)', box); departmentLabel.htmlFor = 'dept-' + authority.id;
      const department = element('input', undefined, box); department.id = departmentLabel.htmlFor; department.value = authority.department || ''; department.maxLength = 150;
      const identityLabel = element('label', 'Authority Entra user object IDs (comma-separated, verified by IT)', box); identityLabel.htmlFor = 'users-' + authority.id;
      const users = element('textarea', undefined, box); users.id = identityLabel.htmlFor; users.value = (routes.authority_users || []).filter(u => u.authority_id === authority.id).map(u => u.object_id).join(', ');
      button('Save authority', async () => {
        if (busy || !input.reportValidity()) return;
        busy = true;
        try { routing = await api('/api/routing', {method:'POST', body:JSON.stringify({authority_id:authority.id,email:input.value,department:department.value,object_ids:users.value.split(',').map(v => v.trim()).filter(Boolean)})}); message('Authority address saved and audited. Review affected pending concerns again.'); }
        catch (error) { message(error.message, true); }
        finally { busy = false; }
      }, box);
    }
    renderPolicy(routes.reminder_policy);
    $('health').textContent = JSON.stringify(health, null, 2); message('');
  } catch (error) { if (selection.isCurrent(generation)) message(error.message, true); }
}
function showDueDate() {
  if (busy || !ticket || !profile?.is_admin) return;
  const snapshot=ticket,generation=selection.generation,root=$('actionForm');root.replaceChildren();
  element('h3','Authority follow-up due date',root);
  element('label','Due date and time (your local time)',root).htmlFor='dueDate';
  const input=element('input',undefined,root);input.type='datetime-local';input.id='dueDate';
  if(snapshot.due_at){const date=new Date(snapshot.due_at);input.value=new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);}
  button('Save due date',async()=>{
    try {if(!input.value)throw new Error('Choose a due date.');await mutate(snapshot.id,{version:snapshot.version,due_at:new Date(input.value).toISOString()},generation,'due');}
    catch(error){message(error.message,true);}
  },root);
  button('Restore default due date',()=>mutate(snapshot.id,{version:snapshot.version,due_at:null},generation,'due'),root,true);
}
function showPilotLabel() {
  if(busy||!ticket||!profile?.is_admin)return;
  const snapshot=ticket,generation=selection.generation,root=$('actionForm');root.replaceChildren();
  element('h3','Independent pilot label',root);
  element('p','Label the correct document and authority from the original request and verified routing matrix. This does not reroute or forward the ticket.',root,'muted');
  element('label','Correct category',root).htmlFor='pilotCategory';const category=element('select',undefined,root);category.id='pilotCategory';
  for(const value of [...routing.categories,'UNKNOWN']){const option=element('option',value.replaceAll('_',' '),category);option.value=value;}
  category.value=snapshot.pilot_label?.category||snapshot.category;
  element('label','Correct authority (ignored for UNKNOWN)',root).htmlFor='pilotAuthority';const authority=element('select',undefined,root);authority.id='pilotAuthority';
  const unset=element('option','Choose verified authority',authority);unset.value='';
  for(const item of routing.authorities){const option=element('option',item.name,authority);option.value=item.id;}
  authority.value=snapshot.pilot_label?.authority_id||'';
  const reason=reasonField(root);
  button('Save reviewed label',()=>{
    if(!reason.value.trim()||(category.value!=='UNKNOWN'&&!authority.value)){message('Choose the verified authority and record your rationale.',true);return;}
    mutate(snapshot.id,{version:snapshot.version,category:category.value,authority_id:category.value==='UNKNOWN'?null:authority.value,reason:reason.value},generation,'label');
  },root);
}
function renderPolicy(policy) {
  const root=$('reminderPolicy');root.replaceChildren();if(!policy)return;
  const inputs={};
  for(const [key,text] of [['due_hours','Default authority due (hours after forwarding)'],['interval_hours','Reminder interval (hours)'],['max_reminders','Maximum reminders per forwarding cycle'],['escalation_delay_hours','Escalate this many hours after the due date']]){
    element('label',text,root).htmlFor='policy-'+key;const input=element('input',undefined,root);input.id='policy-'+key;input.type='number';input.min=key==='max_reminders'||key==='escalation_delay_hours'?'0':'1';input.max=key==='max_reminders'?'20':'8760';input.value=String(policy[key]);inputs[key]=input;
  }
  element('label','Verified escalation email (blank disables email escalation)',root).htmlFor='policy-email';const email=element('input',undefined,root);email.type='email';email.id='policy-email';email.value=policy.escalation_email||'';
  element('label','Automatic reminders',root).htmlFor='policy-enabled';const enabled=element('select',undefined,root);enabled.id='policy-enabled';
  for(const [value,text]of [['true','Enabled'],['false','Disabled']]){const option=element('option',text,enabled);option.value=value;}enabled.value=String(Boolean(policy.enabled));
  button('Save reminder policy',async()=>{
    if(busy||!email.reportValidity())return;busy=true;
    try {const payload={enabled:enabled.value==='true',escalation_email:email.value||null};for(const [key,input]of Object.entries(inputs))payload[key]=Number(input.value);await api('/api/reminder-policy',{method:'POST',body:JSON.stringify(payload)});message('Reminder policy saved and audited. Existing due dates stay unchanged.');}
    catch(error){message(error.message,true);}finally{busy=false;}
  },root);
}
async function loadMetrics() {
  if(!profile?.is_admin)return;
  const generation=selection.next();clearTicket();showView('metrics');message('Loading pilot measures…');
  try {
    const query=new URLSearchParams({mode:$('metricMode').value||'live'});
    if($('metricStart').value)query.set('from',new Date($('metricStart').value).toISOString());
    if($('metricEnd').value)query.set('to',new Date($('metricEnd').value).toISOString());
    const data=await api('/api/metrics?'+query);if(!selection.isCurrent(generation))return;
    const root=$('metricCards');root.replaceChildren();
    for(const [key,text,unit]of [['average_turnaround_hours','Average verified turnaround','hours'],['median_turnaround_hours','Median verified turnaround','hours'],['turnaround_sample_size','Verified turnaround sample','cases'],['routing_accuracy_percent','Routing accuracy','%'],['routing_evaluated_concerns','Labelled and routed sample','cases'],['classification_accuracy_percent','Baseline classification accuracy','%'],['pending_concerns','Pending concerns',''],['overdue_authority_concerns','Overdue authority concerns',''],['closure_rate_percent','Closure rate','%'],['reopen_rate_percent','Reopen rate','%'],['unresolved_rate_percent','Reported unresolved rate','%'],['reminders_accepted','Reminders accepted / simulated',''],['escalations_accepted','Escalations accepted / simulated',''],['failed_notifications','Failed notifications',''],['uncertain_notifications','Uncertain notifications','']]){
      const card=element('div',undefined,root,'metric-card');element('span',text,card,'muted');element('strong',data[key]===null?'Not yet measurable':`${data[key]} ${unit}`,card);
    }
    $('metricDefinition').textContent=`${data.total_concerns} cases · ${data.labelled_concerns} reviewed labels · ${data.definition}`;message('');
  }catch(error){if(selection.isCurrent(generation))message(error.message,true);}
}
async function pollChanges() {
  if(!token||busy||polling||document.hidden)return;polling=true;
  try {
    const result=await api('/api/changes');$('liveStatus').textContent='Status synchronized '+new Date().toLocaleTimeString();
    if(lastRevision!==null&&result.revision!==lastRevision){
      const root=$('actionForm');
      if(currentView==='current'&&ticket){
        const id=ticket.id,generation=selection.generation;
        const updated=await api(`/api/concerns/${id}`);
        if(!selection.isCurrent(generation)||ticket?.id!==id)return;
        const changed=updated.revision!==ticket.revision||updated.status!==ticket.status||updated.version!==ticket.version||JSON.stringify(updated.deliveries)!==JSON.stringify(ticket.deliveries);
        if(changed){
          if(root?.children.length)message('This ticket has changed. Refresh before confirming your decision.');
          else {ticket=updated;renderTicket();await loadHistory(generation,true);}
        }
      }else if(currentView==='queue')await loadQueue(true);
      else if(currentView==='metrics')await loadMetrics();
    }
    lastRevision=result.revision;
  }catch(error){$('liveStatus').textContent='Live updates paused: '+error.message;}
  finally{polling=false;}
}
async function start(interactive = false) {
  try {
    token = await accessToken(interactive);
    profile = await api('/api/me');
    routing = profile.is_admin ? await api('/api/routing') : {categories:[],authorities:[],routes:[]};
    $('settingsTab').hidden = !profile.is_admin; $('metricsTab').hidden = !profile.is_admin;
    $('queueTab').textContent = profile.is_admin ? 'Pending Approvals' : 'Assigned to me';
    queueState = profile.is_admin ? 'AWAITING_APPROVAL' : 'FORWARDED';
    const states = profile.is_admin ? [['AWAITING_APPROVAL','Pending approvals'],['NEEDS_REVIEW','Needs review'],['FAILED','Failed deliveries / notifications'],['FORWARDED','Awaiting authority'],['ALL','All concerns']] : [['FORWARDED','Awaiting my completion'],['ADDRESSED','Addressed'],['AWAITING_CONFIRMATION','Awaiting student confirmation'],['CLOSED','Closed'],['ALL','All assigned concerns']];
    $('queueFilter').replaceChildren();
    for (const [value,text] of states) { const option = element('option', text, $('queueFilter')); option.value = value; }
    $('queueFilter').value = queueState;
    $('signin').hidden = true; $('workspace').hidden = false;
    await selectCurrent();
    lastRevision = (await api('/api/changes')).revision;
    if (!pollTimer) pollTimer = setInterval(pollChanges, 5000);
  } catch (error) {
    token = ''; $('workspace').hidden = true; $('signin').hidden = false;
    message(error instanceof InteractionRequiredAuthError ? 'Sign in with an authorized UPES Microsoft identity.' : error.message, true);
  }
}
Office.onReady(async info => {
  if (info.host !== Office.HostType.Outlook) { message('Open this add-in from Outlook.', true); return; }
  try {
    config = await (await fetch('/addin/config', {cache:'no-store'})).json();
    $('mode').textContent = config.dryRun ? 'DRY RUN · No real email delivery' : 'LIVE · Approval required before forwarding';
    $('mailbox').textContent = config.mailbox;
    if (!config.configured) { message('UPES IT must configure Microsoft sign-in and authorized admin IDs before this add-in can access concerns.', true); return; }
    if (!Office.context.requirements.isSetSupported('NestedAppAuth', '1.1')) { message('This Outlook build does not support the required Microsoft sign-in. Update Outlook or use supported Outlook on the web.', true); return; }
    msal = await createNestablePublicClientApplication({auth:{clientId:config.clientId,authority:`https://login.microsoftonline.com/${config.tenantId}`,redirectUri:location.origin+'/addin/auth.html'},cache:{cacheLocation:'memoryStorage'}});
    $('signin').hidden = false; $('signin').addEventListener('click', () => start(true));
    Office.context.mailbox.addHandlerAsync(Office.EventType.ItemChanged, () => { if (token) selectCurrent(); }, result => {
      if (result.status !== Office.AsyncResultStatus.Succeeded) message('Message-change tracking unavailable. Use Current Email to refresh the selected message.', true);
    });
    $('currentTab').onclick = selectCurrent; $('queueTab').onclick = () => loadQueue(true); $('settingsTab').onclick = loadSettings; $('metricsTab').onclick = loadMetrics; $('metricRefresh').onclick = loadMetrics;
    $('refresh').onclick = () => ticket ? openTicket(ticket.id) : selectCurrent();
    $('queueFilter').onchange = e => { queueState = e.target.value; loadQueue(true); };
    $('queueMore').onclick = () => loadQueue(false); $('historyMore').onclick = () => loadHistory();
    await start();
  } catch (error) { message('Unable to initialize the add-in: ' + error.message, true); }
});
