'use strict';

// All authorization and state changes are checked again by the server.
// Nothing sensitive is persisted in browser storage.
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const state = {user:null, adminUser:null, viewAsUsers:[], csrf:null, health:null, task:null, demo:null, page:'tasks', capture:null, busy:false, generation:0, poll:null, pollBusy:false, currentTrusted:false, lastChecked:null, workerReferenceDraft:'', liveBusy:false, demos:[]};
const statusLabels = {
  draft:'Instruction draft', recording_instruction:'Supervisor recording', transcribing_instruction:'Transcribing instruction',
  awaiting_worker_recording:'Worker recording needed', recording_worker:'Worker recording', verification_pending:'Verification in progress',
  awaiting_supervisor_ack:'Supervisor correction needed', awaiting_worker_rerecording:'Fresh worker recording needed',
  verification_failed:'Check failed · work blocked', awaiting_approvals:'Human approvals needed', released:'Released to work'
};
const nextSteps = {
  draft:'Work is blocked. The assigned supervisor records the instruction, reviews its transcript and all requirements, then publishes it.',
  recording_instruction:'Work is blocked. The supervisor is recording the instruction. A canceled or incomplete recording cannot be used.',
  transcribing_instruction:'Work is blocked. The recording is being transcribed. Review the full transcript and requirements when processing finishes.',
  awaiting_worker_recording:'Work is blocked. The assigned worker signs in on a second device and records the task in their own words.',
  recording_worker:'Work is blocked. A worker recording is in progress. A new attempt invalidates earlier approvals.',
  verification_pending:'Work is blocked. AI is comparing the worker recording with every published requirement.',
  awaiting_supervisor_ack:'Work is blocked. The supervisor must explain and acknowledge the discrepancies. The worker must then make a fresh recording.',
  awaiting_worker_rerecording:'Work is blocked. Corrections have been acknowledged. The worker must explain the whole task again in a new voice recording.',
  verification_failed:'Work is blocked. Recording or verification failed. Review the error below and retry the recording after resolving it.',
  awaiting_approvals:'Work is blocked. The assigned worker and supervisor must each approve this exact passing attempt. The supervisor then requests release.',
  released:'This task has been released by the supervisor for this instruction version and worker attempt. Follow all other required workplace approvals and procedures.'
};
const PAGES = ['tasks','new','detail','demo','scenario','about'];
const sameId = (a,b) => a != null && b != null && String(a) === String(b);
const shortId = value => String(value || '').slice(0,8);
const prototypeMode = () => state.health?.prototype === true || state.user?.prototype === true;
const workflowRole = () => prototypeMode() && state.user?.role==='admin'?'admin':state.user?.workflow_role || state.user?.role;
const isSupervisor = task => workflowRole() === 'supervisor' && sameId(state.user.id,task?.supervisor_id);
const isWorker = task => workflowRole() === 'worker' && sameId(state.user.id,task?.worker_id);
const profileLabel = user => user?.display_role || ({admin:'Admin',project_manager:'Project manager',supervisor:'Supervisor',worker:'Worker'}[user?.role]) || user?.display_name || 'Account';
function roleCopy(text){
  if(!prototypeMode()) return String(text);
  return String(text).replace(/\b(supervisors?)\b/gi,word=>{
    const lower=word.toLowerCase(),plural=lower.endsWith('s');
    const mapped='project manager'+(plural?'s':'');
    return /^[A-Z]/.test(word)?mapped.charAt(0).toUpperCase()+mapped.slice(1):mapped;
  });
}
const staticCopy=[];
const textWalker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
while(textWalker.nextNode()){
  const node=textWalker.currentNode;
  if(node.parentElement && !node.parentElement.closest('script,style,#viewAsPanel,#prototypeInfo,#adminOverviewNotice,#demoPage')) staticCopy.push({node,text:node.textContent});
}
const staticLabels=Array.from(document.querySelectorAll('[aria-label]')).filter(node=>!node.closest('#viewAsPanel,#prototypeInfo,#adminOverviewNotice,#demoPage')).map(node=>({node,text:node.getAttribute('aria-label')}));
const defaultAccountCopy=Object.fromEntries(['loginIntro','accountsHelp','assignedOperatorLabel','assignmentQuestion','assignmentHelp','approvalInfoHelp','understandingInfoHelp','correctionRerecordHelp','aboutRetellingTitle','aboutRetellingHelp','aboutCorrectionTitle','aboutCorrectionHelp','aboutApprovalsHelp','aboutDisclosure','correctionTitle'].map(id=>[id,$(id).textContent]));
function applyRoleCopy(){
  staticCopy.forEach(({node,text})=>{if(node.isConnected) node.textContent=roleCopy(text);});
  staticLabels.forEach(({node,text})=>{if(node.isConnected) node.setAttribute('aria-label',roleCopy(text));});
  const prototype=prototypeMode();
  const overrides={loginIntro:'Choose a prototype account to explore the workflow.',accountsHelp:'Project manager gives the instruction. Supervisor operates a phone or shared device and records the worker’s own explanation. The worker needs no account or phone. Admin can switch views without signing out.',assignedOperatorLabel:'Assigned supervisor',assignmentQuestion:'Why assign a supervisor?',understandingInfoHelp:'The worker explains the task in their own words while the Supervisor operates the device. AI compares the recording with every requirement. Project manager confirms the instruction and Supervisor reviews the worker’s understanding. The worker has no app approval.',correctionRerecordHelp:'Ask the same worker to explain the whole task again in a new recording, including the corrections.',approvalInfoHelp:'Project manager confirms the instruction and Supervisor reviews the worker’s recorded understanding for this exact version. The worker has no account and gives no app approval. Changed instructions or new recordings require fresh confirmation. Release to real work is disabled in this prototype.',assignmentHelp:'Choose the Supervisor who will operate the device, ask the worker to explain the task, and record the worker’s own words. The worker needs no account or phone.',aboutRetellingTitle:'Worker speaks; Supervisor records.',aboutRetellingHelp:'The Supervisor operates a phone or shared shop-floor device and asks the worker to explain what the Project manager requested. The worker does not need a phone or account.',aboutCorrectionTitle:'Supervisor explains corrections.',aboutCorrectionHelp:'Explain the differences to the worker, acknowledge the correction, and ask the same worker to repeat the whole task in a new recording.',aboutApprovalsHelp:'Project manager confirms the instruction. Supervisor reviews and confirms the worker’s recorded understanding.',aboutDisclosure:'Explore the workflow with these three accounts. Recording the worker’s own words requires their consent. This prototype does not release real work.',correctionTitle:'Supervisor correction'};
  for(const [id,text] of Object.entries(defaultAccountCopy)) $(id).textContent=prototype?overrides[id]:text;
  $('assignmentInfo').setAttribute('aria-label',prototype?'About supervisor assignment':'About worker assignment');
}
const hide = (id, hidden) => $(id).classList.toggle('hidden',Boolean(hidden));
const formatDate = value => { if (!value) return ''; const d = new Date(typeof value === 'number' && value < 1e12 ? value*1000 : value); return Number.isNaN(d.valueOf()) ? '' : d.toLocaleString(); };
const statusClass = status => status === 'released' ? 'released' : ['awaiting_supervisor_ack','awaiting_worker_rerecording','verification_failed'].includes(status) ? 'needs-attention' : ['verification_pending','transcribing_instruction','recording_instruction','recording_worker'].includes(status) ? 'waiting' : '';
const releaseValid = task => task.status !== 'released' || task.release_eligible === true && task.release_environment_ready === true;
const knownWorkerSpeaker = attempt => Boolean(attempt?.speaker?.kind==='shop_floor_worker' && attempt.speaker.reference && attempt.speaker.worker_consent_confirmed===true);
const legacySpeaker = task => prototypeMode() && Boolean(task?.latest_attempt) && (!knownWorkerSpeaker(task.latest_attempt) || task.worker_recording_required===true);
const displayStatus = status => prototypeMode() && status==='awaiting_supervisor_ack'?'Supervisor correction needed':roleCopy(statusLabels[status] || 'Unknown state · work blocked');
const statusBadge = task => `<span class="status ${releaseValid(task) && task.status!=='released'?statusClass(task.status):''}">${esc(legacySpeaker(task)?'New worker recording needed':!releaseValid(task)?'Release no longer valid · work blocked':task.status==='released'?'Released · open to confirm':displayStatus(task.status))}</span>`;

