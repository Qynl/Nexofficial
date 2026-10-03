/* Settings — model providers, voice, data, about. */

import { store } from './state.js';
import { api } from './api.js';
import { toast, errorToast } from './toasts.js';
import { voice } from './voice.js';

export function initSettings() {
  const overlay = document.getElementById('settings-overlay');
  document.getElementById('btn-settings').addEventListener('click', async () => {
    overlay.hidden = false;
    renderModelPanel();
    renderVoicePanel();
    renderDataPanel();
    renderAboutPanel();
  });
  overlay.querySelector('.modal-close').onclick = () => (overlay.hidden = true);
  overlay.onclick = (e) => {
    if (e.target === overlay) overlay.hidden = true;
  };
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') overlay.hidden = true;
  });
  document.querySelectorAll('.settings-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.settings-tab')
        .forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.settings-panel')
        .forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      document.querySelector(`.settings-panel[data-panel="${tab.dataset.tab}"]`)
        .classList.add('active');
    });
  });
}

/* ─── Model panel ────────────────────────────────────────────── */

async function renderModelPanel() {
  const panel = document.querySelector('.settings-panel[data-panel="model"]');
  panel.innerHTML = '<div style="color:var(--fg-faint);font-size:13px">loading…</div>';
  let view;
  try {
    view = await api.providers();
  } catch (err) {
    panel.innerHTML = '';
    const failure = document.createElement('div');
    failure.className = 'form-err';
    failure.textContent = err.message;
    panel.appendChild(failure);
    return;
  }
  panel.innerHTML = '';

  const intro = document.createElement('p');
  intro.style.cssText =
    'color:var(--fg-faint);font-size:12.5px;margin:0 0 14px;';
  intro.textContent = 'Two roles, each with its own provider chain: '
    + 'Chat answers you; Agent plans, evaluates and repairs. If a provider '
    + 'fails or is rate-limited, the next one in the chain takes over '
    + 'without losing the work.';
  panel.appendChild(intro);
  panel.appendChild(roleRouting(view));

  for (const p of view.providers || []) {
    panel.appendChild(providerCard(p, view));
  }
}

function roleRouting(view) {
  const box = document.createElement('div');
  box.className = 'role-routing';
  const title = document.createElement('div');
  title.className = 'role-routing-title';
  title.textContent = 'Model flight plan';
  box.appendChild(title);

  const providerModels = new Map(
    (view.providers || []).map((p) => [p.name, p.model || '']));
  const names = [...providerModels.keys()];
  for (const role of ['chat', 'agent']) {
    const config = (view.roles || {})[role] || {};
    const live = config.serving || config.active || config.provider || '';
    const row = document.createElement('div');
    row.className = 'role-route';

    const identity = document.createElement('div');
    const roleName = document.createElement('strong');
    roleName.textContent = role === 'chat' ? 'Chat brain' : 'Agent brain';
    const status = document.createElement('small');
    status.textContent = live
      ? `live: ${live} · ${config.serving_model || config.active_model || config.model || 'default model'}`
      : 'no provider currently available';
    identity.appendChild(roleName);
    identity.appendChild(status);

    const primary = document.createElement('select');
    primary.dataset.role = role;
    primary.dataset.f = 'provider';
    for (const name of names) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      option.selected = name === config.provider;
      primary.appendChild(option);
    }

    const model = document.createElement('input');
    model.type = 'text';
    model.dataset.role = role;
    model.dataset.f = 'model';
    model.value = config.model || '';
    model.placeholder = 'primary model override';
    primary.addEventListener('change', () => {
      model.value = providerModels.get(primary.value) || '';
    });

    const fallbacks = document.createElement('input');
    fallbacks.type = 'text';
    fallbacks.dataset.role = role;
    fallbacks.dataset.f = 'fallbacks';
    const chain = (view.chains || {})[role] || [];
    fallbacks.value = chain.filter((name) => name !== config.provider).join(', ');
    fallbacks.placeholder = 'fallbacks, in order';

    row.appendChild(identity);
    row.appendChild(primary);
    row.appendChild(model);
    row.appendChild(fallbacks);
    box.appendChild(row);
  }

  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'btn-approve role-route-save';
  save.textContent = 'Save routing';
  save.addEventListener('click', async () => {
    const roles = {};
    for (const role of ['chat', 'agent']) {
      const get = (field) => box.querySelector(
        `[data-role="${role}"][data-f="${field}"]`);
      roles[role] = {
        provider: get('provider').value,
        model: get('model').value.trim(),
        fallbacks: get('fallbacks').value.split(',')
          .map((name) => name.trim()).filter(Boolean),
      };
    }
    try {
      const res = await api.saveProviders({ roles });
      store.set({ provider: res.provider });
      toast('success', 'Saved model routing');
      renderModelPanel();
    } catch (err) {
      errorToast(err);
    }
  });
  box.appendChild(save);
  return box;
}

