/* Run cards — the agent's work, made visible.
 *
 * A run is rendered as an inline card in the transcript:
 *
 *   ┌──────────────────────────────────────────────┐
 *   │ ⚙  Goal of the run              3/7   ▾      │
 *   │   EXECUTING · step title                     │
 *   ├──────────────────────────────────────────────┤
 *   │  ✓ Create the project                        │
 *   │      · mc.create_project — ok (240ms)        │
 *   │  ↻ Add a welcome note                        │
 *   │      · mc.add_note — retry: adjusted args    │
 *   │  ○ Verify the result                         │
 *   │  [approval card if waiting]                  │
 *   └──────────────────────────────────────────────┘
 *
 * Phases shown to the user are EXECUTION state, never model
 * chain-of-thought: Planning → Executing → Evaluating → Done.
 */

import { store } from './state.js';
import { api } from './api.js';
import { toast } from './toasts.js';

const PHASE_LABELS = {
  planning: 'Planning',
  executing: 'Executing',
  evaluating: 'Evaluating progress',
  adapting: 'Adjusting the plan',
  waiting: 'Waiting for you',
  finishing: 'Wrapping up',
};
const PHASE_ORDER = ['planning', 'executing', 'evaluating', 'adapting',
                     'waiting', 'finishing'];

const runCards = new Map();   // run_id -> {el, steps, ...}

export function getRunCard(runId) {
  return runCards.get(runId);
}

export function buildRunCard(message) {
  const el = document.createElement('div');
  el.className = 'run-card open';
  el.dataset.status = 'running';
  el.dataset.live = '1';
  const runId = (message.meta && message.meta.run_id) || message.id;
  const card = {
    runId,
    el,
    goal: message.content,
    steps: new Map(),
    phase: null,
    waiting: null,
  };
  runCards.set(runId, card);
  renderCard(card);
  return el;
}

function renderCard(card) {
  const el = card.el;
  el.innerHTML = '';

  // head
  const head = document.createElement('div');
  head.className = 'run-head';
  head.addEventListener('click', () => el.classList.toggle('open'));

  const icon = document.createElement('div');
  icon.className = 'run-icon';
  icon.innerHTML = gearSvg();
  head.appendChild(icon);

  const titleWrap = document.createElement('div');
  titleWrap.className = 'run-title-wrap';
  const goal = document.createElement('div');
  goal.className = 'run-goal';
  goal.textContent = card.goal;
  titleWrap.appendChild(goal);
  const line = document.createElement('div');
  line.className = 'run-status-line';
  titleWrap.appendChild(line);
  head.appendChild(titleWrap);

  const count = document.createElement('span');
  count.className = 'run-count';
  head.appendChild(count);

  const chev = document.createElement('span');
  chev.className = 'run-chev';
  chev.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>';
  head.appendChild(chev);

  el.appendChild(head);
  card.lineEl = line;
  card.countEl = count;
  card.iconEl = icon;

  // body
  const body = document.createElement('div');
  body.className = 'run-body';
  const inner = document.createElement('div');
  inner.className = 'run-body-inner';
  const steps = document.createElement('div');
  steps.className = 'run-steps';
  inner.appendChild(steps);
  const quality = document.createElement('section');
  quality.className = 'run-quality';
  quality.hidden = true;
  quality.setAttribute('aria-label', 'Production evidence');
  inner.appendChild(quality);
  body.appendChild(inner);
  el.appendChild(body);
  card.stepsEl = steps;
  card.qualityEl = quality;

  updateStatusLine(card);
  updateCounts(card);
}

function updateStatusLine(card) {
  const line = card.lineEl;
  line.innerHTML = '';
  if (card.phase && PHASE_LABELS[card.phase]) {
    const chip = document.createElement('span');
    chip.className = 'run-phase-chip';
    chip.textContent = PHASE_LABELS[card.phase];
    line.appendChild(chip);
  }
  if (card.note) {
    const note = document.createElement('span');
    note.className = 'run-note';
    note.textContent = card.note;
    note.style.cssText =
      'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
    line.appendChild(note);
  }
}

