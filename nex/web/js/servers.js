/* Capabilities — MCP server management: discover, connect, inspect,
   monitor, reconnect, remove. This is where Nex gets its tools. */

import { store } from './state.js';
import { api } from './api.js';
import { toast, errorToast } from './toasts.js';
import { confirmDialog } from './sidebar.js';

export function initServers() {
  document.getElementById('btn-servers').addEventListener('click', () => {
    showServersView();
  });
  document.getElementById('btn-servers-back').addEventListener('click', () => {
    showChatView();
  });
  document.getElementById('btn-add-server').addEventListener('click', () => {
    addServerDialog();
  });
  renderServerList();
  store.subscribe((s, prev) => {
    if (s.servers !== prev.servers && s.view === 'servers') {
      renderServerList();
    }
  });
}

export function showServersView() {
  document.body.dataset.view = 'servers';
  document.getElementById('view-chat').hidden = true;
  document.getElementById('view-servers').hidden = false;
  store.set({ view: 'servers' });
  renderServerList();
  api.servers().then((s) => store.set({ servers: s })).catch(() => {});
  refreshProductionReadiness();
}

export function showChatView() {
  document.body.dataset.view = 'chat';
  document.getElementById('view-servers').hidden = true;
  document.getElementById('view-chat').hidden = false;
  store.set({ view: 'chat' });
}

async function refreshProductionReadiness() {
  try {
    renderProductionReadiness(await api.productionReadiness());
  } catch {
    const el = document.getElementById('production-readiness');
    if (el) el.hidden = true;
  }
}

function renderProductionReadiness(data) {
  const el = document.getElementById('production-readiness');
  if (!el) return;
  el.hidden = false;
  el.innerHTML = '';

  const top = document.createElement('div');
  top.className = 'pr-head';
  const label = document.createElement('strong');
  label.textContent = 'Game-production MCP readiness';
  const score = document.createElement('span');
  score.className = 'pr-score';
  score.dataset.ready = data.ready_for_large_scope ? '1' : '0';
  score.textContent = `${Number(data.score || 0)}/100`;
  top.appendChild(label);
  top.appendChild(score);
  el.appendChild(top);

  const desc = document.createElement('p');
  desc.textContent = data.ready_for_large_scope
    ? 'The connected tool surface covers the minimum evidence loop for large staged builds.'
    : 'Nex can still do useful work, but this is not yet a complete virtual game studio.';
  el.appendChild(desc);

  const checks = document.createElement('div');
  checks.className = 'pr-checks';
  const entries = [
    ...Object.entries(data.gates || {}),
    ...Object.entries(data.disciplines || {}),
  ];
  for (const [name, ok] of entries) {
    const chip = document.createElement('span');
    chip.dataset.ok = ok ? '1' : '0';
    chip.textContent = `${ok ? '✓' : '—'} ${name.replaceAll('_', ' ')}`;
    checks.appendChild(chip);
  }
  el.appendChild(checks);

  if ((data.blockers || []).length) {
    const blockers = document.createElement('div');
    blockers.className = 'pr-blockers';
    blockers.textContent = `Hard gaps: ${data.blockers.join(', ')}`;
    el.appendChild(blockers);
  }

  const profiles = data.engines || {};
  const engineGrid = document.createElement('div');
  engineGrid.className = 'engine-readiness-grid';
  for (const id of ['unreal_5_8', 'roblox_studio']) {
    const profile = profiles[id];
    if (!profile) continue;
    engineGrid.appendChild(engineReadinessCard(profile));
  }
  if (engineGrid.children.length) el.appendChild(engineGrid);
}

