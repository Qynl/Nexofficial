/* Chat — the transcript: messages, streaming, actions, scroll. */

import { renderMarkdown, renderStreaming } from './markdown.js';
import { store } from './state.js';
import { api } from './api.js';
import { toast } from './toasts.js';
import { buildRunCard } from './runview.js';
import { face, faceStateFor } from './face.js';
import { voice } from './voice.js';

const chatEl = () => document.getElementById('chat');
const scroller = () => document.getElementById('scroller');
const heroEl = () => document.getElementById('hero');

let autoFollow = true;
let liveMessage = null;       // {id, el, text} while streaming
let typingEl = null;

/* ─── scroll management ─────────────────────────────────────────── */

function isNearBottom() {
  const s = scroller();
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}

function scrollToBottom(smooth = true) {
  const s = scroller();
  s.scrollTo({ top: s.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

export function initChatScroll() {
  const s = scroller();
  let tidy = null;
  s.addEventListener('scroll', () => {
    const near = isNearBottom();
    if (near !== autoFollow) {
      autoFollow = near;
      document.getElementById('scroll-pill').hidden = near;
      document.body.classList.toggle('user-scrolled', !near);
    }
    // "Load earlier" is implicit: full history renders on open.
    clearTimeout(tidy);
  }, { passive: true });

  document.getElementById('scroll-pill').addEventListener('click', () => {
    autoFollow = true;
    document.getElementById('scroll-pill').hidden = true;
    scrollToBottom();
  });
}

/* ─── rendering ─────────────────────────────────────────────────── */

export function renderMessages() {
  const chat = chatEl();
  chat.innerHTML = '';
  const { messages } = store.get();
  for (const m of messages) {
    if (m.kind === 'run') {
      chat.appendChild(buildRunCard(m));
    } else {
      chat.appendChild(buildMessage(m));
    }
  }
  updateHero();
  if (messages.length) {
    autoFollow = true;
    requestAnimationFrame(() => scrollToBottom(false));
  }
}

export function updateHero() {
  const hero = heroEl();
  const { messages, activeId } = store.get();
  const empty = !messages.length;
  hero.classList.toggle('show', empty);
  face.setMode(empty && activeId ? 'hero'
                : activeId ? 'bar' : 'hero');
  if (empty) {
    renderSuggestions();
  }
}

function renderSuggestions() {
  const host = document.getElementById('hero-suggestions');
  const sub = document.getElementById('hero-sub');
  const { servers } = store.get();
  host.innerHTML = '';
  if (servers.connected > 0) {
    sub.textContent = `${servers.connected} server${servers.connected > 1 ? 's' : ''} connected · ${servers.tools} tools available`;
    const names = (servers.servers || [])
      .filter((s) => s.status === 'connected' && s.tools_count > 0)
      .slice(0, 3);
    for (const s of names) {
      addSuggestion(host, `What can you do with ${s.server}?`);
    }
    addSuggestion(host, 'What are you connected to right now?');
  } else {
    sub.textContent = 'No capability servers connected — I can only chat until you add one.';
    addSuggestion(host, 'What is Nex?');
    addSuggestion(host, 'How do I connect a server?');
  }
}

function addSuggestion(host, text) {
  const b = document.createElement('button');
  b.className = 'suggestion';
  b.textContent = text;
  b.addEventListener('click', () => {
    const input = document.getElementById('composer-input');
    input.value = text;
    input.dispatchEvent(new Event('input'));
    input.focus();
  });
  host.appendChild(b);
}

/* ─── message DOM ─────────────────────────────────────────────── */

function buildMessage(m) {
  const el = document.createElement('div');
  el.className = m.role === 'user' ? 'msg msg-user' : 'msg msg-assistant';
  el.dataset.id = m.id;

  if (m.role === 'user') {
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = m.content;
    el.appendChild(bubble);
    el.appendChild(actionRow(m));
  } else {
    const body = document.createElement('div');
    body.className = 'body';
    body.appendChild(renderMarkdown(m.content));
    el.appendChild(body);
    el.appendChild(actionRow(m));
  }
  return el;
}

function actionRow(m) {
  const row = document.createElement('div');
  row.className = 'msg-actions';

  if (m.role === 'assistant' && m.kind !== 'run') {
    addAct(row, copyIcon(), 'Copy', async () => {
      await navigator.clipboard.writeText(m.content);
      toast('success', 'Copied');
    });
    addAct(row, speakerIcon(), 'Listen', () => {
      voice.speak(m.content);
    });
    addAct(row, refreshIcon(), 'Regenerate', async () => {
      try {
        store.set({ generating: true });
        showTyping();
        await api.regenerate(m.conversation_id || store.get().activeId);
      } catch (err) {
        store.set({ generating: false });
        hideTyping();
        toast('error', 'Could not regenerate', err.message);
      }
    });
  } else if (m.role === 'user') {
    addAct(row, pencilIcon(), 'Edit & resend', () => {
      const input = document.getElementById('composer-input');
      input.value = m.content;
      input.focus();
      toast('info', 'Message loaded — press Enter to resend');
    });
    addAct(row, copyIcon(), 'Copy', async () => {
      await navigator.clipboard.writeText(m.content);
      toast('success', 'Copied');
    });
  }
  return row;
}

function addAct(row, icon, label, fn) {
  const b = document.createElement('button');
  b.title = label;
  b.setAttribute('aria-label', label);
  b.innerHTML = icon;
  b.appendChild(document.createTextNode(''));
  b.addEventListener('click', fn);
  row.appendChild(b);
}

/* ─── typing indicator ─────────────────────────────────────────── */

export function showTyping() {
  if (typingEl) return;
  typingEl = document.createElement('div');
  typingEl.className = 'msg msg-assistant';
  typingEl.innerHTML =
    '<div class="msg-typing"><span></span><span></span><span></span></div>';
  chatEl().appendChild(typingEl);
  if (autoFollow) scrollToBottom();
}

export function hideTyping() {
  if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
}

/* ─── streaming ─────────────────────────────────────────────────── */

export function onChatStarted(ev) {
  hideTyping();
  const el = document.createElement('div');
  el.className = 'msg msg-assistant';
  el.dataset.id = ev.message_id;
  const body = document.createElement('div');
  body.className = 'body caret';
  el.appendChild(body);
  chatEl().appendChild(el);
  liveMessage = { id: ev.message_id, el, text: '', body };
  if (autoFollow) scrollToBottom();
}

export function onChatDelta(ev) {
  if (!liveMessage || liveMessage.id !== ev.message_id) return;
  liveMessage.text = ev.text != null ? ev.text : liveMessage.text + ev.delta;
  const body = liveMessage.body;
  const fresh = renderStreaming(liveMessage.text);
  fresh.classList.add('caret');
  body.replaceWith(fresh);
  liveMessage.body = fresh;
  if (autoFollow) scrollToBottom();
}

export function onChatDone(ev) {
  hideTyping();
  if (liveMessage) {
    const final = renderMarkdown(ev.content || liveMessage.text);
    liveMessage.body.replaceWith(final);
    liveMessage.el.appendChild(actionRow({
      role: 'assistant', content: ev.content || liveMessage.text,
      conversation_id: ev.conversation_id,
    }));
    liveMessage = null;
    if (voice.state.ttsEnabled) {
      face.setState(faceStateFor('speaking'));
      voice.speak(ev.content, {
        onEnd: () => face.setState(faceStateFor('idle')),
      });
    }
  }
  if (autoFollow) scrollToBottom();
  store.set({ generating: false });
  hideTyping();
  refreshMessages();
}

export function onChatError(ev) {
  hideTyping();
  if (liveMessage) {
    liveMessage.el.remove();
    liveMessage = null;
  }
  const el = document.createElement('div');
  el.className = 'msg-error';
  const title = document.createElement('div');
  title.className = 'err-title';
  title.textContent = ev.message || 'Something went wrong';
  el.appendChild(title);
  if (ev.detail) {
    const d = document.createElement('div');
    d.className = 'err-detail';
    d.textContent = ev.detail;
    el.appendChild(d);
  }
  const retry = document.createElement('button');
  retry.className = 'err-retry';
  retry.textContent = 'Try again';
  retry.addEventListener('click', () => {
    el.remove();
    store.set({ generating: true });
    showTyping();
    api.regenerate(ev.conversation_id).catch(() => {
      store.set({ generating: false });
      hideTyping();
    });
  });
  el.appendChild(retry);
  chatEl().appendChild(el);
  if (autoFollow) scrollToBottom();
  store.set({ generating: false });
  face.setState(faceStateFor('error'));
  setTimeout(() => face.setState(faceStateFor('idle')), 2600);
}

export function onChatAccepted(ev) {
  // user message echoed back (another tab / same tab optimistic path)
  const st = store.get();
  if (ev.conversation_id === st.activeId && ev.message) {
    st.messages.push(ev.message);
    chatEl().appendChild(buildMessage(ev.message));
    if (autoFollow) scrollToBottom();
    updateHero();
  }
}

export async function refreshMessages() {
  const { activeId } = store.get();
  if (!activeId) return;
  try {
    const data = await api.messages(activeId);
    store.set({ messages: data.messages || [] });
    renderMessages();
  } catch { /* transient */ }
}

/* ─── icons ────────────────────────────────────────────────────── */

function copyIcon() {
  return '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
}
function speakerIcon() {
  return '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>';
}
function refreshIcon() {
  return '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/></svg>';
}
function pencilIcon() {
  return '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3l4 4L8 20l-5 1 1-5z"/></svg>';
}
