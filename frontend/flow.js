'use strict';

// Flow map, dashboard and scenario. Display only: the server decides every transition.
// Uses helpers defined in app.js (esc, sameId, prototypeMode, workflowRole, state) at call time.
const FLOW_STAGES = [
  {key:'created', label:'Task created'},
  {key:'instruction', label:'Instruction published'},
  {key:'explain', label:'Worker explains'},
  {key:'correction', label:'Correction'},
  {key:'confirm', label:'Confirmations'},
  {key:'done', label:'Done'}
];
const INSTRUCTION_STEPS = ['Record instruction', 'Gemini drafts requirements', 'Review and publish'];

function flowNames(){
  return prototypeMode() ? {issuer:'Project manager', operator:'Supervisor'} : {issuer:'Supervisor', operator:'Worker'};
}

function waitingList(...names){ return names.filter(Boolean).join(' and ') || null; }

function flowForTask(task){
  const names = flowNames(), corrected = Boolean(task.correction), status = task.status;
  if(prototypeMode() && task.latest_attempt && task.worker_recording_required === true && ['awaiting_supervisor_ack','awaiting_worker_rerecording','awaiting_approvals','released','verification_failed'].includes(status))
    return {stage:2, tone:'stuck', waitingOn:names.operator, headline:'New worker recording needed', corrected};
  switch(status){
    case 'draft':
      return task.instruction_recording_id
        ? {stage:1, tone:'active', waitingOn:names.issuer, headline:'Review the requirements and publish', sub:2, corrected}
        : {stage:1, tone:'active', waitingOn:names.issuer, headline:'Record the instruction', sub:0, corrected};
    case 'recording_instruction': return {stage:1, tone:'progress', waitingOn:names.issuer, headline:'Instruction is being recorded', sub:0, corrected};
    case 'transcribing_instruction': return {stage:1, tone:'progress', waitingOn:null, headline:'Gemini is transcribing the instruction', sub:1, corrected};
    case 'awaiting_worker_recording': return {stage:2, tone:'active', waitingOn:names.operator, headline:'Record the worker explaining the task', corrected};
    case 'recording_worker': return {stage:corrected?3:2, tone:'progress', waitingOn:names.operator, headline:'Worker is being recorded', corrected};
    case 'verification_pending': return {stage:corrected?3:2, tone:'progress', waitingOn:null, headline:'Gemini is comparing the explanation', corrected};
    case 'awaiting_supervisor_ack': return {stage:3, tone:'stuck', waitingOn:names.operator, headline:'Mismatch found · explain the correction and record again', corrected:true};
    case 'awaiting_worker_rerecording': return {stage:3, tone:'active', waitingOn:names.operator, headline:'Record the same worker again', corrected:true};
    case 'verification_failed': {
      const atWorker = Boolean(task.instruction && task.instruction_recording_id);
      return {stage:atWorker?(corrected?3:2):1, tone:'stuck', waitingOn:atWorker?names.operator:names.issuer, headline:'Recording or check failed · record again', corrected};
    }
    case 'awaiting_approvals':
      if(task.supervisor_ok && task.worker_ok) return {stage:5, tone:'done', waitingOn:null, headline:prototypeMode()?'Fully confirmed · real release is off in this prototype':'Confirmed · ready for release', corrected};
      return {stage:4, tone:'active', waitingOn:waitingList(!task.supervisor_ok && names.issuer, !task.worker_ok && names.operator), headline:'Matching explanation · waiting for confirmations', corrected};
    case 'released': return {stage:5, tone:'done', waitingOn:null, headline:'Released to work', corrected};
    default: return {stage:0, tone:'stuck', waitingOn:null, headline:'Status not recognised · refresh', corrected};
  }
}

function flowForDemo(demo){
  const corrected = Boolean(demo.correction_text);
  switch(demo.status){
    case 'awaiting_worker_example': return {stage:2, tone:'active', waitingOn:'Supervisor', headline:'Choose the worker’s explanation and compare it', corrected};
    case 'awaiting_supervisor_ack': return {stage:3, tone:'stuck', waitingOn:'Supervisor', headline:'Mismatch found · explain the correction', corrected:true};
    case 'awaiting_corrected_example': return {stage:3, tone:'active', waitingOn:'Supervisor', headline:'Worker explains again after the correction', corrected:true};
    case 'awaiting_approvals': return {stage:4, tone:'active', waitingOn:waitingList(!demo.pm_ok && 'Project manager', !demo.supervisor_ok && 'Supervisor'), headline:'Matching explanation · waiting for confirmations', corrected};
    case 'demo_complete': return {stage:5, tone:'done', waitingOn:null, headline:'Demo complete · both roles confirmed', corrected};
    default: return {stage:0, tone:'stuck', waitingOn:null, headline:'Status not recognised · refresh', corrected};
  }
}