function engineReadinessCard(profile) {
  const card = document.createElement('section');
  card.className = 'engine-readiness-card';
  card.dataset.ready = profile.ready ? '1' : '0';

  const head = document.createElement('div');
  head.className = 'engine-readiness-head';
  const title = document.createElement('strong');
  title.textContent = profile.label || profile.id;
  const score = document.createElement('span');
  score.textContent = `${Number(profile.score || 0)}/100`;
  head.appendChild(title);
  head.appendChild(score);
  card.appendChild(head);

  const checks = document.createElement('div');
  checks.className = 'engine-readiness-checks';
  const labels = new Map((profile.requirements || [])
    .map((item) => [item.id, item.label]));
  for (const [id, ok] of Object.entries(profile.checks || {})) {
    const chip = document.createElement('span');
    chip.dataset.ok = ok ? '1' : '0';
    const label = labels.get(id) || id.replaceAll('_', ' ');
    chip.textContent = `${ok ? '✓' : '—'} ${label}`;
    checks.appendChild(chip);
  }
  card.appendChild(checks);

  const note = document.createElement('p');
  const missing = (profile.blockers || []).join(', ')
    || 'review optional disciplines';
  note.textContent = profile.ready
    ? 'Live MCP coverage supports the engine-specific production loop.'
    : `Missing hard coverage: ${missing}`;
  card.appendChild(note);
  return card;
}

/* ─── list ───────────────────────────────────────────────────── */

export function renderServerList() {
  const host = document.getElementById('server-list');
  if (!host) return;
  host.innerHTML = '';
  const { servers } = store.get();
  const list = (servers && servers.servers) || [];

  if (!list.length) {
    const empty = document.createElement('div');
    empty.className = 'servers-empty';
    empty.innerHTML = `
      <div class="se-icon">
        <svg viewBox="0 0 24 24" width="42" height="42" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round">
          <rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/>
          <circle cx="7" cy="7.5" r="0.6" fill="currentColor"/><circle cx="7" cy="16.5" r="0.6" fill="currentColor"/>
        </svg>
      </div>
      <h3>No capability servers</h3>
      <p>Nex has no built-in tools — it can only act through MCP servers you
      connect. Add one to give it abilities: a Blender bridge, a game
      server, a home automation hub, anything that speaks MCP.</p>`;
    host.appendChild(empty);
    return;
  }

  for (const s of list) {
    host.appendChild(serverCard(s));
  }
}

