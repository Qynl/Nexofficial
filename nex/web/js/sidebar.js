/* Sidebar — conversations, search, capabilities summary. */

import { store } from './state.js';
import { api } from './api.js';
import { toast, errorToast } from './toasts.js';
import { renderMessages, updateHero } from './chat.js';

export function initSidebar() {
  const list = document.getElementById('convo-list');
  const search = document.getElementById('search-input');

  document.getElementById('btn-new-chat').addEventListener('click', newChat);
  document.getElementById('btn-collapse').addEventListener('click', () => {
    document.body.classList.add('sb-collapsed');
    document.body.classList.remove('sb-open');
  });
  document.getElementById('sidebar-peek').addEventListener('click', () => {
    document.body.classList.remove('sb-collapsed');
    document.body.classList.add('sb-open');
  });
  document.getElementById('btn-menu').addEventListener('click', () => {
    document.body.classList.toggle('sb-open');
  });

  let searchTimer = null;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = search.value.trim();
      if (!q) {
        renderList(store.get().conversations);
        return;
      }
      try {
        const res = await api.search(q);
        renderList(res.conversations || []);
      } catch { /* keep old list */ }
    }, 220);
  });

  store.subscribe((s, prev) => {
    if (s.conversations !== prev.conversations) {
      if (!search.value.trim()) renderList(s.conversations);
    }
    if (s.activeId !== prev.activeId) {
      markActive();
    }
    if (s.servers !== prev.servers) {
      updateCapDot(s.servers);
    }
  });

  function renderList(convos) {
    list.innerHTML = '';
    if (!convos.length) {
      const empty = document.createElement('div');
      empty.className = 'sb-empty';
      empty.textContent = 'No conversations yet.';
      list.appendChild(empty);
      return;
    }
    const groups = groupByDate(convos);
    for (const [label, items] of groups) {
      if (label) {
        const h = document.createElement('div');
        h.className = 'sb-group-label';
        h.textContent = label;
        list.appendChild(h);
      }
      for (const c of items) {
        list.appendChild(convoRow(c));
      }
    }
    markActive();
  }

  function convoRow(c) {
    const row = document.createElement('div');
    row.className = 'convo-row' + (c.preview ? ' has-preview' : '');
    row.dataset.id = c.id;
    row.setAttribute('role', 'listitem');
    row.tabIndex = 0;

    const name = document.createElement('div');
    name.className = 'convo-name';
    name.textContent = c.title || 'Untitled';
    row.appendChild(name);
    if (c.preview) {
      const prev = document.createElement('div');
      prev.className = 'convo-preview';
      prev.textContent = c.preview;
      row.appendChild(prev);
    }

    const acts = document.createElement('div');
    acts.className = 'convo-acts';
    const rename = document.createElement('button');
    rename.title = 'Rename';
    rename.setAttribute('aria-label', 'Rename conversation');
    rename.innerHTML = pencilIcon();
    rename.addEventListener('click', (e) => {
      e.stopPropagation();
      renameConversation(c);
    });
    const del = document.createElement('button');
    del.title = 'Delete';
    del.setAttribute('aria-label', 'Delete conversation');
    del.innerHTML = trashIcon();
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteConversation(c);
    });
    acts.appendChild(rename);
    acts.appendChild(del);
    row.appendChild(acts);

    row.addEventListener('click', () => openConversation(c.id));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') openConversation(c.id);
    });
    return row;
  }

  function markActive() {
    const activeId = store.get().activeId;
    list.querySelectorAll('.convo-row').forEach((r) => {
      r.classList.toggle('active', r.dataset.id === activeId);
    });
  }

  async function newChat() {
    try {
      const res = await api.newConversation();
      const convos = await api.conversations();
      store.set({ conversations: convos.conversations,
                  activeId: res.conversation.id,
                  messages: [] });
      renderMessages();
      updateHero();
      document.getElementById('composer-input').focus();
      if (window.innerWidth <= 900) {
        document.body.classList.remove('sb-open');
      }
    } catch (err) {
      errorToast(err);
    }
  }

  async function openConversation(id) {
    if (store.get().activeId === id) return;
    try {
      const res = await api.messages(id);
      store.set({ activeId: id, messages: res.messages || [] });
      renderMessages();
      updateHero();
      if (window.innerWidth <= 900) {
        document.body.classList.remove('sb-open');
      }
    } catch (err) {
      errorToast(err);
    }
  }

  function renameConversation(c) {
    const row = list.querySelector(`.convo-row[data-id="${c.id}"]`);
    if (!row) return;
    const nameEl = row.querySelector('.convo-name');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = c.title || '';
    input.className = 'convo-rename';
    input.style.cssText = 'width:100%;background:var(--surface-2);border:1px solid var(--border-strong);'
      + 'border-radius:6px;color:var(--fg);font-size:13px;padding:3px 7px;outline:none;font-family:inherit;';
    nameEl.replaceWith(input);
    input.focus();
    input.select();
    const done = async (save) => {
      const title = input.value.trim();
      input.replaceWith(nameEl);
      if (save && title && title !== c.title) {
        try {
          await api.renameConversation(c.id, title);
          const convos = await api.conversations();
          store.set({ conversations: convos.conversations });
        } catch (err) {
          errorToast(err);
        }
      } else {
        nameEl.textContent = c.title || 'Untitled';
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') done(true);
      if (e.key === 'Escape') done(false);
      e.stopPropagation();
    });
    input.addEventListener('blur', () => done(true));
    input.addEventListener('click', (e) => e.stopPropagation());
  }

  async function deleteConversation(c) {
    const ok = await confirmDialog({
      title: 'Delete conversation?',
      body: `“${(c.title || 'Untitled').slice(0, 60)}” and its messages will be removed permanently.`,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteConversation(c.id);
      const convos = await api.conversations();
      const patch = { conversations: convos.conversations };
      if (store.get().activeId === c.id) {
        patch.activeId = convos.conversations[0]
          ? convos.conversations[0].id : null;
        patch.messages = [];
        renderMessages();
        updateHero();
      }
      store.set(patch);
      toast('success', 'Conversation deleted');
    } catch (err) {
      errorToast(err);
    }
  }
}

