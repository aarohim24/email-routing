import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';

class Node {
 constructor(tag,doc){this.tag=tag;this.doc=doc;this.children=[];this.events={};this.dataset={};this._text='';this.hidden=false;this.disabled=false;this.value='';}
 set id(value){this._id=value;this.doc.ids.set(value,this);}get id(){return this._id;}
 set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
 append(node){this.children.push(node);}replaceChildren(...nodes){this._text='';this.children=nodes;}
 addEventListener(name,callback){this.events[name]=callback;}setAttribute(){}reportValidity(){return true;}
 fire(name){return this.events[name]?.({target:this})??this['on'+name]?.({target:this});}
}
const source=await build({entryPoints:['src/taskpane.js'],absWorkingDir:fileURLToPath(new URL('..',import.meta.url)),bundle:true,write:false,format:'iife',plugins:[{name:'mock-msal-for-unit-tests',setup(build){build.onResolve({filter:/^@azure\/msal-browser$/},()=>({path:'msal',namespace:'mock'}));build.onLoad({filter:/.*/,namespace:'mock'},()=>({contents:`export class InteractionRequiredAuthError extends Error{};export async function createNestablePublicClientApplication(){return {getAllAccounts:()=>[],acquireTokenSilent:async()=>({accessToken:'test-token'}),acquireTokenPopup:async()=>({accessToken:'test-token'})}};`,loader:'js'}));}}]});
const script=source.outputFiles[0].text;
const ticket=(id,subject='Transcript request')=>({id,subject,sender:'student@demo.invalid',received_at:'2026-10-24T10:00:00Z',body:'<img src=x onerror=alert(1)>',version:3,status:'AWAITING_APPROVAL',category:'TRANSCRIPT',authority:{name:'Rita Godiyal',email:'authority@demo.invalid'},actions:['approve','reject','route'],deliveries:[]});
async function harness(contextHandler,{isAdmin=true,apiHandler=null}={}){
 const doc={ids:new Map(),createElement(tag){return new Node(tag,this);},getElementById(id){return this.ids.get(id);},querySelectorAll(selector){if(selector==='nav button')return ['currentTab','queueTab','settingsTab','metricsTab'].map(id=>this.ids.get(id));return [...this.ids.values()].flatMap(function walk(node){return [...(node.tag==='button'?[node]:[]),...node.children.flatMap(walk)];});}};
 for(const id of ['notice','signin','workspace','current','queue','settings','ticket','history','historyMore','queueItems','queueMore','authorityForms','health','mode','mailbox','currentTab','queueTab','settingsTab','metricsTab','metrics','metricMode','metricStart','metricEnd','metricRefresh','metricCards','metricDefinition','reminderPolicy','liveStatus','refresh','queueFilter']){const node=new Node(id.endsWith('Tab')||id==='refresh'?'button':'div',doc);node.id=id;}
 for(const [id,view]of [['currentTab','current'],['queueTab','queue'],['settingsTab','settings']])doc.ids.get(id).dataset.view=view;
 let ready,changed,timer;const calls=[];
 const office={HostType:{Outlook:'Outlook'},EventType:{ItemChanged:'itemChanged'},AsyncResultStatus:{Succeeded:'succeeded'},onReady(fn){ready=fn;},context:{requirements:{isSetSupported:()=>true},mailbox:{item:{internetMessageId:'first'},userProfile:{emailAddress:'alumni@demo.invalid'},addHandlerAsync(type,callback,done){changed=callback;done({status:'succeeded'});}}}};
 const fetch=async(path,options={})=>{calls.push({path,options});let data;
 if(path==='/addin/config')data={configured:true,tenantId:'test',clientId:'test',scope:'test',mailbox:'alumni@demo.invalid',dryRun:true};
 else if(path==='/api/me')data={is_admin:isAdmin,object_id:isAdmin?'test-admin':'authority'};
 else if(path==='/api/changes')data={revision:1};
 else if(path==='/api/routing')data={categories:['TRANSCRIPT','MIGRATION'],authorities:[{id:'rita',name:'Rita Godiyal',email:'authority@demo.invalid'}],routes:[],authority_users:[],reminder_policy:{enabled:1,due_hours:24,interval_hours:24,max_reminders:2,escalation_delay_hours:24,escalation_email:null}};
 else if(path.startsWith('/api/context'))data=await contextHandler(path);
 else if(path.endsWith('/history'))data={items:[],next_before:null};
 else if(path.startsWith('/api/concerns?'))data={items:[ticket(2)],next_before:null};
 else if(path.endsWith('/actions'))data={...ticket(1),actions:[],deliveries:[{kind:'FORWARD',state:'PENDING',recipient:'authority@demo.invalid',dry_run:true}]};
 else data=ticket(1);
 if(apiHandler){const override=await apiHandler(path,options);if(override!==undefined)data=override;}
 return {ok:true,json:async()=>data};};
 vm.runInNewContext(script,{Office:office,document:doc,fetch,URLSearchParams,location:{origin:'https://localhost:8000'},console,setTimeout,clearTimeout,setInterval:fn=>{timer=fn;return 1;}});
 await ready({host:'Outlook'});
 return {doc,office,calls,changed,tick:()=>timer?.()};
}
const find=(root,text)=>{if(root.tag==='button'&&root.textContent===text)return root;for(const child of root.children){const result=find(child,text);if(result)return result;}};
const settle=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};