function serverCard(s) {
  const el = document.createElement('div');
  el.className = 'server-card';
  el.dataset.status = s.status;

  // head
  const head = document.createElement('div');
  head.className = 'server-head';
  const name = document.createElement('span');
  name.className = 'server-name';
  name.textContent = s.server;
  head.appendChild(name);

  const badge = document.createElement('span');
  badge.className = 'server-badge status-' + s.status;
  badge.textContent = s.status;
  head.appendChild(badge);

  const trust = document.createElement('span');
  trust.className = 'server-badge ' + (s.trusted ? 'trusted' : 'untrusted');
  trust.textContent = s.trusted ? 'trusted' : 'untrusted';
  trust.title = s.trusted
    ? 'Autonomous runs may call tools on this server (subject to policy)'
    : 'Tools on this server require approval / are refused in autonomous runs';
  head.appendChild(trust);

  const endpoint = document.createElement('span');
  endpoint.className = 'server-endpoint';
  endpoint.textContent = s.transport === 'stdio'
    ? `${s.command || ''} ${(s.args || []).join(' ')}`.trim()
    : (s.url || '');
  head.appendChild(endpoint);

  el.appendChild(head);

  // stats
  const stats = document.createElement('div');
  stats.className = 'server-stats';
  const addStat = (prefix, value, suffix = '') => {
    const span = document.createElement('span');
    span.append(document.createTextNode(prefix));
    const strong = document.createElement('b');
    strong.textContent = String(value);
    span.appendChild(strong);
    if (suffix) span.append(document.createTextNode(suffix));
    stats.appendChild(span);
  };
  addStat('', Number(s.tools_count || 0), ' tools');
  addStat('transport ', s.transport || 'unknown');
  if (s.latency_ms != null) addStat('', Math.round(Number(s.latency_ms) || 0), 'ms');
  if (s.protocol_version) addStat('MCP ', s.protocol_version);
  el.appendChild(stats);

  if (s.error || s.last_error) {
    const err = document.createElement('div');
    err.className = 'server-error';
    err.textContent = s.error || s.last_error;
    el.appendChild(err);
  }

  // actions
  const acts = document.createElement('div');
  acts.className = 'server-acts';

  const connected = s.status === 'connected';
  const connectBtn = mkBtn(connected ? 'Reconnect' : 'Connect',
    connected ? 'var(--fg-dim)' : 'var(--fg)', connected ? '' : 'var(--bg)');
  connectBtn.addEventListener('click', async () => {
    connectBtn.disabled = true;
    try {
      await api.serverAction(s.server, connected ? 'reconnect' : 'connect');
      refresh();
    } catch (err) {
      errorToast(err);
    }
  });
  acts.appendChild(connectBtn);

  if (connected) {
    const dis = mkBtn('Disconnect');
    dis.addEventListener('click', async () => {
      dis.disabled = true;
      try {
        await api.serverAction(s.server, 'disconnect');
        refresh();
      } catch (err) {
        errorToast(err);
      }
    });
    acts.appendChild(dis);
  }

  const trustBtn = mkBtn(s.trusted ? 'Mark untrusted' : 'Mark trusted');
  trustBtn.addEventListener('click', async () => {
    trustBtn.disabled = true;
    try {
      await api.serverAction(s.server, 'trust', { trusted: !s.trusted });
      refresh();
    } catch (err) {
      errorToast(err);
    }
  });
  acts.appendChild(trustBtn);

  const remove = mkBtn('Remove', 'var(--bad)');
  remove.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: `Remove “${s.server}”?`,
      body: 'The server is disconnected and its configuration is deleted. '
            + 'Nex immediately loses every tool it provided.',
      confirmLabel: 'Remove',
      danger: true,
    });
    if (!ok) return;
    try {
      await api.removeServer(s.server);
      refresh();
    } catch (err) {
      errorToast(err);
    }
  });
  acts.appendChild(remove);

  el.appendChild(acts);

  // tools (lazy loaded)
  const toolsWrap = document.createElement('div');
  toolsWrap.className = 'server-tools';
  const toggle = document.createElement('button');
  toggle.className = 'tools-toggle';
  const toggleIcon = document.createElement('span');
  toggleIcon.className = 'run-chev';
  toggleIcon.style.transform = 'rotate(-90deg)';
  toggleIcon.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12" '
    + 'fill="none" stroke="currentColor" stroke-width="2" '
    + 'stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>';
  toggle.appendChild(toggleIcon);
  toggle.append(document.createTextNode(` ${Number(s.tools_count || 0)} tools`));
  const toolsList = document.createElement('div');
  toolsList.className = 'tools-list';
  const inner = document.createElement('div');
  inner.className = 'tools-list-inner';
  toolsList.appendChild(inner);
  let loaded = false;
  toggle.addEventListener('click', async () => {
    toolsList.classList.toggle('open');
    const chev = toggle.querySelector('.run-chev');
    chev.style.transform = toolsList.classList.contains('open')
      ? '' : 'rotate(-90deg)';
    if (!loaded) {
      loaded = true;
      inner.innerHTML = '<div style="padding:10px;color:var(--fg-faint);font-size:12px">loading…</div>';
      try {
        const res = await api.serverTools(s.server);
        inner.innerHTML = '';
        for (const t of res.tools || []) {
          inner.appendChild(toolCard(t));
        }
        if (!(res.tools || []).length) {
          inner.innerHTML = '<div style="padding:10px;color:var(--fg-faint);font-size:12px">no tools exposed</div>';
        }
      } catch (err) {
        inner.innerHTML = '';
        const error = document.createElement('div');
        error.className = 'server-error';
        error.textContent = err.message;
        inner.appendChild(error);
      }
    }
  });
  toolsWrap.appendChild(toggle);
  toolsWrap.appendChild(toolsList);
  el.appendChild(toolsWrap);

  return el;

  function refresh() {
    api.servers().then((res) => store.set({ servers: res })).catch(() => {});
    refreshProductionReadiness();
  }
}