export function updateCapDot(servers) {
  const dot = document.getElementById('cap-dot');
  const count = document.getElementById('cap-count');
  if (!dot) return;
  const connected = servers ? servers.connected : 0;
  const total = servers ? servers.total : 0;
  dot.dataset.state = connected === 0 ? 'none'
    : connected === total ? 'connected' : 'partial';
  dot.classList.toggle('pulse', total > 0 && connected < total);
  count.textContent = total ? `${connected}/${total}` : '';
}

function groupByDate(convos) {
  const now = new Date();
  const groups = new Map();
  const day = 86400000;
  for (const c of convos) {
    const d = new Date((c.updated_at || 0) * 1000);
    const age = (now - d) / day;
    let label;
    if (age < 1 && d.getDate() === now.getDate()) label = 'Today';
    else if (age < 2) label = 'Yesterday';
    else if (age < 7) label = 'Previous 7 days';
    else if (age < 30) label = 'Previous 30 days';
    else label = 'Older';
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(c);
  }
  return [...groups.entries()];
}

/* Confirm dialog promise. */
export function confirmDialog({ title, body, confirmLabel = 'Confirm',
                                cancelLabel = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    const overlay = document.getElementById('confirm-overlay');
    const host = document.getElementById('confirm-body');
    host.innerHTML = '';

    const h = document.createElement('h3');
    h.textContent = title;
    h.style.cssText = 'margin:0 0 8px;font-size:15.5px;';
    const p = document.createElement('p');
    p.textContent = body;
    p.style.cssText = 'margin:0 0 18px;color:var(--fg-dim);font-size:13.5px;';
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;';
    const cancel = document.createElement('button');
    cancel.className = 'btn-deny';
    cancel.textContent = cancelLabel;
    const ok = document.createElement('button');
    ok.className = 'btn-approve';
    ok.textContent = confirmLabel;
    if (danger) {
      ok.style.background = 'var(--bad)';
      ok.style.color = '#2a0b0b';
    }
    row.appendChild(cancel);
    row.appendChild(ok);
    host.appendChild(h);
    host.appendChild(p);
    host.appendChild(row);

    const close = (val) => {
      overlay.hidden = true;
      resolve(val);
    };
    ok.addEventListener('click', () => close(true));
    cancel.addEventListener('click', () => close(false));
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close(false);
    }, { once: true });
    overlay.hidden = false;
    ok.focus();
  });
}

function pencilIcon() {
  return '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3l4 4L8 20l-5 1 1-5z"/></svg>';
}
function trashIcon() {
  return '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15"/></svg>';
}