class RequestError extends Error {
  constructor(message,status=0){super(message);this.status=status;}
}
function errorText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(x => typeof x === 'string' ? x : x.msg || 'Invalid request').join('; ');
  return detail?.message || 'The request could not be completed. Refresh and try again.';
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body != null && typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type','application/json');
  if (state.csrf && options.method && options.method !== 'GET') headers.set('X-CSRF-Token',state.csrf);
  let response;
  try { response = await fetch('/api'+path,{...options,headers,credentials:'same-origin',cache:'no-store'}); }
  catch (error) { if(error.name === 'AbortError') throw error; throw new RequestError('The service could not be reached. Check your connection and refresh before continuing.'); }
  let body = null;
  try { body = await response.json(); } catch (_) { /* Empty error responses are handled below. */ }
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') endSession();
    throw new RequestError(errorText(body?.detail || body?.message) || 'Request failed.',response.status);
  }
  return body;
}
function notice(message, success=false) {
  $('noticeText').textContent = message;
  $('notice').classList.toggle('success',success);
  hide('notice',false);
}
function dismissNotice(){ hide('notice',true); }
function stopTracks(capture) {
  if(capture?.timer) clearInterval(capture.timer);
  capture?.stream?.getTracks().forEach(track => track.stop());
}
function clearTaskView(){
  document.querySelectorAll('audio').forEach(audio=>{audio.pause();audio.removeAttribute('src');audio.load();});
  for(const id of ['taskList','dashboardSummary','taskFlow','demoFlow','scenarioPage','detailTitle','detailMeta','instructionText','requirementList','requirementEditor','understandingContent','historyList','correctionDisplay','versionNote','approvalScope','approvalList','extractionNotice']) $(id).replaceChildren();
  for(const id of ['instructionEditor','correctionText','taskTitle']) $(id).value='';
  $('workerReference').value='';$('workerReference').readOnly=false;hide('workerIdentityPanel',true);state.workerReferenceDraft='';
  $('newTaskForm').reset();$('reviewComplete').checked=false;$('approvalConsent').checked=false;$('recordConsent').checked=false;
  clearDemoView();
  state.task=null;state.demos=[];state.currentTrusted=false;state.lastChecked=null;state.generation++;
}
function endSession() {
  const capture = state.capture;
  if(capture) {
    capture.cancelled = true;
    capture.controller?.abort();
    if(capture.recorder?.state !== 'inactive') { try { capture.recorder?.stop(); } catch (_) {} }
    stopTracks(capture);
    capture.chunks = [];
  }
  clearTaskView();$('sessionName').textContent='';
  $('loginForm').reset();
  state.capture=null;state.user=null;state.adminUser=null;state.viewAsUsers=[];state.csrf=null;
  clearInterval(state.poll);
  hide('workspace',true);hide('session',true);hide('loginPage',false);hide('viewAsPanel',true);hide('adminOverviewNotice',true);
  $('viewAsSelect').replaceChildren();
  $('password').value='';$('recordConsent').checked=false;$('approvalConsent').checked=false;
}
async function loadSession(){
  const session = await api('/me');
  applySession(session);
}
function applySession(session){
  if(!session?.user || !session.csrf_token) throw new RequestError('The session response was incomplete. Sign in again.');
  state.user=session.user;state.csrf=session.csrf_token;state.adminUser=session.admin_user || null;state.viewAsUsers=session.view_as_users || [];
  const admin=prototypeMode() && state.adminUser?.role==='admin';
  $('sessionName').textContent=admin?`Signed in as Admin · Viewing ${profileLabel(session.user)}`:profileLabel(session.user);
  hide('session',false);hide('loginPage',true);hide('workspace',false);hide('newTab',workflowRole()!=='supervisor');
  hide('viewAsPanel',!admin);hide('adminOverviewNotice',!prototypeMode() || workflowRole()!=='admin');
  if(admin){
    $('viewAsSelect').innerHTML='<option value="">Admin</option>'+state.viewAsUsers.filter(user=>user.role!=='admin').map(user=>`<option value="${esc(user.id)}">${esc(profileLabel(user))}</option>`).join('');
    $('viewAsSelect').value=session.user.role==='admin'?'':session.user.id;
  }
  applyRoleCopy();renderPrototypeLogins();
  clearInterval(state.poll);state.poll=setInterval(pollTask,4000);
}
function renderPrototypeLogins(){
  const prototype=state.health?.prototype===true;
  hide('prototypeLogins',!prototype);
  if(!prototype){$('prototypeLogins').replaceChildren();return;}
  if($('prototypeLogins').childElementCount) return;
  const profiles=[{username:'admin',password:'admin123',label:'Admin'},{username:'manager',password:'manager123',label:'Project manager'},{username:'supervisor',password:'super123',label:'Supervisor'}];
  $('prototypeLogins').innerHTML='<p class="eyebrow">TRY A ROLE</p>'+profiles.map(profile=>`<button type="button" class="login-shortcut" aria-label="Sign in as ${profile.label}" data-login-user="${profile.username}" data-login-password="${profile.password}"><strong>Sign in as ${profile.label}</strong><small>${profile.username} / ${profile.password}</small></button>`).join('');
}
async function loadHealth(){
  try {
    state.health = await api('/health');
    const h=state.health;
    const canRelease=h.release_enabled === true && h.provider_configured === true;
    $('environment').className='environment'+(h.prototype?' prototype':canRelease?' ready':'');
    $('environmentText').textContent=h.prototype?'Prototype mode':canRelease ? 'Connected · Release requires a passing voice check and both assigned participants’ approvals.' : 'Draft and review only · Release to work is disabled in this environment. A configured production service is required.';
    hide('prototypeInfo',!h.prototype);applyRoleCopy();renderPrototypeLogins();
  } catch(error) {
    state.health=null;$('environment').className='environment unavailable';
    $('environmentText').textContent='Service unavailable · Check your connection and refresh.';hide('prototypeInfo',true);renderPrototypeLogins();
  }
}
async function setPage(page){
  if(state.capture) await cancelRecording('Recording canceled because you left the task.');
  if(!state.user) return;
  state.generation++;state.page=page;dismissNotice();
  document.querySelectorAll('audio').forEach(audio=>audio.pause());
  for(const name of PAGES) hide(name+'Page',name !== page);
  for(const name of ['tasks','new','scenario','about']) $(name+'Tab').classList.toggle('active',name===page || ['detail','demo'].includes(page) && name==='tasks');
  if(!['detail','demo'].includes(page)){state.task=null;state.demo=null;state.currentTrusted=false;state.lastChecked=null;}
  if(page==='tasks') await refreshTasks();
  if(page==='new') await loadPeople();
  if(page==='scenario') renderScenario();
  window.scrollTo({top:0,behavior:'instant'});
}
async function refreshTasks(){
  if(!state.user) return;
  const generation=state.generation;
  const demoEnabled=prototypeMode();
  const [liveResult,demoResult]=await Promise.allSettled([api('/tasks'),demoEnabled?api('/prototype/demos'):Promise.resolve([])]);
  if(!state.user || generation!==state.generation) return;
  if(liveResult.status==='rejected' && demoResult.status==='rejected'){blockTaskList();notice('Tasks could not be loaded. Check your connection and refresh.');return;}
  const tasks=liveResult.status==='fulfilled'?(Array.isArray(liveResult.value)?liveResult.value:liveResult.value?.tasks || []):[];
  const demos=demoResult.status==='fulfilled'?(Array.isArray(demoResult.value)?demoResult.value:[]):[];
  state.demos=demos.filter(demo=>demo.kind==='prerecorded_demo');
  const dashboard=dashboardHtml(tasks,state.demos);
  $('dashboardSummary').innerHTML=(tasks.length || state.demos.length)?dashboard.summary:'';
  let html=dashboard.list;
  if(demoEnabled && demoResult.status==='rejected') html+='<div class="empty">Prerecorded demos are unavailable. Refresh to try again.</div>';
  if(liveResult.status==='rejected') html+='<div class="empty">Your other tasks could not be checked. Reconnect and refresh.</div>';
  $('taskList').innerHTML=html || `<div class="empty">${esc(prototypeMode()?workflowRole()==='admin'?'No tasks yet. Choose Project manager above to create the first instruction.':'No tasks assigned yet. Project managers create instructions. Assigned Supervisors record workers’ understanding.':'No tasks assigned yet. Supervisors can create a task. Workers will see tasks assigned to their account.')}</div>`;
}
function renderScenario(){
  $('scenarioPage').innerHTML=scenarioHtml();
  $('scenarioDemoButton').addEventListener('click',()=>void openScenarioDemo());
}
async function openScenarioDemo(){
  try{
    const demos=state.demos?.length?state.demos:await api('/prototype/demos');
    const demo=(demos || []).find(item=>item.scenario_id==='demo-01-carton-quantities');
    if(!demo){notice('The Cartons and bottles demo is not available in this view. Choose Supervisor or Project manager.');return;}
    if(await openDemo(demo.id) && workflowRole()==='admin') notice('You are viewing as Admin. Choose Supervisor in “View as” to compare, correct and confirm.',true);
  }catch(error){notice(error.message);}
}
function blockTaskList(){ $('taskList').innerHTML='<div class="empty">Task status unavailable · work blocked. Reconnect and refresh to check the current instructions.</div>'; }
async function loadPeople(){
  if(workflowRole() !== 'supervisor') return;
  try {
    const result=await api('/people');
    const people=Array.isArray(result)?result:result?.people || [];
    $('workerId').innerHTML=`<option value="">${esc(prototypeMode()?'Choose a supervisor':'Choose a worker')}</option>`+people.filter(person=>person.role == null || (person.workflow_role || person.role) === 'worker').map(person=>`<option value="${esc(person.id)}">${esc(person.display_name || profileLabel(person) || person.username)}</option>`).join('');
    if(!people.length) notice(prototypeMode()?'No Supervisor account is available for this site.':'No worker accounts are available. Ask the administrator to assign workers to this site.');
  } catch(error){notice(error.message);}
}
async function openTask(id){
  if(state.capture) await cancelRecording('Recording canceled because you changed tasks.');
  const generation=++state.generation;dismissNotice();markUntrusted();
  document.querySelectorAll('audio').forEach(audio=>audio.pause());
  try {
    const task=await api('/tasks/'+encodeURIComponent(id));
    if(!state.user || generation!==state.generation) return;
    if(!sameId(state.task?.id,task.id)) state.workerReferenceDraft=task.worker_reference || '';
    state.task=task;state.demo=null;state.page='detail';state.currentTrusted=true;state.lastChecked=Date.now();
    for(const name of PAGES) hide(name+'Page',name!=='detail');
    $('tasksTab').classList.add('active');for(const tab of ['newTab','scenarioTab','aboutTab']) $(tab).classList.remove('active');
    renderTask();window.scrollTo({top:0,behavior:'instant'});
    return true;
  } catch(error){notice(error.message);return false;}
}
async function switchView(){
  if(!prototypeMode() || state.adminUser?.role!=='admin' || state.busy) return;
  const userId=$('viewAsSelect').value || null,taskId=state.task?.id,demoId=state.demo?.id;
  if((userId===null && state.user?.role==='admin') || sameId(userId,state.user?.id)) return;
  if(state.capture) await cancelRecording('Recording canceled because you changed views.');
  state.busy=true;$('viewAsSelect').disabled=true;clearTaskView();hide('workspace',true);dismissNotice();
  $('sessionName').textContent='Signed in as Admin · Switching view…';
  try{
    const session=await api('/prototype/view-as',{method:'POST',body:JSON.stringify({user_id:userId})});
    applySession(session);
    const opened=demoId?await openDemo(demoId):taskId?await openTask(taskId):false;
    if(!opened && state.user) await setPage('tasks');
  }catch(error){
    try{await loadSession();await setPage('tasks');}
    catch(_){hide('workspace',true);$('sessionName').textContent='Signed in as Admin · View unavailable';}
    notice(error.message+' Refresh the page if the selected view could not be restored.');
  }finally{state.busy=false;$('viewAsSelect').disabled=false;renderFreshness();}
}
async function reloadTask(){
  const id=state.task?.id;
  if(!id || !state.user) return;
  const generation=state.generation;
  try{
    const task=await api('/tasks/'+encodeURIComponent(id));
    if(generation===state.generation && state.task?.id===id && state.user){state.task=task;state.currentTrusted=true;state.lastChecked=Date.now();renderTask();}
  }catch(error){markUntrusted();throw error;}
}
function markUntrusted(){
  state.currentTrusted=false;
  $('approvalConsent').checked=false;$('recordConsent').checked=false;
  renderFreshness();
}
function renderFreshness(){
  if(state.demo){renderDemoFreshness();return;}
  const task=state.task;if(!task) return;
  const trusted=state.currentTrusted;
  const releasedInvalid=trusted && !releaseValid(task);
  $('freshnessNote').textContent=state.lastChecked ? `Last checked ${new Date(state.lastChecked).toLocaleTimeString()}${trusted?' · Checks refresh while this task is open.':' · Current status is unconfirmed.'}` : 'Waiting for a current status check.';
  $('taskStatus').textContent=trusted && legacySpeaker(task)?'New worker recording needed':releasedInvalid?'Release no longer valid · work blocked':trusted ? displayStatus(task.status) : 'Status unavailable · work blocked';
  $('taskStatus').className='status '+(trusted && !releasedInvalid?statusClass(task.status):'');
  const step=roleCopy(releasedInvalid?'Work is blocked. This release no longer has current verified evidence and approvals. Ask the supervisor to review the task and renew its verification.':trusted ? nextSteps[task.status] || 'Work is blocked. This task state is not recognized. Refresh or contact your supervisor.' : 'Work is blocked. The current task status cannot be confirmed. Wait for a successful connection check before recording, approving, publishing or releasing work.');
  const prototypeSteps={awaiting_worker_recording:'Next: Supervisor asks the worker to explain what the Project manager requested and records the worker’s own words. The worker needs no phone or login.',recording_worker:'The Supervisor is operating the device while the worker speaks. A new recording clears earlier approvals.',awaiting_supervisor_ack:'Next: Supervisor explains each difference to the worker, acknowledges the correction, and records the same worker repeating the whole task.',awaiting_worker_rerecording:'Next: Supervisor records the same worker explaining the whole task again, including the corrected details.',awaiting_approvals:'Next: Project manager confirms the instruction. Supervisor reviews and confirms the worker’s recorded understanding.'};
  $('nextStep').textContent=trusted && legacySpeaker(task)?'This earlier recording does not identify a worker speaker. The Supervisor must record the worker’s own understanding before this workflow can continue.':prototypeMode() && trusted && prototypeSteps[task.status]?prototypeSteps[task.status]:prototypeMode() && trusted && !releasedInvalid?step.replace(/^Work is blocked\. /,'Next: '):step;
  $('nextStep').className='next-step'+(trusted && task.status==='released' && !releasedInvalid?' released':'');
  for(const id of ['reviseButton','publishButton','acknowledgeButton']) $(id).disabled=!trusted || state.busy || Boolean(state.capture);
  $('approvalConsent').disabled=!trusted || state.busy;
  $('recordConsent').disabled=!trusted || state.busy;
  $('approveButton').disabled=!trusted || legacySpeaker(task) || !$('approvalConsent').checked || state.busy;
  $('withdrawButton').disabled=!trusted || state.busy;
  $('releaseButton').disabled=!trusted || state.busy || legacySpeaker(task) || state.health?.release_enabled!==true || state.health?.provider_configured!==true || task.release_environment_ready!==true || task.release_eligible!==true || task.latest_attempt?.verification?.passed!==true || !task.worker_ok || !task.supervisor_ok || task.status!=='awaiting_approvals';
  updateRecordingControls();
}
function renderTask(){
  const task=state.task;if(!task) return;
  const supervisor=isSupervisor(task),worker=isWorker(task),attempt=task.latest_attempt;
  $('detailTitle').textContent=task.title;
  $('taskFlow').innerHTML=flowMapHtml(flowForTask(task));
  $('detailMeta').textContent=`TASK ${shortId(task.id)}`;
  $('instructionText').textContent=task.instruction || 'No reviewed instruction has been published yet.';
  $('requirementList').innerHTML=(task.requirements || []).map(requirement=>`<li>${esc(requirement.text)}</li>`).join('');
  $('versionNote').textContent=`Instruction version ${task.instruction_revision || 0}. Approvals apply to this instruction and recorded understanding.`;
  const activeStates=['recording_instruction','transcribing_instruction','recording_worker','verification_pending'];
  hide('reviseButton',!supervisor || !task.instruction_recording_id || task.status==='draft' || activeStates.includes(task.status));
  const canPublish=supervisor && task.status==='draft' && Boolean(task.instruction_recording_id);
  hide('publishPanel',!canPublish);
  if(canPublish){
    const uncertain=task.extraction?.uncertainties || [];
    hide('extractionNotice',task.extraction?.complete===true && !uncertain.length);
    $('extractionNotice').textContent='Review carefully: the suggested requirement list may be incomplete.'+(uncertain.length?' '+uncertain.join(' '):'');
    $('instructionEditor').value=task.instruction || task.instruction_transcript || '';
    $('requirementEditor').replaceChildren();
    const requirements=task.requirements || [];
    if(requirements.length) requirements.forEach(addRequirement); else addRequirement();
    $('reviewComplete').checked=false;
  }
  $('recordConsent').checked=false;
  const interrupted=Boolean(task.active_ticket && sameId(task.active_ticket.actor_id,state.user.id) && !state.capture);
  const canRecord=supervisor && task.status==='draft' || worker && ['awaiting_worker_recording','awaiting_worker_rerecording','verification_failed','awaiting_approvals','released'].includes(task.status) && Boolean(task.instruction_recording_id);
  hide('recordPanel',!canRecord && !state.capture && !interrupted);
  const operatorCapture=prototypeMode() && worker;
  $('recordTitle').textContent=supervisor?'Record your instruction':operatorCapture?'Record worker’s understanding':'Explain the task in your own words';
  $('recordHelp').textContent=interrupted?'A previous recording is still active on the service. Cancel it before making a fresh recording.':operatorCapture?'Ask the worker to explain what the Project manager asked. The worker speaks; you operate this phone or shared shop-floor device. No worker login or phone is needed.':supervisor?'State the complete instruction, including actions, quantities, limits, deadlines and exceptions. Review the transcript before publishing.':'Use your own words to explain the whole task and its requirements. Speak only when you are ready. Typed text cannot be used as a worker verification attempt.';
  if(!prototypeMode()) $('recordHelp').textContent=interrupted?'A previous recording is still active on the service. Cancel it before making a fresh recording. Unfinished audio cannot authorize work.':supervisor?'State the complete instruction, including actions, quantities, limits, deadlines and exceptions. Review the transcript before publishing.':['awaiting_approvals','released'].includes(task.status)?'Starting a fresh recording immediately blocks work and clears earlier approvals. Explain the whole task again. Typed text cannot be used as verification.':'Use your own words to explain the whole task and its requirements. Speak only when you are ready. Typed text cannot be used as a worker verification attempt.';
  $('recordCapability').textContent=recordingCapability() || (prototypeMode()?supervisor?'Use a quiet, safe place and record the full instruction.':'Read the recording notice to the worker before recording. The worker must agree.':'Use a quiet, safe place. The worker must use a second device with their own signed-in account.');
  hide('workerIdentityPanel',!operatorCapture);
  $('workerReference').value=task.worker_reference || state.capture?.workerReference || state.workerReferenceDraft || '';
  $('workerReference').readOnly=task.worker_reference_locked===true;
  $('workerReference').disabled=Boolean(state.capture);
  $('workerReferenceHelp').textContent=task.worker_reference_locked?'This worker’s identity is fixed for this task. Record the same worker after any correction; use a new task for a different worker.':'Enter the worker’s name or badge. You can correct it until the first valid recording is stored.';
  $('recordConsentText').textContent=operatorCapture?'The worker has heard the recording notice and agreed. I am recording their own words.':'I have read this disclosure and consent to this recording and its processing for this task.';
  const disclosure=state.health?.recording_disclosure;
  $('recordDisclosure').textContent=disclosure?.notice ? `${disclosure.notice}\n\nProcessing service: ${disclosure.provider || 'unavailable'}${disclosure.model?' · Model: '+disclosure.model:''}.\nAudio retention: ${disclosure.retention_seconds} seconds. Recording limit: ${disclosure.max_seconds} seconds and ${disclosure.max_bytes} bytes.` : 'The recording policy is unavailable. Refresh after the service is restored.';
  $('consentVersion').textContent='Consent policy: '+(state.health?.consent_version || 'unavailable · recording blocked');
  updateRecordingControls();
  renderUnderstanding(task);
  hide('acknowledgmentPanel',!(prototypeMode()?worker:supervisor) || legacySpeaker(task) || task.status!=='awaiting_supervisor_ack');
  $('acknowledgmentHelp').textContent=prototypeMode()?'Explain each difference to the worker, acknowledge the correction, and ask the same worker to repeat the whole task. Record the worker again; acknowledgment does not approve the task.':'Discuss every discrepancy with the worker. Acknowledgment opens a fresh worker recording; it does not approve the task.';
  $('correctionText').value='';
  const correction=typeof task.correction === 'string'?task.correction:task.correction?.text || task.correction?.correction;
  hide('correctionPanel',!correction);
  $('correctionDisplay').textContent=correction || '';
  const approvalState=['awaiting_approvals','released'].includes(task.status) && attempt?.verification?.passed === true && !legacySpeaker(task);
  hide('approvalsPanel',!approvalState);
  $('approvalScope').textContent=roleCopy(`Instruction version ${task.instruction_revision || 0} · Current worker recording`);
  $('approvalList').innerHTML=prototypeMode()?`<li>Project manager instruction confirmation <span>${task.supervisor_ok?'Confirmed':'Confirmation needed'}</span></li><li>Supervisor review of worker understanding <span>${task.worker_ok?'Confirmed':'Confirmation needed'}</span></li>`:`<li>Worker <span>${task.worker_ok?'Approved this version':'Approval needed'}</span></li><li>Supervisor <span>${task.supervisor_ok?'Approved this version':'Approval needed'}</span></li>`;
  const mineApproved=worker?Boolean(task.worker_ok):supervisor?Boolean(task.supervisor_ok):false;
  const canApprove=approvalState && task.status==='awaiting_approvals' && (worker || supervisor) && !mineApproved;
  hide('approvalConsentLabel',!canApprove);hide('approveButton',!canApprove);
  $('approvalConsent').checked=false;$('approveButton').disabled=true;
  $('approvalStatement').textContent=prototypeMode()?worker?'I reviewed this worker’s recording and the comparison. After any corrections, I confirm the worker’s recorded understanding matches the Project manager’s instruction.':'I confirm this instruction and its requirements are correct. I reviewed the worker’s recorded understanding for this version.':worker?'I understand the full instruction shown here and confirm that this recording accurately explains what I will do.':'I reviewed the instruction, the worker’s recording transcript and every verification result. I approve this exact version and attempt.';
  hide('withdrawButton',!approvalState || !mineApproved);
  hide('releaseArea',!supervisor || !approvalState || task.status==='released');
  const configured=state.health?.release_enabled === true && state.health?.provider_configured === true && task.release_environment_ready === true;
  $('releaseHelp').textContent=!configured ? (prototypeMode()?'Prototype preview · Release to real work is off.':'Release is disabled in this environment. Development or unconfigured AI checks cannot authorize work.') : task.worker_ok && task.supervisor_ok ? 'Both participants have approved this passing attempt. Release only after all other required workplace approvals are complete.' : 'Both assigned participants must approve this passing attempt before release.';
  $('releaseButton').disabled=!configured || task.release_eligible !== true || !task.worker_ok || !task.supervisor_ok || !approvalState || task.status!=='awaiting_approvals';
  const history=task.history || [];
  renderHistory(history);
  if($('historyDetails').open) void loadHistory();
  renderFreshness();
}
function renderHistory(history){
  const labels={recording_consent_accepted:'Recording consent accepted',recording_upload_started:'Recording received for processing',recording_stored:'Recording secured',verification_completed:'Understanding checked',instruction_revision_started:'New instruction version started',instruction_published:'Instruction published',verification_approved:'Understanding approved',verification_approval_withdrawn:'Approval withdrawn',work_released:'Task released to work',task_created:'Task created',discrepancies_acknowledged:'Supervisor correction acknowledged',instruction_transcribed:'Instruction transcribed',recording_cancelled:'Recording canceled'};
  $('historyList').innerHTML=history.length ? history.map(event=>{const action=event.action || event.event || event.type || 'Activity',actor=event.actor_id || event.actor,person=sameId(actor,state.task?.supervisor_id)?prototypeMode()?'Project manager':'Supervisor':sameId(actor,state.task?.worker_id)?prototypeMode()?'Supervisor':'Worker':'';return `<li>${esc(prototypeMode() && action==='discrepancies_acknowledged'?'Supervisor correction acknowledged':roleCopy(labels[action] || String(action).replace(/_/g,' ')))}${person?' · '+person:''}${formatDate(event.created || event.timestamp || event.at)?'<br>'+esc(formatDate(event.created || event.timestamp || event.at)):''}</li>`;}).join('') : '<li>No activity details available.</li>';
}
async function loadHistory(){
  const id=state.task?.id,generation=state.generation;if(!id) return;
  try{const history=await api('/tasks/'+encodeURIComponent(id)+'/history');if(state.user && sameId(state.task?.id,id) && generation===state.generation) renderHistory(Array.isArray(history)?history:[]);}
  catch(error){if(state.user && sameId(state.task?.id,id)) $('historyList').innerHTML='<li>Activity history is unavailable. Refresh and try again.</li>';}
}
function renderUnderstanding(task){
  const attempt=task.latest_attempt,verification=attempt?.verification,prototype=prototypeMode(),attributed=knownWorkerSpeaker(attempt) && task.worker_recording_required!==true;
  let html='';
  if(task.failure_reason) html+=`<div class="disclosure">${esc(task.failure_reason)}</div>`;
  if(!attempt){$('understandingContent').innerHTML=html+'<p>'+esc(prototype?'No worker recording has been checked yet. After the instruction is published, the Supervisor records the worker explaining it. The worker needs no phone or account.':'No worker recording has been checked yet. The assigned worker records on a second device after the instruction is published.')+'</p>';return;}
  if(prototype && !attributed) html+='<div class="disclosure">This earlier recording does not identify a worker speaker. It cannot be treated as the worker’s own words. The Supervisor must make a new recording of the worker’s understanding.</div>';
  if(prototype && attributed) html+=`<div class="speaker-identity"><strong>Worker: ${esc(attempt.speaker.reference)}</strong><span>Device operated by: Supervisor</span></div>`;
  html+=`<p class="caption">${prototype && !attributed?'Earlier recording':'Recorded understanding'}${attempt.status?' · '+esc(String(attempt.status).replace(/_/g,' ')):''}</p>`;
  if(attempt.transcript) html+=`<p class="caption">${prototype?attributed?'Worker’s words · Read-only recording transcript':'Earlier recording · Speaker not identified':'Read-only recording transcript'}</p><div class="transcript">${esc(attempt.transcript)}</div>`;
  if(attempt.recording_id) html+=`<p class="caption">${esc(prototype && !attributed?'Listen to the earlier recording':'Listen to this worker recording')}</p><audio controls preload="none" controlslist="nodownload" aria-label="${prototype && !attributed?'Earlier recording':'Worker recording'}" src="/api/recordings/${encodeURIComponent(attempt.recording_id)}"></audio>`;
  if(prototype && !attributed){$('understandingContent').innerHTML=html;return;}
  if(verification){
    html+=`<p class="verification-summary"><strong>${verification.passed===true?'Voice check passed · human approvals still required':'Work blocked · review the differences'}</strong></p><p>${esc(verification.summary || '')}</p>`;
    const requirements=task.requirements || [];
    const reasons={matched:'This requirement was explicitly matched in the recording.',contradiction:'The recorded explanation conflicts with this requirement.',missing:'This requirement was not identified in the recorded explanation.',uncertain:'The check could not confirm a clear match for this requirement.'};
    html+=(verification.requirements || []).map(result=>{
      const expected=requirements.find(item=>sameId(item.id,result.id))?.text || result.text || result.id;
      const evidence=typeof result.evidence==='string'?result.evidence:JSON.stringify(result.evidence || '');
      if(!prototype) return `<div class="verification-result"><strong>${esc(expected)}</strong><span class="result-tag ${result.status!=='matched'?'issue':''}">${esc(String(result.status || 'uncertain').replace(/_/g,' '))}</span><p>${esc(evidence || 'No evidence provided.')}</p></div>`;
      return `<div class="verification-result comparison-result"><span class="result-tag ${result.status!=='matched'?'issue':''}">${esc(String(result.status || 'uncertain').replace(/_/g,' '))}</span><div class="comparison-label">What project manager asked</div><p class="comparison-value">${esc(expected)}</p><div class="comparison-label">What worker said</div><p class="comparison-value">${esc(evidence || 'No supporting quote was identified for this requirement.')}</p><p class="comparison-reason">${esc(reasons[result.status] || reasons.uncertain)}</p></div>`;
    }).join('');
    for(const [key,label] of [['extra_actions','Additional actions to review'],['uncertainties','Uncertainties']]) if(Array.isArray(verification[key]) && verification[key].length) html+=`<p class="verification-summary"><strong>${label}</strong></p><ul class="detail-list">${verification[key].map(value=>`<li>${esc(typeof value==='string'?value:value.text || value.reason || JSON.stringify(value))}</li>`).join('')}</ul>`;
    const differences=(verification.requirements || []).some(result=>result.status!=='matched') || Boolean(verification.extra_actions?.length || verification.uncertainties?.length);
    if(prototype && differences) html+=`<div class="disclosure">${isWorker(task)?'Explain this difference to the worker, acknowledge the correction, and ask them to repeat the whole task.':'The Supervisor explains the differences to the worker, acknowledges the correction, and records the worker repeating the whole task.'}</div>`;
  } else html+='<p>The check is pending or unavailable. This recording cannot authorize work.</p>';
  $('understandingContent').innerHTML=html;
}
const demoStatuses={awaiting_worker_example:'Choose a worker example',awaiting_supervisor_ack:'Supervisor correction needed',awaiting_corrected_example:'Choose a fresh example',awaiting_approvals:'Demo confirmations needed',demo_complete:'Demo complete'};
const demoVariantLabels={correct:'Correct understanding',incorrect:'Incorrect understanding',corrected:'Correct after correction',still_incorrect:'Still incorrect after correction'};
const demoStatusLabel=status=>demoStatuses[status] || 'Status unavailable';
const validDemo=demo=>prototypeMode() && demo?.kind==='prerecorded_demo' && demo.source==='scripted_demo' && demo.provider_used===false && demo.real_release_enabled===false;
const demoIsManager=demo=>state.user?.role==='project_manager' && sameId(state.user.id,demo.project_manager_id);
const demoIsSupervisor=demo=>state.user?.role==='supervisor' && sameId(state.user.id,demo.supervisor_id);
const demoVariantList=demo=>{const allowed=demo?.status==='awaiting_worker_example'?['correct','incorrect']:demo?.status==='awaiting_corrected_example'?['corrected','still_incorrect']:[];return (Array.isArray(demo?.available_variants)?demo.available_variants:[]).filter(variant=>variant && allowed.includes(variant.id));};
const demoAudioUrl=(demo,variant)=>'/api/prototype/demos/'+encodeURIComponent(demo.id)+'/audio/'+encodeURIComponent(variant);
function clearDemoView(){
  state.demo=null;
  for(const id of ['demoTitle','demoStatus','demoNextStep','demoFreshness','demoInstruction','demoRequirements','demoVariant','demoSelectedTranscript','demoPreviousCorrection','demoComparison','demoApprovalList','demoApprovalStatement','demoHistory','demoLiveResult','demoLiveStatus']) $(id).replaceChildren();
  $('demoCorrectionText').value='';
}
async function openDemo(id){
  if(!prototypeMode() || !state.user) return false;
  if(state.capture) await cancelRecording('Recording canceled because you opened a prerecorded demo.');
  const generation=++state.generation;dismissNotice();markUntrusted();
  document.querySelectorAll('audio').forEach(audio=>audio.pause());
  try{
    const demo=await api('/prototype/demos/'+encodeURIComponent(id));
    if(!validDemo(demo)) throw new RequestError('This example could not be identified as a prerecorded demo. Refresh the task list.');
    if(!state.user || generation!==state.generation) return false;
    state.task=null;state.demo=demo;state.page='demo';state.currentTrusted=true;state.lastChecked=Date.now();
    for(const page of PAGES) hide(page+'Page',page!=='demo');
    $('tasksTab').classList.add('active');for(const tab of ['newTab','scenarioTab','aboutTab']) $(tab).classList.remove('active');
    renderDemo();window.scrollTo({top:0,behavior:'instant'});return true;
  }catch(error){notice(error.message);return false;}
}
async function reloadDemo(){
  const id=state.demo?.id,generation=state.generation;if(!id || !state.user) return;
  try{
    const demo=await api('/prototype/demos/'+encodeURIComponent(id));
    if(!validDemo(demo)) throw new RequestError('The prerecorded demo could not be verified. Refresh before continuing.');
    if(state.user && sameId(state.demo?.id,id) && generation===state.generation){state.demo=demo;state.currentTrusted=true;state.lastChecked=Date.now();renderDemo();}
  }catch(error){markUntrusted();throw error;}
}
function renderDemo(){
  const demo=state.demo;if(!validDemo(demo)) return;
  const attempt=demo.latest_attempt;
  $('demoTitle').textContent=demo.title;
  $('demoFlow').innerHTML=flowMapHtml(flowForDemo(demo));
  $('demoInstruction').textContent=demo.instruction?.text || '';
  $('demoRequirements').innerHTML=(demo.instruction?.requirements || []).map(requirement=>`<li>${esc(requirement.text)}</li>`).join('');
  const instructionUrl=demoAudioUrl(demo,'instruction');
  if($('demoInstructionAudio').getAttribute('src')!==instructionUrl) $('demoInstructionAudio').src=instructionUrl;
  const variants=demoVariantList(demo),canChoose=demoIsSupervisor(demo) && ['awaiting_worker_example','awaiting_corrected_example'].includes(demo.status);
  hide('demoResponsePanel',!canChoose);
  $('demoResponseTitle').textContent=demo.status==='awaiting_corrected_example'?'Choose a fresh worker example':'Choose a worker example';
  $('demoResponseHelp').textContent=demo.status==='awaiting_corrected_example'?'The demo correction was acknowledged. Choose the worker’s next prepared explanation. A new demo attempt replaces earlier confirmations.':'Choose a prepared example of the worker’s understanding. Listen to the sample voice, then compare it with the instruction.';
  $('demoVariant').innerHTML='<option value="">Choose an example</option>'+variants.map(variant=>`<option value="${esc(variant.id)}">${esc(demoVariantLabels[variant.id])}</option>`).join('');
  $('demoVariant').value='';selectDemoVariant();
  hide('demoCorrectionPanel',!demoIsSupervisor(demo) || demo.status!=='awaiting_supervisor_ack' || attempt?.verification?.passed!==false);
  $('demoCorrectionText').value=demo.correction_suggestion || '';
  hide('demoPreviousCorrectionPanel',!demo.correction_text);
  $('demoPreviousCorrection').textContent=demo.correction_text || '';
  renderDemoComparison(demo);
  const passing=attempt?.verification?.passed===true && ['awaiting_approvals','demo_complete'].includes(demo.status);
  hide('demoApprovalsPanel',!passing);
  $('demoApprovalList').innerHTML=`<li>Project manager instruction confirmation <span>${demo.pm_ok?'Confirmed':'Needed'}</span></li><li>Supervisor review of worker example <span>${demo.supervisor_ok?'Confirmed':'Needed'}</span></li>`;
  const mineApproved=demoIsManager(demo)?demo.pm_ok:demoIsSupervisor(demo)?demo.supervisor_ok:false;
  hide('demoApproveButton',!passing || demo.status!=='awaiting_approvals' || !(demoIsManager(demo) || demoIsSupervisor(demo)) || Boolean(mineApproved));
  $('demoApproveButton').textContent=demoIsManager(demo)?'OK — confirm instruction':'OK — confirm worker understanding';
  $('demoApprovalStatement').textContent=demoIsManager(demo)?'Confirm the prepared instruction and requirements for this exact demo example.':demoIsSupervisor(demo)?'Confirm that you reviewed the prepared worker example and its comparison.':'Project manager and Supervisor each confirm this matching example from their own view.';
  hide('demoCompleteMessage',demo.status!=='demo_complete');
  hide('demoRestartButton',!(demoIsManager(demo) || demoIsSupervisor(demo)));
  renderDemoLive(demo);
  $('demoHistory').innerHTML=(demo.history || []).map(event=>`<li>${esc(String(event.action || event.event || 'Demo activity').replace(/_/g,' '))}${event.created?'<br>'+esc(formatDate(event.created)):''}</li>`).join('') || '<li>No demo activity yet.</li>';
  renderDemoFreshness();
}
function selectDemoVariant(){
  const demo=state.demo;if(!demo) return;
  const variant=demoVariantList(demo).find(item=>item.id===$('demoVariant').value);
  const audio=$('demoSelectedAudio');audio.pause();
  if(variant){audio.src=demoAudioUrl(demo,variant.id);audio.hidden=false;}
  else {audio.removeAttribute('src');audio.load();audio.hidden=true;}
  $('demoSelectedTranscript').textContent=variant?.transcript || '';
  hide('demoSelectedTranscript',!variant?.transcript);
  renderDemoFreshness();
}
function renderDemoComparison(demo){
  const attempt=demo.latest_attempt;
  if(!attempt){$('demoComparison').innerHTML='<p>Choose a worker example from the Supervisor view to see the prepared comparison. No microphone or live AI is needed.</p>';return;}
  const verification=attempt.verification || {},requirements=demo.instruction?.requirements || [];
  let html=`<p class="caption">Sample worker voice · ${esc(demoVariantLabels[attempt.variant] || 'Prepared example')}</p><audio controls preload="none" controlslist="nodownload" aria-label="Compared sample worker voice" src="${esc(demoAudioUrl(demo,attempt.variant))}"></audio><p class="caption">Prepared worker words</p><div class="transcript">${esc(attempt.transcript || '')}</div><p class="verification-summary"><strong>${verification.passed===true?demo.status==='demo_complete'?'Matching demo example · both confirmations saved':'Matching demo example · confirmations needed':'Differences in this demo example'}</strong></p><p>${esc(verification.summary || '')}</p>`;
  const reasons={matched:'This prepared example matches the requirement.',contradiction:'The sample explanation conflicts with this requirement.',missing:'The sample explanation omits this requirement.',uncertain:'This prepared comparison does not confirm a clear match.'};
  html+=(verification.requirements || []).map(result=>`<div class="verification-result comparison-result"><span class="result-tag ${result.status!=='matched'?'issue':''}">${esc(result.status || 'uncertain')}</span><div class="comparison-label">What project manager asked</div><p class="comparison-value">${esc(requirements.find(requirement=>sameId(requirement.id,result.id))?.text || result.text || result.id)}</p><div class="comparison-label">What worker said</div><p class="comparison-value">${esc(result.evidence || 'No supporting words in this prepared example.')}</p><p class="comparison-reason">${esc(reasons[result.status] || reasons.uncertain)}</p></div>`).join('');
  for(const [key,label] of [['extra_actions','Extra actions in the example'],['uncertainties','Unclear details']]) if(verification[key]?.length) html+=`<p><strong>${label}</strong></p><ul class="detail-list">${verification[key].map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul>`;
  if(verification.passed!==true) html+='<p class="disclosure">Supervisor reviews the difference, acknowledges the demo correction, then chooses a fresh example. An example that is still incorrect needs another correction.</p>';
  $('demoComparison').innerHTML=html;
}
const liveDecisionLabels={passed:'Live check · matches every requirement',discrepancy:'Live check · differences found',review_required:'Live check · needs human review'};
function renderDemoLive(demo){
  const configured=state.health?.provider_configured===true,latest=(demo.live_checks || [])[0];
  if(!state.liveBusy) $('demoLiveStatus').textContent=configured?'Gemini transcribes the sample and compares it with each requirement. It usually takes 10–30 seconds.':'Live Gemini is not configured on this server. The prepared comparison is still available.';
  if(!latest){$('demoLiveResult').replaceChildren();return;}
  const verification=latest.verification || {},requirements=demo.instruction?.requirements || [];
  const reasons={matched:'Gemini found this requirement in the sample.',contradiction:'Gemini found words that conflict with this requirement.',missing:'Gemini did not find this requirement in the sample.',uncertain:'Gemini could not confirm a clear match.'};
  let html=`<p class="caption">Latest live result · ${esc(demoVariantLabels[latest.variant] || latest.label || 'Sample')} · ${esc(latest.model || 'Gemini')} · ${(Number(latest.latency_ms || 0)/1000).toFixed(1)} s</p><p class="verification-summary"><strong>${esc(liveDecisionLabels[latest.decision] || 'Live check result')}</strong></p><p class="caption">${latest.agrees_with_prepared?'Agrees with the prepared result for this sample.':'Differs from the prepared result for this sample (prepared: '+esc(String(latest.expected_decision || '').replace(/_/g,' '))+'). A human reviews any difference.'}</p><p>${esc(verification.summary || '')}</p><p class="caption">Gemini transcript of the sample voice</p><div class="transcript">${esc(latest.transcript || '')}</div>`;
  html+=(verification.requirements || []).map(result=>`<div class="verification-result comparison-result"><span class="result-tag ${result.status!=='matched'?'issue':''}">${esc(result.status || 'uncertain')}</span><div class="comparison-label">What project manager asked</div><p class="comparison-value">${esc(requirements.find(requirement=>sameId(requirement.id,result.id))?.text || result.id)}</p><div class="comparison-label">What Gemini heard the worker say</div><p class="comparison-value">${esc(result.evidence || 'No supporting quote was identified.')}</p><p class="comparison-reason">${esc(reasons[result.status] || reasons.uncertain)}</p></div>`).join('');
  for(const [key,label] of [['extra_actions','Extra actions Gemini noticed'],['uncertainties','Details Gemini found unclear']]) if(verification[key]?.length) html+=`<p><strong>${label}</strong></p><ul class="detail-list">${verification[key].map(item=>`<li>${esc(typeof item==='string'?item:JSON.stringify(item))}</li>`).join('')}</ul>`;
  html+=`<p class="disclosure">${esc(latest.disclosure || 'Practice only. Not worker evidence.')}</p>`;
  $('demoLiveResult').innerHTML=html;
}
async function runDemoLiveCheck(){
  const demo=state.demo;if(!demo || !validDemo(demo) || !state.currentTrusted || state.busy || state.liveBusy) return;
  state.liveBusy=true;renderDemoFreshness();const id=demo.id,started=Date.now();
  $('demoLiveStatus').textContent='Gemini is transcribing and comparing the sample…';
  const timer=setInterval(()=>{$('demoLiveStatus').textContent=`Gemini is transcribing and comparing the sample… ${Math.floor((Date.now()-started)/1000)} s`;},1000);
  try{
    await api('/prototype/demos/'+encodeURIComponent(id)+'/live-check',{method:'POST',body:JSON.stringify({variant:$('demoLiveVariant').value})});
    clearInterval(timer);state.liveBusy=false;
    if(sameId(state.demo?.id,id)){await reloadDemo();$('demoLiveStatus').textContent='Live result received. It does not change this demo.';}
  }catch(error){clearInterval(timer);state.liveBusy=false;$('demoLiveStatus').textContent=error.message;}
  finally{clearInterval(timer);state.liveBusy=false;renderDemoFreshness();}
}
function renderDemoFreshness(){
  const demo=state.demo;if(!demo) return;
  const trusted=state.currentTrusted && validDemo(demo),supervisor=demoIsSupervisor(demo),manager=demoIsManager(demo);
  $('demoStatus').textContent=trusted?demoStatusLabel(demo.status):'Status unavailable';
  $('demoStatus').className='status waiting';
  hide('demoCompleteMessage',!trusted || demo.status!=='demo_complete');
  const steps={awaiting_worker_example:supervisor?'Choose a correct or incorrect worker example, then press OK to compare it.':'The instruction is preloaded. Open the Supervisor view to choose a worker example.',awaiting_supervisor_ack:supervisor?'Review the differences and the suggested correction, then acknowledge it before choosing a fresh worker example.':'The Supervisor must acknowledge the demo correction before a fresh worker example.',awaiting_corrected_example:supervisor?'The correction was acknowledged. Choose Correct after correction or Still incorrect after correction.':'The Supervisor chooses a fresh worker example after the correction.',awaiting_approvals:'The matching example needs separate Project manager and Supervisor confirmations.',demo_complete:'Demo complete. Both roles confirmed this example. No real work is released.'};
  $('demoNextStep').textContent=trusted?steps[demo.status] || 'This demo status is not recognized. Refresh before continuing.':'The current demo status cannot be confirmed. Reconnect before continuing; no demo action will be retried automatically.';
  $('demoFreshness').textContent=state.lastChecked?`Last checked ${new Date(state.lastChecked).toLocaleTimeString()}${trusted?' · Demo only':' · Status unconfirmed'}`:'Checking demo status…';
  const available=demoVariantList(demo).map(variant=>variant.id),selected=$('demoVariant').value;
  const choiceStage=['awaiting_worker_example','awaiting_corrected_example'].includes(demo.status);
  $('demoVariant').disabled=!trusted || state.busy || !supervisor || !choiceStage;
  $('demoAttemptButton').disabled=!trusted || state.busy || !supervisor || !choiceStage || !available.includes(selected);
  $('demoCorrectionText').disabled=!trusted || state.busy || !supervisor || demo.status!=='awaiting_supervisor_ack';
  $('demoAcknowledgeButton').disabled=!trusted || state.busy || !supervisor || demo.status!=='awaiting_supervisor_ack' || demo.latest_attempt?.verification?.passed!==false || !demo.latest_attempt?.id || $('demoCorrectionText').value.trim().length<3;
  const mineApproved=manager?demo.pm_ok:supervisor?demo.supervisor_ok:true;
  $('demoApproveButton').disabled=!trusted || state.busy || !(manager || supervisor) || mineApproved || demo.status!=='awaiting_approvals' || demo.latest_attempt?.verification?.passed!==true || !demo.latest_attempt?.id;
  $('demoRestartButton').disabled=!trusted || state.busy || !(manager || supervisor);
  $('demoLiveButton').disabled=!trusted || state.busy || state.liveBusy || state.health?.provider_configured!==true;
  $('demoLiveVariant').disabled=!trusted || state.liveBusy;
  $('demoLiveButton').textContent=state.liveBusy?'Checking with Gemini…':'Run live Gemini check';
}
async function demoMutation(endpoint,body,success){
  const demo=state.demo;if(!demo || !validDemo(demo) || !state.currentTrusted || state.busy || state.capture) return;
  state.busy=true;const id=demo.id;markUntrusted();
  try{await api('/prototype/demos/'+encodeURIComponent(id)+endpoint,{method:'POST',body:JSON.stringify(body)});await reloadDemo();notice(success,true);}
  catch(error){let refreshed=false;try{await reloadDemo();refreshed=true;}catch(_){}notice(error.message+' '+(refreshed?'The latest demo is shown. Review it before trying again.':'Reconnect and refresh before continuing.')+' No demo action was retried.');}
  finally{state.busy=false;renderDemoFreshness();}
}
async function pollDemo(){
  if(state.pollBusy || !state.user || !state.demo || state.page!=='demo' || document.hidden || state.busy) return;
  state.pollBusy=true;const id=state.demo.id,generation=state.generation;
  try{
    const demo=await api('/prototype/demos/'+encodeURIComponent(id));
    if(!validDemo(demo)) throw new RequestError('This prerecorded demo could not be verified.');
    if(state.user && sameId(state.demo?.id,id) && generation===state.generation && !state.busy){
      const changed=demo.version!==state.demo.version,reconnected=!state.currentTrusted;
      state.demo=demo;state.currentTrusted=true;state.lastChecked=Date.now();
      if(changed){renderDemo();notice('This demo changed. Review the current step before continuing.',true);}
      else {renderDemoFreshness();if(reconnected) notice('Connection restored. The current demo was checked.',true);}
    }
  }catch(error){markUntrusted();if(error.status!==401) notice(error.message);}finally{state.pollBusy=false;}
}