function stepState(flow, index){
  if(flow.tone === 'done') return index === 3 && !flow.corrected ? 'skipped' : 'done';
  if(index < flow.stage) return index === 3 && !flow.corrected ? 'skipped' : 'done';
  if(index === flow.stage) return flow.tone === 'stuck' ? 'stuck' : 'current';
  return 'upcoming';
}

function flowMapHtml(flow, options = {}){
  const compact = options.compact === true;
  const steps = FLOW_STAGES.map((stage, index) => {
    const kind = stepState(flow, index);
    const mark = kind === 'done' ? '✓' : kind === 'stuck' ? '!' : kind === 'skipped' ? '–' : String(index + 1);
    const note = kind === 'stuck' ? 'Locked here' : kind === 'current' ? 'Now' : kind === 'skipped' ? 'Not needed' : '';
    return `<li class="flow-step ${kind}" aria-label="${esc(stage.label)}: ${esc(note || kind)}"><span class="flow-dot" aria-hidden="true">${mark}</span><span class="flow-label">${esc(stage.label)}${!compact && note ? `<small>${esc(note)}</small>` : ''}</span></li>`;
  }).join('');
  const active = Math.min(flow.stage, FLOW_STAGES.length - 1);
  const stepName = `<p class="flow-stepname">${flow.tone === 'done' ? 'All 6 steps done' : `Step ${active + 1} of 6 · ${esc(FLOW_STAGES[active].label)}${flow.tone === 'stuck' ? ' · Locked here' : ''}`}</p>`;
  let sub = '';
  if(!compact && flow.stage === 1 && Number.isInteger(flow.sub))
    sub = `<ol class="flow-substeps" aria-label="Instruction steps">${INSTRUCTION_STEPS.map((label, index) => `<li class="${index < flow.sub ? 'done' : index === flow.sub ? 'current' : ''}">${index < flow.sub ? '✓ ' : ''}${esc(label)}</li>`).join('')}</ol>`;
  const waiting = flow.waitingOn ? `<span class="flow-waiting">${flow.tone === 'progress' ? 'With' : 'Waiting on'} ${esc(flow.waitingOn)}</span>` : '';
  return `<div class="flow ${compact ? 'compact' : ''}"><ol class="flow-map">${steps}</ol>${stepName}${sub}<div class="flow-status ${flow.tone}"><strong>${flow.tone === 'stuck' ? 'Locked · ' : flow.tone === 'done' ? 'Done · ' : ''}${esc(flow.headline)}</strong>${waiting}</div></div>`;
}

function myTurn(flow){
  const role = workflowRole();
  if(role === 'admin' || !flow.waitingOn) return false;
  const mine = prototypeMode() ? (role === 'supervisor' ? 'Project manager' : 'Supervisor') : (role === 'supervisor' ? 'Supervisor' : 'Worker');
  return flow.waitingOn.includes(mine) && flow.tone !== 'progress';
}

function dashboardHtml(tasks, demos){
  const items = [
    ...demos.map(demo => ({kind:'demo', id:demo.id, title:demo.title, flow:flowForDemo(demo), meta:'Prerecorded demo · sample voices'})),
    ...tasks.map(task => ({kind:'task', id:task.id, title:task.title, flow:flowForTask(task), meta:`Live task ${shortId(task.id)}${task.worker_reference ? ' · Worker '+task.worker_reference : ''}`}))
  ];
  const isAdmin = workflowRole() === 'admin';
  const counts = {stuck:0, turn:0, moving:0, confirm:0, done:0};
  for(const item of items){
    item.turn = myTurn(item.flow);
    if(item.flow.tone === 'done') counts.done++;
    else if(item.flow.tone === 'stuck') counts.stuck++;
    else if(item.flow.stage === 4) counts.confirm++;
    else counts.moving++;
    if(item.turn) counts.turn++;
  }
  const perStage = FLOW_STAGES.map((stage, index) => ({label:stage.label, count:items.filter(item => item.flow.stage === index && item.flow.tone !== 'done' || index === 5 && item.flow.tone === 'done').length, stuck:items.filter(item => item.flow.stage === index && item.flow.tone === 'stuck').length}));
  const cards = [
    {label:'Locked · needs action', value:counts.stuck, tone:'stuck'},
    {label:isAdmin ? 'In progress' : 'Your turn', value:isAdmin ? counts.moving : counts.turn, tone:'active'},
    {label:'Waiting to confirm', value:counts.confirm, tone:'confirm'},
    {label:'Fully done', value:counts.done, tone:'done'}
  ];
  const order = item => (item.flow.tone === 'stuck' ? 0 : item.turn ? 1 : item.flow.tone === 'done' ? 4 : item.flow.stage === 4 ? 2 : 3);
  items.sort((a, b) => order(a) - order(b) || String(a.title || '').localeCompare(String(b.title || '')));
  const cardsHtml = `<div class="dash-cards">${cards.map(card => `<div class="dash-card ${card.tone}"><span class="dash-value">${card.value}</span><span class="dash-label">${esc(card.label)}</span></div>`).join('')}</div>`;
  const pipeline = `<div class="panel dash-pipeline"><div class="panel-heading"><h2>Where every task is</h2><details class="info compact"><summary aria-label="About the flow map">i</summary><p>Each task moves through six steps. A task locks at the step where it is stuck, for example when the worker’s explanation does not match, until the named person acts. Correction is skipped when the first explanation already matches.</p></details></div><ol class="pipeline">${perStage.map(stage => `<li class="${stage.stuck ? 'has-stuck' : ''}"><span class="pipe-count">${stage.count}</span><span class="pipe-label">${esc(stage.label)}</span>${stage.stuck ? `<small>${stage.stuck} locked</small>` : ''}</li>`).join('')}</ol></div>`;
  const list = items.map(item => `<button type="button" class="task-card flow-card ${item.flow.tone}${item.turn ? ' my-turn' : ''}" ${item.kind === 'demo' ? 'data-demo-id' : 'data-task-id'}="${esc(item.id)}"><span class="task-info"><strong>${esc(item.title)}${item.kind === 'demo' ? ' <span class="demo-badge">Demo</span>' : ''}${item.turn ? ' <span class="turn-badge">Your turn</span>' : ''}</strong><small>${esc(item.meta)}</small>${flowMapHtml(item.flow, {compact:true})}</span></button>`).join('');
  return {summary:cardsHtml + pipeline, list};
}