function toolCard(t) {
  const el = document.createElement('div');
  el.className = 'tool-card';
  const head = document.createElement('div');
  head.className = 't-head';
  const name = document.createElement('span');
  name.className = 't-name';
  name.textContent = t.name;
  head.appendChild(name);
  const cat = document.createElement('span');
  cat.className = 'cat-badge cat-' + (t.category || 'unknown');
  cat.textContent = t.category || 'unknown';
  cat.title = t.requires_confirmation
    ? 'Requires approval before autonomous use'
    : 'Classified safe for autonomous use by policy';
  head.appendChild(cat);
  if (t.requires_confirmation) {
    const lock = document.createElement('span');
    lock.className = 'cat-badge cat-unknown';
    lock.textContent = 'approval';
    head.appendChild(lock);
  }
  el.appendChild(head);
  if (t.description) {
    const d = document.createElement('div');
    d.className = 't-desc';
    d.textContent = t.description.slice(0, 400);
    el.appendChild(d);
  }
  if (t.schema && Object.keys(t.schema.properties || {}).length) {
    const pre = document.createElement('div');
    pre.className = 't-schema';
    pre.textContent = `Input\n${fmtSchema(t.schema)}`;
    el.appendChild(pre);
  }
  if (t.output_schema && Object.keys(t.output_schema.properties || {}).length) {
    const pre = document.createElement('div');
    pre.className = 't-schema';
    pre.textContent = `Returns (validated)\n${fmtSchema(t.output_schema)}`;
    el.appendChild(pre);
  }
  return el;
}

function fmtSchema(schema) {
  const lines = [];
  for (const [k, v] of Object.entries(schema.properties || {})) {
    const req = (schema.required || []).includes(k) ? '*' : '';
    lines.push(`${k}${req}: ${v.type || '?'}${v.description ? ' — ' + v.description.slice(0, 60) : ''}`);
  }
  return lines.join('\n') + '\n(* required)';
}

function mkBtn(label, color, bg) {
  const b = document.createElement('button');
  b.className = 'btn-ghost';
  b.textContent = label;
  b.style.fontSize = '12.5px';
  if (color) b.style.color = color;
  if (bg) b.style.background = bg;
  return b;
}

/* ─── add dialog ─────────────────────────────────────────────── */