function providerCard(p, view) {
  const el = document.createElement('div');
  el.className = 'prov-card';

  const state = p.state || {};
  const tone = !p.enabled ? 'var(--fg-faint)'
    : !p.configured ? 'var(--warn)'
    : state.status === 'rate_limited' || state.status === 'cooling'
      ? 'var(--warn)'
    : state.status === 'error' ? 'var(--bad)'
    : 'var(--good)';

  const head = document.createElement('div');
  head.className = 'prov-head';
  const dot = document.createElement('span');
  dot.className = 'prov-dot';
  dot.style.background = tone;
  const name = document.createElement('span');
  name.className = 'prov-name';
  name.textContent = p.name;
  const kind = document.createElement('span');
  kind.className = 'prov-kind';
  kind.textContent = p.kind;
  head.appendChild(dot);
  head.appendChild(name);
  head.appendChild(kind);
  if (p.key_mismatch) {
    const mm = document.createElement('span');
    mm.className = 'cat-badge cat-unknown';
    mm.textContent = 'key bound to another host';
    head.appendChild(mm);
  }
  el.appendChild(head);

  // roles this provider serves
  const roles = document.createElement('div');
  roles.className = 'prov-roles';
  for (const [role, chain] of Object.entries(view.chains || {})) {
    const idx = chain.indexOf(p.name);
    if (idx >= 0) {
      const tag = document.createElement('span');
      tag.className = 'role-tag';
      tag.textContent = idx === 0 ? `${role} · primary` : `${role} · fallback ${idx}`;
      roles.appendChild(tag);
    }
  }
  el.appendChild(roles);

  const telemetry = document.createElement('div');
  telemetry.className = 'prov-telemetry';
  const facts = [
    state.last_model ? `model ${state.last_model}` : null,
    state.last_purpose ? `job ${state.last_purpose}` : null,
    state.total_calls != null ? `${state.total_ok || 0}/${state.total_calls} calls ok` : null,
    (state.total_prompt_tokens || state.total_completion_tokens)
      ? `${(state.total_prompt_tokens || 0) + (state.total_completion_tokens || 0)} tokens`
      : null,
    state.budget_left != null ? `${state.budget_left} RPM left` : 'unmetered',
  ].filter(Boolean);
  telemetry.textContent = facts.join(' · ');
  el.appendChild(telemetry);
  if (p.note) {
    const note = document.createElement('div');
    note.className = 'prov-note';
    note.textContent = p.note;
    el.appendChild(note);
  }

  // expandable form
  const form = document.createElement('div');
  form.className = 'prov-form';
  form.hidden = true;
  form.innerHTML = `
    <div class="form-row">
      <div class="field"><label>Base URL</label>
        <input data-f="base_url" type="url"></div>
      <div class="field"><label>Default model</label>
        <div style="display:flex;gap:6px">
          <input data-f="model" type="text" style="flex:1">
          <button type="button" class="btn-ghost" data-act="models" style="flex:none;font-size:12px">list</button>
        </div>
        <div class="form-note" data-slot="models" hidden></div>
      </div>
    </div>
    <div class="form-row">
      <div class="field"><label>API key <span class="hint">— stored server-side (0600), bound to this host</span></label>
        <input data-f="api_key" type="password" autocomplete="off"></div>
      <div class="field"><label>Rate limit <span class="hint">requests/min</span></label>
        <input data-f="rpm" type="number" min="0"></div>
    </div>
    <div class="switch-row">
      <div class="sw-text"><div class="sw-title">Structured agent output</div>
        <div class="sw-sub">Request JSON mode for plans, evaluations, diagnoses and batches.</div></div>
      <label class="switch"><input data-f="structured_outputs" type="checkbox"><span class="knob"></span></label>
    </div>
    <div class="switch-row">
      <div class="sw-text"><div class="sw-title">Enabled</div>
        <div class="sw-sub">Disabled providers are skipped in every chain.</div></div>
      <label class="switch"><input data-f="enabled" type="checkbox"><span class="knob"></span></label>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px;align-items:center">
      <button type="button" class="btn-approve" data-act="save" style="padding:7px 14px">Save</button>
      <button type="button" class="btn-ghost" data-act="test" style="font-size:12.5px">Test connection</button>
      <span data-slot="test" style="font-size:12px;color:var(--fg-faint)"></span>
    </div>`;
  form.querySelector('[data-f="base_url"]').value = p.base_url || '';
  form.querySelector('[data-f="model"]').value = p.model || '';
  form.querySelector('[data-f="api_key"]').placeholder = p.key_masked || 'not set';
  form.querySelector('[data-f="rpm"]').value = p.rpm || 0;
  form.querySelector('[data-f="structured_outputs"]').checked = Boolean(p.structured_outputs);
  form.querySelector('[data-f="enabled"]').checked = Boolean(p.enabled);
  el.appendChild(form);

  head.style.cursor = 'pointer';
  head.addEventListener('click', () => (form.hidden = !form.hidden));

  form.querySelector('[data-act="models"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    const slot = form.querySelector('[data-slot="models"]');
    slot.hidden = false;
    slot.textContent = 'fetching…';
    const curated = (view.catalog || [])
      .filter((item) => item.provider === p.name);
    try {
      const res = await api.providerModels(p.name);
      const live = res.models || [];
      const notes = new Map(curated.map((item) => [item.id, item.note || '']));
      const models = [...new Set([...live, ...curated.map((item) => item.id)])];
      slot.innerHTML = '';
      if (!models.length) {
        slot.textContent = 'no models reported';
        return;
      }
      for (const m of models.slice(0, 80)) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn-ghost';
        b.textContent = m + (live.includes(m) ? ' · live' : ' · curated');
        b.title = notes.get(m) || '';
        b.style.cssText = 'display:inline-block;margin:3px 4px 0 0;padding:4px 9px;font-size:11.5px;font-family:var(--font-mono)';
        b.addEventListener('click', () => {
          form.querySelector('[data-f="model"]').value = m;
        });
        slot.appendChild(b);
      }
    } catch (err) {
      slot.innerHTML = '';
      if (!curated.length) {
        slot.textContent = err.message;
        return;
      }
      const warning = document.createElement('span');
      warning.textContent = 'live list unavailable; showing curated starting points: ';
      slot.appendChild(warning);
      for (const item of curated) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn-ghost';
        b.textContent = item.id;
        b.title = item.note || '';
        b.addEventListener('click', () => {
          form.querySelector('[data-f="model"]').value = item.id;
        });
        slot.appendChild(b);
      }
    }
  });

  form.querySelector('[data-act="test"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    const slot = form.querySelector('[data-slot="test"]');
    slot.textContent = 'testing…';
    try {
      const res = await api.testProvider(p.name);
      slot.textContent = res.ok
        ? `ok — ${res.latency_ms}ms, ${res.models_count} models`
        : (res.error || 'failed');
      slot.style.color = res.ok ? 'var(--good)' : 'var(--bad)';
    } catch (err) {
      slot.textContent = err.message;
      slot.style.color = 'var(--bad)';
    }
  });

  form.querySelector('[data-act="save"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    const get = (f) => form.querySelector(`[data-f="${f}"]`);
    const patch = {
      providers: {
        [p.name]: {
          base_url: get('base_url').value.trim() || null,
          model: get('model').value.trim() || null,
          rpm: parseInt(get('rpm').value, 10) || 0,
          structured_outputs: get('structured_outputs').checked,
          enabled: get('enabled').checked,
        },
      },
    };
    const key = get('api_key').value.trim();
    if (key) patch.providers[p.name].api_key = key;
    try {
      const res = await api.saveProviders(patch);
      store.set({ provider: res.provider });
      toast('success', `Saved ${p.name}`);
      renderModelPanel();
    } catch (err) {
      errorToast(err);
    }
  });

  return el;
}