const SCENARIO = {
  setting: 'Sunrise Packaging · Line 3 · Pune · night shift',
  steps: [
    {time:'21:40', who:'Project manager', title:'A rush export order', text:'An export order for a pharmacy client must leave on the 06:00 truck. Priya, the production planner, opens EchoCheck and records the instruction: “Pack exactly 12 cartons with 24 bottles in each carton. Apply a blue label to every carton.” Gemini drafts three requirements; Priya checks them and publishes.', stage:1},
    {time:'22:05', who:'Supervisor', title:'The shift handover', text:'Ravi, the shift supervisor, meets Mahesh, a contract worker on his third night. Mahesh has no company phone or login. Ravi opens the task on the shared floor phone, reads him the recording notice and Mahesh agrees.', stage:2},
    {time:'22:06', who:'Worker · Gemini', title:'The slip', text:'Mahesh explains: “I will pack 24 cartons. I will put 12 bottles in each carton.” It is the same 288 bottles, but the wrong packing. Gemini shows what Priya asked next to Mahesh’s own words and flags two contradictions. The task locks at Correction.', stage:3, stuck:true},
    {time:'22:08', who:'Supervisor', title:'Fixed on the spot', text:'Ravi explains the difference right there and records Mahesh again from the same screen. This time every requirement matches.', stage:3},
    {time:'22:10', who:'Both', title:'Confirmed before work starts', text:'Ravi confirms Mahesh’s understanding and Priya confirms the instruction. The dashboard shows the task as fully done, and Mahesh starts packing.', stage:5}
  ],
  without: 'Without the check, the mistake surfaces at the 05:30 quality audit: 288 bottles to unpack and repack, a missed truck and a client escalation.',
  with: 'With EchoCheck it cost four minutes on the floor, before any carton was packed, with the worker’s own words on record.'
};

function scenarioHtml(){
  const timeline = SCENARIO.steps.map(step => `<li class="scenario-step${step.stuck ? ' stuck' : ''}"><div class="scenario-time">${esc(step.time)}<small>${esc(step.who)}</small></div><div class="scenario-body"><strong>${esc(step.title)}</strong><p>${esc(step.text)}</p><span class="scenario-stage">${step.stuck ? 'Locked at ' : 'Step: '}${esc(FLOW_STAGES[step.stage].label)}</span></div></li>`).join('');
  return `<div class="panel narrow scenario"><p class="eyebrow">SCENARIO · ${esc(SCENARIO.setting)}</p><h1 id="scenarioTitle">One instruction, one night shift.</h1><p>How a single misheard number is caught before it becomes 288 repacked bottles.</p><ol class="scenario-timeline">${timeline}</ol><div class="scenario-impact"><div class="impact without"><strong>Without EchoCheck</strong><p>${esc(SCENARIO.without)}</p></div><div class="impact with"><strong>With EchoCheck</strong><p>${esc(SCENARIO.with)}</p></div></div><button class="button full" id="scenarioDemoButton" type="button">Walk through it in the app</button><p class="caption" id="scenarioHint">Opens the “Cartons and bottles” demo, which uses this exact instruction and the worker’s real sample recordings. Act as Supervisor to compare, correct and confirm.</p><p class="disclosure">Illustrative example. The company, people and times are fictional; the instruction and worker explanations are the prerecorded demo clips in this app. No real work is released.</p></div>`;
}