function updateCounts(card) {
  const steps = [...card.steps.values()];
  const done = steps.filter((s) => ['success', 'failed', 'skipped']
    .includes(s.status)).length;
  card.countEl.textContent = steps.length
    ? `${done}/${steps.length}` : '';
}

/* ─── event handlers (called from main.js dispatch) ─────────────── */

export function onRunStarted(ev) {
  // A brand new run in the active conversation gets a card.
  const st = store.get();
  if (ev.conversation_id !== st.activeId) return;
  let card = runCards.get(ev.run_id);
  if (card) return;
  const el = document.createElement('div');
  card = {
    runId: ev.run_id, el, goal: ev.goal, steps: new Map(), phase: null,
  };
  runCards.set(ev.run_id, card);
  renderCard(card);
  document.getElementById('chat').appendChild(el);
  scrollFollow();
}

export function onRunPlan(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  card.el.classList.add('open');
  for (const s of ev.plan.steps || []) {
    upsertStep(card, s);
  }
  updateCounts(card);
  scrollFollow();
}

export function onRunStep(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  upsertStep(card, ev.step);
  updateCounts(card);
  scrollFollow();
}

function upsertStep(card, s) {
  let step = card.steps.get(s.id);
  if (!step) {
    const el = document.createElement('div');
    el.className = 'run-step';
    el.dataset.s = s.status;
    step = { el, tools: new Map(), data: s };
    card.steps.set(s.id, step);
    card.stepsEl.appendChild(el);
    renderStep(step, s);
  } else {
    step.data = { ...step.data, ...s };
    renderStep(step, step.data);
  }
}

function renderStep(step, s) {
  const el = step.el;
  el.dataset.s = s.status;

  const st = document.createElement('div');
  st.className = 'step-status';
  st.dataset.s = s.status;
  st.setAttribute('aria-label', s.status);

  const main = document.createElement('div');
  main.className = 'step-main';
  const name = document.createElement('div');
  name.className = 'step-name';
  name.textContent = s.name || s.id;
  main.appendChild(name);
  if (s.tool) {
    const meta = document.createElement('div');
    meta.className = 'step-meta';
    meta.textContent = `${s.server}.${s.tool}`;
    main.appendChild(meta);
  }
  if (s.error) {
    const err = document.createElement('div');
    err.className = 'step-error';
    err.textContent = s.error;
    main.appendChild(err);
  }
  const tools = document.createElement('div');
  tools.className = 'step-tools';
  for (const t of step.tools.values()) {
    tools.appendChild(renderToolRow(t));
  }
  main.appendChild(tools);

  el.innerHTML = '';
  el.appendChild(st);
  el.appendChild(main);
  step.toolsEl = tools;
}

function renderToolRow(t) {
  const el = document.createElement('div');
  el.className = 'tool-row';
  el.dataset.p = t.phase;
  const phase = document.createElement('span');
  phase.className = 't-phase';
  phase.textContent = t.phase === 'called' ? '→'
    : t.phase === 'ok' ? '✓'
    : t.phase === 'error' ? '✗'
    : t.phase === 'retry' ? '↻' : '·';
  const name = document.createElement('span');
  name.className = 't-name';
  name.textContent = t.tool || '';
  const prev = document.createElement('span');
  prev.className = 't-preview';
  prev.textContent = t.preview || '';
  el.appendChild(phase);
  el.appendChild(name);
  el.appendChild(prev);
  if (t.duration_ms != null) {
    const dur = document.createElement('span');
    dur.className = 't-dur';
    dur.textContent = fmtDur(t.duration_ms);
    el.appendChild(dur);
  }
  return el;
}

export function onRunTool(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  const step = card.steps.get(ev.step_id);
  if (!step) return;
  const key = `${ev.tool}-${step.tools.size}`;
  const t = {
    tool: `${ev.server}.${ev.tool}`, phase: ev.phase,
    preview: ev.preview || '', duration_ms: ev.duration_ms,
  };
  // update the last row for this tool if it exists (phase transitions)
  let existing = null;
  for (const [k, v] of step.tools) {
    if (v.tool === t.tool && v.phase !== 'ok' && v.phase !== 'error') {
      existing = [k, v];
      break;
    }
  }
  if (existing) {
    step.tools.set(existing[0], { ...existing[1], ...t });
  } else {
    step.tools.set(key, t);
  }
  renderStep(step, step.data);
}