/* ─── Voice panel ─────────────────────────────────────────────── */

function renderVoicePanel() {
  const panel = document.querySelector('.settings-panel[data-panel="voice"]');
  const vs = voice.state;
  panel.innerHTML = '';

  if (!vs.ttsSupported && !vs.sttSupported) {
    panel.innerHTML = '<div class="form-note">This browser does not expose '
      + 'speech APIs. Nex works fully without voice.</div>';
    return;
  }

  if (vs.ttsSupported) {
    const row = document.createElement('div');
    row.className = 'switch-row';
    row.innerHTML = `
      <div class="sw-text">
        <div class="sw-title">Read replies aloud</div>
        <div class="sw-sub">New replies are spoken automatically. Any message
        also has a “listen” action. Speaking stops the moment you interact.</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="set-tts" ${vs.ttsEnabled ? 'checked' : ''}>
        <span class="knob"></span>
      </label>`;
    panel.appendChild(row);
    row.querySelector('#set-tts').addEventListener('change', (e) => {
      voice.setEnabled(e.target.checked);
    });

    const voiceField = document.createElement('div');
    voiceField.className = 'field';
    voiceField.innerHTML = `
      <label>Voice</label>
      <select id="set-voice" style="width:100%"></select>`;
    panel.appendChild(voiceField);
    const sel = voiceField.querySelector('#set-voice');
    const fill = () => {
      const voices = voice.voices();
      sel.innerHTML = '<option value="">browser default</option>';
      for (const v of voices) {
        const o = document.createElement('option');
        o.value = v.voiceURI;
        o.textContent = `${v.name} (${v.lang})`;
        if (v.voiceURI === vs.voiceURI) o.selected = true;
        sel.appendChild(o);
      }
    };
    fill();
    sel.addEventListener('change', () => voice.setVoice(sel.value));
    // voices load async in some browsers
    setTimeout(fill, 600);

    const rateField = document.createElement('div');
    rateField.className = 'field';
    rateField.innerHTML = `
      <label>Rate <span class="hint">— ${vs.rate.toFixed(2)}×</span></label>
      <input id="set-rate" type="range" min="0.5" max="1.6" step="0.05"
             value="${vs.rate}" style="width:100%">`;
    panel.appendChild(rateField);
    rateField.querySelector('#set-rate').addEventListener('input', (e) => {
      voice.setRate(parseFloat(e.target.value));
      rateField.querySelector('.hint').textContent =
        '— ' + vs.rate.toFixed(2) + '×';
    });
  }

  if (vs.sttSupported) {
    const row = document.createElement('div');
    row.className = 'switch-row';
    row.innerHTML = `
      <div class="sw-text">
        <div class="sw-title">Send dictated text immediately</div>
        <div class="sw-sub">When a dictated phrase is recognized, send it
        without waiting for you to press Enter.</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="set-autosend" ${vs.autoSend ? 'checked' : ''}>
        <span class="knob"></span>
      </label>`;
    panel.appendChild(row);
    row.querySelector('#set-autosend').addEventListener('change', (e) => {
      voice.setAutoSend(e.target.checked);
    });
  }
}