test('initial Outlook item renders as text, never email HTML',async()=>{
 const h=await harness(()=>ticket(1,'<script>unsafe subject</script>'));
 const root=h.doc.getElementById('ticket');assert.match(root.textContent,/<script>unsafe subject/);
 assert.equal(root.children.some(n=>['script','img'].includes(n.tag)),false);
 assert.match(root.textContent,/TICKET #1/);
});
test('no selected email clears prior actionable ticket',async()=>{
 const h=await harness(()=>ticket(1));h.office.context.mailbox.item=null;await h.changed();await settle();
 assert.equal(h.doc.getElementById('ticket').textContent,'');assert.match(h.doc.getElementById('notice').textContent,/Select one received email/);
});
test('slow response for old selection cannot replace new selection',async()=>{
 let resolveOld;const old=new Promise(resolve=>{resolveOld=resolve;});
 const h=await harness(()=>ticket(1));
 // Swap the fetch handler by using a second selection whose response is deliberately delayed.
 // The harness callback branches on the current message ID in the request.
 const h2=await harness(path=>path.includes('message_id=slow')?old:ticket(path.includes('message_id=new')?3:1));
 h2.office.context.mailbox.item={internetMessageId:'slow'};h2.changed();await settle();
 h2.office.context.mailbox.item={internetMessageId:'new'};h2.changed();await settle();
 resolveOld(ticket(2));await settle();
 assert.match(h2.doc.getElementById('ticket').textContent,/TICKET #3/);
 assert.doesNotMatch(h2.doc.getElementById('ticket').textContent,/TICKET #2/);
});
test('approval requires explicit confirmation and posts a versioned command',async()=>{
 const h=await harness(()=>ticket(1));find(h.doc.getElementById('ticket'),'Approve').fire('click');
 assert.equal(h.calls.some(c=>c.path.endsWith('/actions')),false);
 await find(h.doc.getElementById('actionForm'),'Confirm approve').fire('click');await settle();
 const call=h.calls.find(c=>c.path.endsWith('/actions'));
 assert.deepEqual(JSON.parse(call.options.body),{action:'approve',version:3,reason:''});
 assert.match(h.doc.getElementById('ticket').textContent,/PENDING/);
});
test('pending approvals view is inside the same sidebar',async()=>{
 const h=await harness(()=>ticket(1));await h.doc.getElementById('queueTab').fire('click');
 assert.equal(h.doc.getElementById('queue').hidden,false);
 assert.match(h.doc.getElementById('queueItems').textContent,/#2/);
});

test('authority view exposes completion, not admin routing or metrics',async()=>{
 const h=await harness(()=>({...ticket(1),status:'FORWARDED',actions:['addressed']}),{isAdmin:false});
 assert.equal(h.doc.getElementById('settingsTab').hidden,true);
 assert.equal(h.doc.getElementById('metricsTab').hidden,true);
 assert.equal(h.doc.getElementById('queueTab').textContent,'Assigned to me');
 assert.equal(find(h.doc.getElementById('ticket'),'Approve'),undefined);
 find(h.doc.getElementById('ticket'),'Mark addressed').fire('click');
 h.doc.getElementById('reason').value='Department fulfilled this request; reference 123';
 await find(h.doc.getElementById('actionForm'),'Confirm mark addressed').fire('click');
 assert.equal(JSON.parse(h.calls.find(c=>c.path.endsWith('/actions')).options.body).action,'addressed');
});
test('admin can set an explicit due date through the existing ticket',async()=>{
 const h=await harness(()=>ticket(1));
 find(h.doc.getElementById('ticket'),'Set due date').fire('click');
 h.doc.getElementById('dueDate').value='2026-11-01T12:00';
 await find(h.doc.getElementById('actionForm'),'Save due date').fire('click');
 const call=h.calls.find(c=>c.path.endsWith('/due'));
 assert.equal(JSON.parse(call.options.body).version,3);
 assert.equal(typeof JSON.parse(call.options.body).due_at,'string');
});
test('metrics remain within Outlook and show missing denominators honestly',async()=>{
 const h=await harness(()=>ticket(1),{apiHandler:async path=>path.startsWith('/api/metrics?')?{total_concerns:0,labelled_concerns:0,definition:'No verified cases',average_turnaround_hours:null,median_turnaround_hours:null,routing_accuracy_percent:null}:undefined});
 await h.doc.getElementById('metricsTab').fire('click');
 assert.equal(h.doc.getElementById('metrics').hidden,false);
 assert.match(h.doc.getElementById('metricCards').textContent,/Not yet measurable/);
 assert.equal(h.calls.find(c=>c.path.startsWith('/api/metrics?')).path,'/api/metrics?mode=live');
});
test('authority completion is reflected by admin polling without leaving Outlook',async()=>{
 let revision=1;
 const h=await harness(()=>ticket(1),{apiHandler:async path=>path==='/api/changes'?{revision}:path==='/api/concerns/1'?{...ticket(1),status:'ADDRESSED',actions:[]}:undefined});
 revision=2;await h.tick();
 assert.match(h.doc.getElementById('ticket').textContent,/ADDRESSED/);
 assert.match(h.doc.getElementById('liveStatus').textContent,/synchronized/);
});
test('polling preserves an in-progress admin decision and asks for refresh',async()=>{
 let revision=1;
 const h=await harness(()=>ticket(1),{apiHandler:async path=>path==='/api/changes'?{revision}:path==='/api/concerns/1'?{...ticket(1),version:4}:undefined});
 find(h.doc.getElementById('ticket'),'Reject').fire('click');
 h.doc.getElementById('reason').value='Draft explanation';revision=2;await h.tick();
 assert.equal(h.doc.getElementById('reason').value,'Draft explanation');
 assert.match(h.doc.getElementById('notice').textContent,/changed/);
});