export function onRunPhase(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  card.phase = ev.phase;
  card.note = ev.detail || '';
  if (ev.phase === 'executing' || ev.phase === 'planning') {
    card.el.classList.add('open');
  }
  updateStatusLine(card);
  updateAgentPill(card);
}

export function onRunProgress(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  card.note = ev.note || card.note;
  updateStatusLine(card);
  updateCounts(card);
  updateAgentPill(card);
  scrollFollow();
}

export function onRunEval() { /* verdict details stay internal */ }

export function onRunQuality(ev) {
  const card = runCards.get(ev.run_id);
  const scorecard = ev.scorecard || {};
  if (!card || !scorecard.active || !card.qualityEl) return;
  const el = card.qualityEl;
  el.hidden = false;
  el.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'quality-head';
  const title = document.createElement('strong');
  title.textContent = 'Production evidence';
  const score = document.createElement('span');
  score.className = 'quality-score';
  score.dataset.passed = scorecard.passed ? '1' : '0';
  score.textContent = `${Number(scorecard.score || 0)}/100`;
  head.appendChild(title);
  head.appendChild(score);
  el.appendChild(head);

  const track = document.createElement('div');
  track.className = 'quality-track';
  const fill = document.createElement('span');
  fill.style.width = `${Math.max(0, Math.min(100,
    Number(scorecard.score || 0)))}%`;
  track.appendChild(fill);
  el.appendChild(track);

  const gates = document.createElement('div');
  gates.className = 'quality-gates';
  for (const gate of scorecard.gates || []) {
    const chip = document.createElement('span');
    chip.className = 'quality-gate';
    chip.dataset.status = gate.status || 'not_run';
    chip.title = gate.label || gate.id || '';
    const mark = gate.status === 'passed' ? '✓' : gate.status === 'unavailable'
      ? '—' : '○';
    chip.textContent = `${mark} ${gate.id || 'gate'}`;
    gates.appendChild(chip);
  }
  el.appendChild(gates);

  const note = document.createElement('p');
  note.className = 'quality-note';
  note.textContent = scorecard.passed
    ? 'All required MCP evidence gates passed. This is evidence coverage, not an artistic AAA guarantee.'
    : 'Unverified dimensions stay visible; Nex will not rename missing evidence “done.”';
  el.appendChild(note);
}

export function onRunWaiting(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  card.el.classList.add('open');
  const wrap = document.createElement('div');
  wrap.className = 'run-approval';
  wrap.dataset.run = ev.run_id;

  const title = document.createElement('div');
  title.className = 'appr-title';
  title.innerHTML = warnSvg();
  title.appendChild(document.createTextNode(
    'Approval needed — this tool is ' + categoryLabel(ev)));
  wrap.appendChild(title);

  const call = document.createElement('div');
  call.className = 'appr-call';
  call.textContent = `${ev.server}.${ev.tool}(${fmtArgs(ev.args)})`;
  wrap.appendChild(call);

  const exact = document.createElement('details');
  exact.className = 'appr-exact';
  const exactTitle = document.createElement('summary');
  exactTitle.textContent = 'Review exact arguments';
  const exactBody = document.createElement('pre');
  exactBody.textContent = safeJson(ev.args || {});
  exact.appendChild(exactTitle);
  exact.appendChild(exactBody);
  wrap.appendChild(exact);

  if (ev.reason) {
    const reason = document.createElement('div');
    reason.className = 'appr-reason';
    reason.textContent = ev.reason;
    wrap.appendChild(reason);
  }

  const actions = document.createElement('div');
  actions.className = 'appr-actions';

  const approve = document.createElement('button');
  approve.className = 'btn-approve';
  approve.textContent = 'Approve exact call once';
  approve.addEventListener('click', () => {
    resolveApproval(card, wrap, true, false);
  });

  const always = document.createElement('button');
  always.className = 'btn-approve';
  always.style.background = 'var(--accent)';
  always.style.color = '#06231d';
  always.textContent = 'Allow tool for this session';
  always.addEventListener('click', () => {
    resolveApproval(card, wrap, true, true);
  });

  const deny = document.createElement('button');
  deny.className = 'btn-deny';
  deny.textContent = 'Deny';
  deny.addEventListener('click', () => {
    resolveApproval(card, wrap, false, false);
  });

  actions.appendChild(approve);
  actions.appendChild(always);
  actions.appendChild(deny);
  wrap.appendChild(actions);

  // remove any previous approval card for this run
  const old = card.el.querySelector('.run-approval');
  if (old) old.remove();
  card.el.querySelector('.run-body-inner').appendChild(wrap);
  scrollFollow();
  toast('warn', 'Nex needs your approval',
        `${ev.server}.${ev.tool} — ${ev.reason || ''}`.slice(0, 140));
}