/* ─── Data panel ─────────────────────────────────────────────── */

async function renderDataPanel() {
  const panel = document.querySelector('.settings-panel[data-panel="data"]');
  panel.innerHTML = `
    <div class="switch-row">
      <div class="sw-text">
        <div class="sw-title">Reduce motion</div>
        <div class="sw-sub">Cuts animations to near-zero everywhere (also
        honored automatically from your OS setting).</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="set-motion" ${localStorage.getItem('nex.motion') === 'off' ? 'checked' : ''}>
        <span class="knob"></span>
      </label>
    </div>
    <div class="field" style="margin-top:14px">
      <label>Recent capability activity</label>
      <div id="audit-list" style="font-family:var(--font-mono);font-size:11px;color:var(--fg-faint);max-height:220px;overflow-y:auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);padding:10px">loading…</div>
    </div>
    <div style="display:flex;gap:8px;margin-top:6px">
      <button class="btn-ghost" id="btn-export" style="font-size:12.5px">Export conversations</button>
    </div>`;
  panel.querySelector('#set-motion').addEventListener('change', (e) => {
    localStorage.setItem('nex.motion', e.target.checked ? 'off' : 'on');
    applyMotionPreference();
  });
  panel.querySelector('#btn-export').addEventListener('click', async () => {
    try {
      const st = await api.state();
      const blob = new Blob([JSON.stringify(
        st.conversations || [], null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'nex-conversations.json';
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) {
      errorToast(err);
    }
  });
  try {
    const audit = await api.audit();
    const host = panel.querySelector('#audit-list');
    host.innerHTML = '';
    const entries = audit.entries || [];
    if (!entries.length) {
      host.textContent = 'no external actions yet';
    }
    for (const e of entries.slice().reverse()) {
      const line = document.createElement('div');
      const t = new Date((e.ts || 0) * 1000).toLocaleTimeString();
      line.textContent = `${t} ${e.kind} ${e.server || ''}.${e.tool || ''}`
        + (e.ok ? '' : ` — ${(e.detail || '').slice(0, 80)}`);
      line.style.marginBottom = '2px';
      host.appendChild(line);
    }
  } catch { /* ignore */ }
}

export function applyMotionPreference() {
  const off = localStorage.getItem('nex.motion') === 'off';
  document.documentElement.style.setProperty('--d1', off ? '1ms' : '');
  document.documentElement.style.setProperty('--d2', off ? '1ms' : '');
  document.documentElement.style.setProperty('--d3', off ? '1ms' : '');
  document.documentElement.style.setProperty('--d4', off ? '1ms' : '');
}

/* ─── About panel ────────────────────────────────────────────── */

function renderAboutPanel() {
  const panel = document.querySelector('.settings-panel[data-panel="about"]');
  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
      <div style="width:44px;height:44px;border-radius:13px;background:var(--fg);color:var(--bg);display:flex;align-items:center;justify-content:center">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
          <rect x="4" y="6" width="6.5" height="12" rx="2.5"/>
          <rect x="13.5" y="6" width="6.5" height="12" rx="2.5"/>
        </svg>
      </div>
      <div>
        <div style="font-weight:700;font-size:17px">Nex 2.0</div>
        <div style="color:var(--fg-faint);font-size:12.5px">a personal agent
        platform · stdlib Python, zero build steps</div>
      </div>
    </div>
    <p style="color:var(--fg-dim);font-size:13px;line-height:1.6">
      Nex acts on the world only through the MCP servers you connect —
      it has no built-in filesystem, shell, or network tools. Every tool
      call is policy-classified and audited; risky tools need your
      approval.</p>
    <p style="color:var(--fg-dim);font-size:13px;line-height:1.6">
      Keyboard: <kbd>Enter</kbd> send · <kbd>Shift+Enter</kbd> newline ·
      <kbd>Esc</kbd> stop dictation.</p>`;
}
