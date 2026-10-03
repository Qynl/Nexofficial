/* Nex 2.0 — application boot.
 *
 * Boots the face, the store, the SSE stream and every view module,
 * then routes each server event to exactly one handler.
 */

import { store } from './state.js';
import { api, connectEvents } from './api.js';
import { toast } from './toasts.js';
import { Face, faceStateFor } from './face.js';
import { voice } from './voice.js';
import { initChatScroll, renderMessages, updateHero,
         onChatStarted, onChatDelta, onChatDone, onChatError,
         onChatAccepted, refreshMessages } from './chat.js';
import { initComposer } from './composer.js';
import { initSidebar, updateCapDot } from './sidebar.js';
import { initServers } from './servers.js';
import { initSettings, applyMotionPreference } from './settings.js';
import * as runview from './runview.js';

let face = null;

/* ─── boot ─────────────────────────────────────────────────────── */

async function boot() {
  applyMotionPreference();
  face = new Face(document.getElementById('face-canvas'));
  window.__nexFace = face;   // for debugging

  initChatScroll();
  initSidebar();
  initComposer();
  initServers();
  initSettings();
  initProviderChip();
  bindAuth();

  // initial state
  try {
    const st = await api.state();
    store.set({
      conversations: st.conversations || [],
      servers: st.servers || { servers: [], total: 0, connected: 0, tools: 0 },
      provider: st.provider,
    });
    const first = (st.conversations || [])[0];
    if (first) {
      const res = await api.messages(first.id);
      store.set({ activeId: first.id, messages: res.messages || [] });
    }
    renderMessages();
    updateHero();
    updateCapDot(store.get().servers);
    renderProviderChip();
  } catch (err) {
    if (err.code !== 'auth') {
      toast('error', 'Could not load state', err.message);
    }
  }

  // live event stream
  connectEvents(handleEvent, (status) => {
    const chip = document.getElementById('provider-chip');
    if (status === 'reconnecting') {
      chip.dataset.tone = 'bad';
      document.getElementById('pc-label').textContent = 'reconnecting…';
    }
  });

  // first interaction animates the shell in
  requestAnimationFrame(() => document.body.classList.add('ready'));
  document.getElementById('composer-input').focus();
}

/* ─── event dispatch ───────────────────────────────────────────── */

function handleEvent(ev) {
  const st = store.get();
  const forActive = ev.conversation_id && ev.conversation_id === st.activeId;

  switch (ev.type) {
    // chat
    case 'chat.accepted':
      onChatAccepted(ev);
      bumpConversation(ev.conversation_id);
      break;
    case 'chat.generating':
      if (forActive) {
        face.setState(faceStateFor('thinking'));
      }
      break;
    case 'chat.started':
      if (forActive) onChatStarted(ev);
      break;
    case 'chat.delta':
      if (forActive) {
        onChatDelta(ev);
        face.setState(faceStateFor('speaking'));
      }
      bumpConversation(ev.conversation_id);
      break;
    case 'chat.done':
      if (forActive) onChatDone(ev);
      bumpConversation(ev.conversation_id);
      break;
    case 'chat.error':
      if (forActive) onChatError(ev);
      break;
    case 'chat.regenerating':
      if (forActive) {
        store.set({ generating: true });
        import('./chat.js').then((m) => m.showTyping());
      }
      break;

    // runs
    case 'run.started':
      if (forActive) {
        runview.onRunStarted(ev);
        face.setState(faceStateFor('planning'));
        store.set({ activeRun: { run_id: ev.run_id, goal: ev.goal } });
      }
      bumpConversation(ev.conversation_id);
      break;
    case 'run.phase':
      if (forActive) {
        runview.onRunPhase(ev);
        const map = { planning: 'planning', executing: 'working',
                      evaluating: 'verifying', adapting: 'planning',
                      waiting: 'thinking', finishing: 'working' };
        face.setState(faceStateFor(map[ev.phase] || 'working'));
      }
      break;
    case 'run.plan': if (forActive) runview.onRunPlan(ev); break;
    case 'run.step': if (forActive) runview.onRunStep(ev); break;
    case 'run.tool': if (forActive) runview.onRunTool(ev); break;
    case 'run.progress': if (forActive) runview.onRunProgress(ev); break;
    case 'run.eval': if (forActive) runview.onRunEval(ev); break;
    case 'run.program': if (forActive) runview.onRunProgram(ev); break;
    case 'run.quality': if (forActive) runview.onRunQuality(ev); break;
    case 'run.waiting':
      if (forActive) runview.onRunWaiting(ev);
      break;
    case 'run.resumed': if (forActive) runview.onRunResumed(ev); break;
    case 'run.completed':
      if (forActive) runview.onRunCompleted(ev);
      if (st.activeRun && st.activeRun.run_id === ev.run_id) {
        store.set({ activeRun: null });
      }
      face.setState(faceStateFor('idle'));
      refreshIfActive(ev.conversation_id);
      break;
    case 'run.cancelled':
      if (forActive) runview.onRunCancelled(ev);
      face.setState(faceStateFor('idle'));
      break;

    // MCP servers
    case 'mcp.status':
      onServerStatus(ev);
      break;
    case 'mcp.tools':
      refreshServersQuiet();
      break;
    case 'mcp.removed':
      refreshServersQuiet();
      break;

    // providers
    case 'provider.status':
    case 'provider.paced':
    case 'provider.trouble':
    case 'provider.recovered':
    case 'provider.auth_error':
      onProviderEvent(ev);
      break;

    case 'hello':
      if (ev.servers) store.set({ servers: ev.servers });
      if (ev.provider) {
        store.set({ provider: ev.provider });
        renderProviderChip();
      }
      break;
    default:
      break;
  }
}