function addServerDialog() {
  const overlay = document.getElementById('add-server-overlay');
  const body = document.getElementById('add-server-body');
  body.innerHTML = '';

  const form = document.createElement('form');
  form.innerHTML = `
    <div class="form-note" style="margin-bottom:14px">
      Nex has first-class readiness profiles for <strong>Unreal Engine 5.8</strong>
      and <strong>Roblox Studio</strong>. Start the editor's MCP server first,
      then enter its documented endpoint or command here. Nex never guesses one.
    </div>
    <div class="field">
      <label>Name <span class="hint">— lowercase, used as the tool namespace</span></label>
      <input name="name" type="text" placeholder="unreal_editor" required
             pattern="[a-z0-9][a-z0-9_-]{0,31}" autocomplete="off">
    </div>
    <div class="field">
      <label>Transport</label>
      <div class="seg" role="tablist">
        <button type="button" class="active" data-t="http">HTTP endpoint</button>
        <button type="button" data-t="stdio">Local command (stdio)</button>
      </div>
    </div>
    <div class="field" data-transport="http">
      <label>Server URL</label>
      <input name="url" type="url" placeholder="http://127.0.0.1:9876/mcp" autocomplete="off">
      <div class="form-note">Loopback endpoints are always allowed. Anything
      else must be confirmed below and can be locked down with
      <code>NEX_HTTP_ALLOW</code>.</div>
    </div>
    <div class="field" data-transport="stdio" hidden>
      <label>Command</label>
      <input name="command" type="text" placeholder="npx" autocomplete="off">
    </div>
    <div class="field" data-transport="stdio" hidden>
      <label>Arguments <span class="hint">— one per line or space separated</span></label>
      <input name="args" type="text" placeholder="-y mcp-server-blender" autocomplete="off">
      <div class="form-note">Nex will run this command on your computer and
      speak MCP over its stdio — the same model as any MCP host. In strict
      deployments set <code>NEX_STDIO_ALLOW</code> to pin the allowed
      commands.</div>
    </div>
    <div class="field">
      <div class="switch-row">
        <div class="sw-text">
          <div class="sw-title">Trust this server</div>
          <div class="sw-sub">Off by default. Trusted servers may serve
          autonomous tool calls (still policy-gated per tool). Leave this off
          until you have reviewed the server and its tools.</div>
        </div>
        <label class="switch">
          <input type="checkbox" name="trusted">
          <span class="knob"></span>
        </label>
      </div>
    </div>
    <div class="field" id="remote-confirm-row" hidden>
      <div class="switch-row">
        <div class="sw-text">
          <div class="sw-title" style="color:var(--warn)">This endpoint is not on this machine</div>
          <div class="sw-sub">Connecting sends your requests to a remote
          host. Confirm you trust it.</div>
        </div>
        <label class="switch">
          <input type="checkbox" name="confirm_remote">
          <span class="knob"></span>
        </label>
      </div>
    </div>
    <div class="form-err" hidden></div>
    <button class="btn-primary btn-block" type="submit">Connect server</button>
  `;

  let transport = 'http';
  form.querySelectorAll('.seg button').forEach((b) => {
    b.addEventListener('click', () => {
      form.querySelectorAll('.seg button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      transport = b.dataset.t;
      form.querySelector('[data-transport="http"]').hidden = transport !== 'http';
      form.querySelectorAll('[data-transport="stdio"]').forEach((el) => {
        el.hidden = transport !== 'stdio';
      });
    });
  });

  const urlInput = form.querySelector('input[name="url"]');
  urlInput.addEventListener('input', () => {
    const row = form.querySelector('#remote-confirm-row');
    try {
      const u = new URL(urlInput.value);
      const host = u.hostname;
      row.hidden = !host || host === '127.0.0.1' || host === 'localhost'
        || host === '::1';
    } catch {
      row.hidden = true;
    }
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = form.querySelector('.form-err');
    errEl.hidden = true;
    const fd = new FormData(form);
    const entry = {
      name: String(fd.get('name') || '').trim(),
      transport,
      trusted: !!fd.get('trusted'),
      confirm_remote: !!fd.get('confirm_remote'),
    };
    if (transport === 'stdio') {
      entry.command = String(fd.get('command') || '').trim();
      entry.args = String(fd.get('args') || '').trim().split(/\s+/).filter(Boolean);
    } else {
      entry.url = String(fd.get('url') || '').trim();
    }
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Connecting…';
    try {
      await api.addServer(entry);
      overlay.hidden = true;
      toast('success', `Server “${entry.name}” added`,
            'Discovering its tools now.');
      const res = await api.servers();
      store.set({ servers: res });
      refreshProductionReadiness();
    } catch (err) {
      errEl.textContent = err.message + (err.detail ? ' — ' + err.detail : '');
      errEl.hidden = false;
      submitBtn.disabled = false;
      submitBtn.textContent = 'Connect server';
    }
  });

  body.appendChild(form);
  overlay.hidden = false;
  form.querySelector('input[name="name"]').focus();

  overlay.querySelector('.modal-close').onclick = () => {
    overlay.hidden = true;
  };
  overlay.onclick = (e) => {
    if (e.target === overlay) overlay.hidden = true;
  };
}