function addRequirement(requirement={}){
  const row=document.createElement('div');row.className='requirement-input-row';
  const id=requirement.id || 'req_'+(globalThis.crypto?.randomUUID?.().replace(/-/g,'') || Date.now().toString(36)+Math.random().toString(36).slice(2));
  const input=document.createElement('textarea');input.rows=2;input.required=true;input.maxLength=2000;input.dataset.requirementId=id;input.value=requirement.text || '';input.setAttribute('aria-label','Requirement to verify');input.placeholder='One complete requirement';
  const remove=document.createElement('button');remove.type='button';remove.className='remove-requirement';remove.setAttribute('aria-label','Remove requirement');remove.textContent='×';remove.addEventListener('click',()=>row.remove());
  row.append(input,remove);$('requirementEditor').append(row);
}
async function mutation(endpoint,body,success){
  if(!state.task || !state.currentTrusted || state.busy || state.capture) return;
  state.busy=true;const id=state.task.id;markUntrusted();
  try {
    await api('/tasks/'+encodeURIComponent(id)+endpoint,{method:'POST',body:JSON.stringify(body)});
    await reloadTask();notice(success,true);
  } catch(error){
    if(error.status===409){let refreshed=false;try{await reloadTask();refreshed=true;}catch(_){}notice(error.message+' '+(refreshed?'The latest task is shown.':'The current status could not be confirmed.')+' Review it before submitting again; no approval or release was retried.');}
    else notice(error.message);
  } finally {state.busy=false;renderFreshness();}
}
function attemptScope(){return {expected_version:state.task.version,attempt_id:state.task.latest_attempt?.id,instruction_revision:state.task.instruction_revision};}
function recordingCapability(){
  if(!window.isSecureContext) return 'Microphone access needs HTTPS (or localhost for development). Open the secure site before recording.';
  if(!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder==='undefined') return 'Voice recording is unavailable in this browser. Use a supported browser with microphone permission.';
  if(!state.health?.consent_version || !state.health?.recording_disclosure?.policy_id) return 'Recording is blocked until the consent policy is available. Refresh after the service is restored.';
  if(state.health?.provider_configured !== true) return prototypeMode()?'Voice processing is not connected yet. You can still explore the roles and task screens.':'Voice processing is not configured. Your administrator must configure the transcription and verification service before recording.';
  return '';
}
function updateRecordingControls(){
  const capture=state.capture;
  const interrupted=Boolean(state.task?.active_ticket && sameId(state.task.active_ticket.actor_id,state.user?.id) && !capture);
  hide('consentArea',Boolean(capture) || interrupted);hide('recordState',!capture);
  $('recordButton').hidden=Boolean(capture) || interrupted;
  const workerIdentityMissing=prototypeMode() && isWorker(state.task) && !$('workerReference').value.trim();
  $('recordButton').disabled=!state.currentTrusted || !$('recordConsent').checked || workerIdentityMissing || Boolean(recordingCapability()) || state.busy;
  $('stopButton').hidden=!capture || capture.phase!=='recording';
  $('cancelRecordingButton').hidden=!capture;
  $('interruptedCancelButton').hidden=!interrupted;
  if(capture){$('recordStateText').textContent=({permission:'Waiting for microphone permission',recording:'Recording',uploading:'Sending & checking recording',ticket:'Preparing recording'})[capture.phase] || 'Recording';}
}
async function startRecording(){
  const task=state.task;
  if(!task || !state.currentTrusted || state.capture || state.busy || !$('recordConsent').checked) return;
  const capability=recordingCapability();if(capability){notice(capability);return;}
  const workerReference=$('workerReference').value.trim();
  if(prototypeMode() && isWorker(task) && !workerReference){notice('Enter the worker’s name or badge before recording their understanding.');return;}
  const capture={taskId:task.id,purpose:isSupervisor(task)?'instruction':'retelling',workerReference,ticket:null,version:task.version,stream:null,recorder:null,chunks:[],bytes:0,cancelled:false,phase:'ticket',controller:new AbortController()};
  state.capture=capture;markUntrusted();updateRecordingControls();dismissNotice();
  try {
    const ticketBody={expected_version:task.version,purpose:capture.purpose,consent_version:state.health.consent_version,consent_policy_id:state.health.recording_disclosure.policy_id,consent_accepted:true};
    if(prototypeMode() && capture.purpose==='retelling'){ticketBody.worker_reference=capture.workerReference;ticketBody.worker_consent_confirmed=true;}
    const ticket=await api('/tasks/'+encodeURIComponent(task.id)+'/recording-tickets',{method:'POST',body:JSON.stringify(ticketBody),signal:capture.controller.signal});
    capture.ticket=ticket.ticket_id;capture.version=ticket.version;
    if(capture.cancelled || state.capture!==capture){await cancelTicket(capture);return;}
    await reloadTask();
    if(capture.cancelled || state.capture!==capture){await cancelTicket(capture);return;}
    capture.phase='permission';updateRecordingControls();
    const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
    capture.stream=stream;
    if(capture.cancelled || state.capture!==capture || document.hidden){stopTracks(capture);await cancelTicket(capture);return;}
    const types=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4','audio/webm','audio/ogg'];
    const mime=types.find(type=>MediaRecorder.isTypeSupported(type));
    if(!mime) throw new Error('This browser cannot produce a supported audio format. Try a current browser with WebM, Ogg, or MP4 audio recording.');
    const recorder=new MediaRecorder(stream,{mimeType:mime});capture.recorder=recorder;
    recorder.addEventListener('dataavailable',event=>{
      if(capture.cancelled || !event.data.size) return;
      capture.bytes+=event.data.size;
      if(capture.bytes>8*1024*1024){void cancelRecording('The recording exceeded 8 MB and was discarded. Make a shorter recording.');return;}
      capture.chunks.push(event.data);
    });
    recorder.addEventListener('error',()=>{void cancelRecording('The microphone recording failed and was discarded. Check microphone permission and try again.');});
    recorder.addEventListener('stop',()=>{if(!capture.cancelled) void uploadRecording(capture);});
    recorder.start(1000);capture.phase='recording';capture.started=Date.now();$('recordTimer').textContent='0:00';
    capture.timer=setInterval(()=>{
      const seconds=Math.floor((Date.now()-capture.started)/1000);$('recordTimer').textContent=`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
      if(seconds>=120){notice('The 2-minute limit was reached. Your recording is being sent for checking.',true);stopRecording();}
    },250);
    updateRecordingControls();
  } catch(error){
    if(state.capture===capture){capture.cancelled=true;stopTracks(capture);capture.chunks=[];state.capture=null;await cancelTicket(capture);if(error.status===409) await loadHealth();try{await reloadTask();}catch(_){}updateRecordingControls();}
    if(error.name!=='AbortError') notice(error.status===409?error.message+' Review the current task and recording policy, then give consent again before making a new recording.':error.name==='NotAllowedError'?'Microphone access was denied. Allow microphone access in your browser, then give consent and try again.':error.message);
  }
}
function stopRecording(){
  const capture=state.capture;if(!capture || capture.phase!=='recording') return;
  capture.phase='uploading';stopTracks(capture);updateRecordingControls();
  if(capture.recorder?.state!=='inactive') capture.recorder.stop();
}
async function uploadRecording(capture){
  stopTracks(capture);
  if(capture.cancelled || state.capture!==capture) return;
  capture.phase='uploading';updateRecordingControls();
  const blob=new Blob(capture.chunks,{type:capture.recorder?.mimeType || capture.chunks[0]?.type || 'audio/webm'});capture.chunks=[];
  try {
    if(!blob.size) throw new Error('The recording was empty. Check the microphone and make a new recording.');
    if(blob.size>8*1024*1024) throw new Error('The recording exceeded 8 MB. Make a shorter recording.');
    await api('/tasks/'+encodeURIComponent(capture.taskId)+'/recordings/'+encodeURIComponent(capture.ticket),{method:'POST',headers:{'Content-Type':blob.type,'X-Expected-Version':String(capture.version)},body:blob,signal:capture.controller.signal});
    if(capture.cancelled) return;
    state.capture=null;$('recordConsent').checked=false;await reloadTask();
    notice(capture.purpose==='instruction'?'Recording received. Review the transcript and every requirement before publishing.':'Recording received. Work remains blocked until verification and all required approvals are complete.',true);
  } catch(error){
    if(capture.cancelled) return;
    capture.cancelled=true;await cancelTicket(capture);state.capture=null;$('recordConsent').checked=false;
    try{await reloadTask();}catch(_){}
    notice(error.status===409?error.message+' Review the latest task before making a new recording.':error.message);
  } finally {if(state.capture===capture) state.capture=null;updateRecordingControls();}
}
async function cancelTicket(capture){
  if(!capture.ticket || !state.user) return;
  try {
    const task=await api('/tasks/'+encodeURIComponent(capture.taskId));
    await api('/recording-tickets/'+encodeURIComponent(capture.ticket)+'/cancel',{method:'POST',body:JSON.stringify({expected_version:task.version})});
  } catch(error){if(error.status!==409 && error.status!==401) notice('The microphone stopped and local audio was discarded. The service could not confirm cancellation. Refresh the task before continuing.');}
}
async function cancelRecording(message){
  const capture=state.capture;if(!capture) return;
  capture.cancelled=true;capture.controller.abort();
  if(capture.recorder?.state!=='inactive'){try{capture.recorder?.stop();}catch(_){}}
  stopTracks(capture);capture.chunks=[];state.capture=null;$('recordConsent').checked=false;updateRecordingControls();
  await cancelTicket(capture);
  if(state.user && state.task && sameId(state.task.id,capture.taskId)){try{await reloadTask();}catch(_){}}
  if(message) notice(message);
}
async function pollTask(){
  if(state.demo && state.page==='demo'){await pollDemo();return;}
  if(state.pollBusy || !state.user || !state.task || state.page!=='detail' || document.hidden || state.capture || state.busy) return;
  state.pollBusy=true;const id=state.task.id,generation=state.generation;
  try{
    const task=await api('/tasks/'+encodeURIComponent(id));
    if(state.user && state.task?.id===id && generation===state.generation && !state.capture && !state.busy){
      const changed=task.version!==state.task.version,reconnected=!state.currentTrusted;
      state.currentTrusted=true;state.lastChecked=Date.now();
      if(changed){state.task=task;renderTask();notice('This task has been updated. Review the current step and version before continuing.',true);}
      else {state.task=task;renderFreshness();if(reconnected) notice('Connection restored. The current task status was checked. Review it before continuing.',true);}
    }
  }catch(error){markUntrusted();if(error.status!==401) notice(error.message);}finally{state.pollBusy=false;}
}

$('loginForm').addEventListener('submit',async event=>{
  event.preventDefault();if(state.busy) return;state.busy=true;$('loginButton').disabled=true;dismissNotice();
  $('prototypeLogins').querySelectorAll('button').forEach(button=>button.disabled=true);
  try{await api('/login',{method:'POST',body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})});$('password').value='';await loadSession();await loadHealth();await setPage('tasks');}
  catch(error){notice(error.message);}finally{state.busy=false;$('loginButton').disabled=false;$('prototypeLogins').querySelectorAll('button').forEach(button=>button.disabled=false);}
});
$('prototypeLogins').addEventListener('click',event=>{
  const button=event.target.closest('[data-login-user]');
  if(!button || state.busy || state.health?.prototype!==true) return;
  $('username').value=button.dataset.loginUser;$('password').value=button.dataset.loginPassword;
  $('loginForm').requestSubmit();
});
$('viewAsSelect').addEventListener('change',()=>void switchView());
$('logoutButton').addEventListener('click',async()=>{
  if(state.capture) await cancelRecording();
  try{await api('/logout',{method:'POST',body:'{}'});endSession();dismissNotice();}
  catch(error){notice('Sign-out could not be confirmed. '+error.message+' Close this browser session if you are leaving a shared device.');}
});
$('dismissNotice').addEventListener('click',dismissNotice);
$('historyDetails').addEventListener('toggle',()=>{if($('historyDetails').open) void loadHistory();});
document.addEventListener('error',event=>{if(event.target.tagName==='AUDIO') notice(event.target.closest('#demoPage')?'This sample voice could not be played. Check your connection and retry playback. The text and comparison are prepared demo content.':'This recording could not be played. It may have expired or the service may be unavailable. Refresh the task before continuing.');},true);
document.addEventListener('play',event=>{if(event.target.tagName==='AUDIO' && event.target.closest('#demoPage')) document.querySelectorAll('#demoPage audio').forEach(audio=>{if(audio!==event.target) audio.pause();});},true);
$('tasksTab').addEventListener('click',()=>void setPage('tasks'));
$('newTab').addEventListener('click',()=>void setPage('new'));
$('aboutTab').addEventListener('click',()=>void setPage('about'));
$('scenarioTab').addEventListener('click',()=>void setPage('scenario'));
$('backButton').addEventListener('click',()=>void setPage('tasks'));
$('refreshButton').addEventListener('click',async()=>{await loadHealth();await refreshTasks();});
document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();if(state.user) void setPage('tasks');});
$('taskList').addEventListener('click',event=>{const demo=event.target.closest('[data-demo-id]');if(demo){void openDemo(demo.dataset.demoId);return;}const button=event.target.closest('[data-task-id]');if(button) void openTask(button.dataset.taskId);});
$('demoBackButton').addEventListener('click',()=>void setPage('tasks'));
$('demoVariant').addEventListener('change',selectDemoVariant);
$('demoCorrectionText').addEventListener('input',renderDemoFreshness);
$('demoAttemptButton').addEventListener('click',()=>{const demo=state.demo;if(demo && !$('demoAttemptButton').disabled) void demoMutation('/attempt',{expected_version:demo.version,variant:$('demoVariant').value},'Prepared worker example compared.');});
$('demoAcknowledgeButton').addEventListener('click',()=>{const demo=state.demo;if(demo && !$('demoAcknowledgeButton').disabled) void demoMutation('/acknowledge',{expected_version:demo.version,attempt_id:demo.latest_attempt?.id,correction:$('demoCorrectionText').value.trim()},'Demo correction acknowledged. Choose a fresh worker example.');});
$('demoApproveButton').addEventListener('click',()=>{const demo=state.demo;if(demo && !$('demoApproveButton').disabled) void demoMutation('/approve',{expected_version:demo.version,attempt_id:demo.latest_attempt?.id,approved:true},'Your confirmation was saved for this demo example.');});
$('demoLiveButton').addEventListener('click',()=>void runDemoLiveCheck());
$('demoRestartButton').addEventListener('click',()=>{const demo=state.demo;if(demo && !$('demoRestartButton').disabled) void demoMutation('/restart',{expected_version:demo.version},'This demo has restarted. Earlier activity remains in its history.');});
$('newTaskForm').addEventListener('submit',async event=>{
  event.preventDefault();if(state.busy || workflowRole()!=='supervisor') return;state.busy=true;$('createButton').disabled=true;
  try{const task=await api('/tasks',{method:'POST',body:JSON.stringify({title:$('taskTitle').value.trim(),worker_id:$('workerId').value})});$('newTaskForm').reset();await openTask(task.id);notice(roleCopy('Task created. Record the full supervisor instruction next.'),true);}
  catch(error){notice(error.message);}finally{state.busy=false;$('createButton').disabled=false;}
});
$('addRequirementButton').addEventListener('click',()=>addRequirement());
$('publishForm').addEventListener('submit',event=>{
  event.preventDefault();if(!state.task || !$('reviewComplete').checked) return;
  const requirements=Array.from($('requirementEditor').querySelectorAll('textarea')).map(input=>({id:input.dataset.requirementId,text:input.value.trim()}));
  if(!requirements.length || requirements.some(item=>!item.text)){notice('Add every requirement before publishing. Empty requirement rows must be completed or removed.');return;}
  void mutation('/publish',{expected_version:state.task.version,instruction:$('instructionEditor').value.trim(),requirements,reviewed_complete:true},prototypeMode()?'Instruction published. Ask the Supervisor to record the worker explaining it in their own words.':'Instruction published. The assigned worker must now record on a second device.');
});
$('reviseButton').addEventListener('click',()=>{if(state.task) void mutation('/revise',{expected_version:state.task.version},prototypeMode()?'A new instruction version has started. Make a new Project manager recording; earlier checks and confirmations no longer apply.':'A new instruction version has started. Earlier checks and approvals no longer authorize work. Make a new supervisor recording.');});
$('recordConsent').addEventListener('change',updateRecordingControls);
$('workerReference').addEventListener('input',()=>{state.workerReferenceDraft=$('workerReference').value;updateRecordingControls();});
$('recordButton').addEventListener('click',()=>void startRecording());
$('stopButton').addEventListener('click',stopRecording);
$('cancelRecordingButton').addEventListener('click',()=>void cancelRecording('Recording canceled. Audio was discarded; work remains blocked.'));
$('interruptedCancelButton').addEventListener('click',async()=>{
  if(!state.task?.active_ticket || state.busy || state.capture) return;
  state.busy=true;$('interruptedCancelButton').disabled=true;
  try{await api('/recording-tickets/'+encodeURIComponent(state.task.active_ticket.id)+'/cancel',{method:'POST',body:JSON.stringify({expected_version:state.task.version})});await reloadTask();notice('Interrupted recording canceled. Give consent and make a fresh recording.',true);}
  catch(error){try{await reloadTask();}catch(_){}notice(error.message);}finally{state.busy=false;$('interruptedCancelButton').disabled=false;}
});
$('acknowledgmentForm').addEventListener('submit',event=>{event.preventDefault();if(state.task) void mutation('/acknowledge',{expected_version:state.task.version,attempt_id:state.task.latest_attempt?.id,correction:$('correctionText').value.trim()},prototypeMode()?'Correction acknowledged. Record the same worker explaining the whole task again, including the corrected details.':'Correction acknowledged. The worker must make a fresh recording before any approval.');});
$('approvalConsent').addEventListener('change',()=>{$('approveButton').disabled=!state.currentTrusted || legacySpeaker(state.task) || !$('approvalConsent').checked || state.busy;});
$('approveButton').addEventListener('click',()=>{if(state.task && $('approvalConsent').checked) void mutation('/approve',{...attemptScope(),approved:true},prototypeMode()?'Your confirmation was saved for this instruction and worker recording.':'Your approval was saved for this exact instruction and worker attempt.');});
$('withdrawButton').addEventListener('click',()=>{if(state.task) void mutation('/withdraw',attemptScope(),'Your approval was withdrawn. Work is blocked until the required checks and approvals pass again.');});
$('releaseButton').addEventListener('click',()=>{if(state.task && !$('releaseButton').disabled) void mutation('/release',attemptScope(),prototypeMode()?'This prototype cannot release real work.':'The supervisor released this exact instruction and worker attempt. Follow all other required workplace procedures.');});
document.addEventListener('visibilitychange',()=>{if(document.hidden && state.capture) void cancelRecording('Recording canceled because the page was hidden. Make a fresh recording when you return.');else if(!document.hidden) void pollTask();});
window.addEventListener('offline',()=>{markUntrusted();blockTaskList();notice('Connection lost. Work is blocked until the current task status can be checked.');});
window.addEventListener('online',()=>void pollTask());
window.addEventListener('pagehide',()=>{const capture=state.capture;if(capture){capture.cancelled=true;capture.controller.abort();try{capture.recorder?.stop();}catch(_){}stopTracks(capture);capture.chunks=[];if(capture.ticket && state.csrf) void fetch('/api/recording-tickets/'+encodeURIComponent(capture.ticket)+'/cancel',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},body:JSON.stringify({expected_version:capture.version})}).catch(()=>{});state.capture=null;}});

(async()=>{
  await loadHealth();
  try{await loadSession();await setPage('tasks');}
  catch(error){endSession();if(error.status!==401) notice(error.message);}
})();