/* conversation list freshness without a refetch storm */
let bumpTimer = null;
function bumpConversation(cid) {
  clearTimeout(bumpTimer);
  bumpTimer = setTimeout(async () => {
    try {
      const res = await api.conversations();
      store.set({ conversations: res.conversations });
    } catch { /* transient */ }
  }, 400);
}

function refreshIfActive(cid) {
  if (cid && cid === store.get().activeId) {
    refreshMessages();
  }
}

let serversTimer = null;
function refreshServersQuiet() {
  clearTimeout(serversTimer);
  serversTimer = setTimeout(async () => {
    try {
      const res = await api.servers();
      store.set({ servers: res });
    } catch { /* transient */ }
  }, 300);
}

const prevServerStatus = new Map();
function onServerStatus(ev) {
  const s = ev.server || {};
  const before = prevServerStatus.get(s.server);
  prevServerStatus.set(s.server, s.status);
  if (before && before !== s.status) {
    if (s.status === 'connected') {
      toast('success', `${s.server} connected`,
            `${s.tools_count ?? 0} tools available`);
    } else if (s.status === 'error') {
      toast('warn', `${s.server} went down`,
            (s.error || '').slice(0, 120));
    } else if (s.status === 'disconnected') {
      toast('info', `${s.server} disconnected`);
    }
  }
  refreshServersQuiet();
}

/* ─── provider chip ───────────────────────────────────────────── */

function initProviderChip() {
  const chip = document.getElementById('provider-chip');
  chip.addEventListener('click', () => {
    import('./settings.js').then(() => {
      document.getElementById('btn-settings').click();
      document.querySelector('.settings-tab[data-tab="model"]').click();
    });
  });
}

function renderProviderChip() {
  const chip = document.getElementById('provider-chip');
  const label = document.getElementById('pc-label');
  const st = store.get();
  const prov = st.provider;
  if (!prov) {
    chip.dataset.tone = 'unknown';
    label.textContent = '…';
    return;
  }
  const chat = (prov.roles || {}).chat || {};
  const serving = chat.serving || chat.active || chat.provider || '';
  const model = chat.serving_model || chat.active_model || chat.model || '';
  label.textContent = serving
    ? `${serving} · ${model || 'default'}`
    : 'no model';
  chip.dataset.tone = !serving ? 'bad'
    : chat.fallback ? 'warn' : 'ok';
  chip.title = `Chat: ${chat.provider || '-'} (${chat.model || '-'})\n`
    + `Agent: ${((prov.roles || {}).agent || {}).provider || '-'} `
    + `(${((prov.roles || {}).agent || {}).model || '-'})`;
}

function onProviderEvent(ev) {
  // rerender from the authoritative snapshot when available
  if (ev.roles) {
    const st = store.get();
    if (st.provider) {
      store.set({ provider: { ...st.provider, roles: ev.roles } });
      renderProviderChip();
    }
  } else {
    api.providers().then((p) => {
      store.set({ provider: p });
      renderProviderChip();
    }).catch(() => {});
  }
  if (ev.type === 'provider.auth_error') {
    toast('error', 'API key rejected',
          `${ev.provider || 'provider'}: fix the key in Settings → Model`);
  } else if (ev.type === 'provider.trouble' && ev.provider) {
    toast('warn', `${ev.provider} had trouble`,
          (ev.error || '').slice(0, 120));
  }
}

/* ─── auth ────────────────────────────────────────────────────── */

function bindAuth() {
  const overlay = document.getElementById('login-overlay');
  const input = document.getElementById('login-token');
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-err');

  const show = () => {
    overlay.hidden = false;
    input.focus();
  };
  window.addEventListener('nex:unauthorized', show);

  const submit = async () => {
    const tok = input.value.trim();
    if (!tok) return;
    btn.disabled = true;
    err.hidden = true;
    try {
      await api.login(tok);
      window.location.reload();
    } catch (e) {
      err.textContent = e.message || 'token rejected';
      err.hidden = false;
      btn.disabled = false;
    }
  };
  btn.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit();
  });

  // If we were opened with ?nex_token=..., the server already set the
  // cookie and redirected; if state load failed for auth reasons, show
  // the card.
  api.state().catch((e) => {
    if (e.code === 'auth') show();
  });
}

boot();