async function resolveApproval(card, wrap, approved, always) {
  wrap.classList.add('resolved');
  wrap.querySelectorAll('button').forEach((b) => (b.disabled = true));
  try {
    await api.resolveRun(card.runId, approved, always);
  } catch (err) {
    toast('error', 'Could not deliver the decision', err.message);
  }
}

export function onRunResumed(ev) {
  const card = runCards.get(ev.run_id);
  if (!card) return;
  const appr = card.el.querySelector('.run-approval');
  if (appr) {
    appr.classList.add('resolved');
    appr.querySelector('.appr-title').lastChild.textContent =
      ev.approved ? ' — approved' : ' — denied';
    setTimeout(() => appr.remove(), 1200);
  }
}

export function onRunCompleted(ev) {
  const card = runCards.get(ev.run_id);
  const report = ev.report || {};
  if (card) {
    delete card.el.dataset.live;
    card.el.dataset.status = report.status || 'completed';
    card.phase = null;
    card.note = '';
    const icon = card.iconEl;
    icon.innerHTML = report.status === 'completed' ? checkSvg()
      : (report.status === 'partial' ? warnSvg() : xSvg());
    updateStatusLine(card);
    updateCounts(card);
    updateAgentPill(null);
    // The summary arrives as a normal assistant message (chat flow).
  }
  if (store.get().activeRun && store.get().activeRun.run_id === ev.run_id) {
    store.set({ activeRun: null });
  }
}

export function onRunCancelled(ev) {
  const card = runCards.get(ev.run_id);
  if (card) {
    delete card.el.dataset.live;
    card.el.dataset.status = 'cancelled';
    updateAgentPill(null);
  }
}

function updateAgentPill(card) {
  const pill = document.getElementById('agent-pill');
  const text = document.getElementById('agent-pill-text');
  if (!card || !card.el.hasAttribute('data-live')) {
    pill.hidden = true;
    return;
  }
  pill.hidden = false;
  const phase = PHASE_LABELS[card.phase || 'executing'] || 'Working';
  text.textContent = card.note ? `${phase} · ${card.note}` : `${phase}…`;
}

function scrollFollow() {
  const s = document.getElementById('scroller');
  if (s.scrollHeight - s.scrollTop - s.clientHeight < 260) {
    s.scrollTo({ top: s.scrollHeight, behavior: 'smooth' });
  }
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function fmtArgs(args) {
  if (!args) return '';
  const parts = Object.entries(args).map(([k, v]) => {
    const rendered = typeof v === 'string' ? v : safeJson(v);
    return `${k}=${rendered.slice(0, 90)}`;
  });
  return parts.join(', ').slice(0, 500);
}

function fmtDur(ms) {
  if (ms == null) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function categoryLabel(ev) {
  const cat = ev.decision && ev.decision.category;
  return cat === 'code_execution'
    ? 'arbitrary code execution'
    : cat === 'destructive'
    ? 'destructive'
    : cat === 'network'
    ? 'a network operation'
    : cat === 'unknown'
    ? 'unclassified'
    : 'potentially risky';
}

function gearSvg() {
  return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M18.5 5.5l-2.1 2.1M7.6 16.4l-2.1 2.1"/></svg>';
}
function checkSvg() {
  return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
}
function warnSvg() {
  return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/></svg>';
}
function xSvg() {
  return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M7 7l10 10M17 7L7 17"/></svg>';
}
