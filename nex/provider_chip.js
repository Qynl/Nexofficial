/*
 * NEX — provider chip (pure, dependency-free).
 *
 * The one question this answers at a glance: WHO IS BUILDING RIGHT NOW —
 * NVIDIA NIM, GPT, or the local model — and on which model?
 *
 * It renders from the server's provider snapshot:
 *
 *   {
 *     roles: {
 *       planner: {provider:"gpt",   model:"gpt-5.1",              active:"gpt",   serving:"gpt",  fallback:false},
 *       builder: {provider:"nim",   model:"nvidia/nemotron-3…",   active:"nim",   serving:"gpt",  fallback:true}
 *     },
 *     providers: { nim: {status:"rate_limited", rpm:40, requests_in_window:40,
 *                        budget_left:0, cooldown_s:12.4}, … }
 *   }
 *
 * Everything here is a pure function so the node test harness can check the
 * labels without a browser. `render()` is the only DOM-touching part.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.NexProviderChip = api;
})(typeof window !== 'undefined' ? window : null, function () {
  const VENDOR_LABEL = {
    nim: 'NIM', nvidia: 'NIM', gpt: 'GPT', openai: 'GPT', local: 'local',
  };

  function providerLabel(name) {
    const key = String(name || '').toLowerCase();
    if (VENDOR_LABEL[key]) return VENDOR_LABEL[key];
    if (!key) return '—';
    return key.charAt(0).toUpperCase() + key.slice(1);
  }

  /* "nvidia/nemotron-3-super-120b-a12b" -> "nemotron-3-super-120b"
   *
   * Vendor prefix goes, and the usual size/quantisation suffixes
   * ("-a12b", "-instruct", "-it") are dropped — what is left is the part a
   * human actually recognises. Anything still too long is cut at a hyphen
   * boundary instead of mid-word. */
  function shortModel(model, max) {
    let m = String(model || '').trim();
    if (!m) return '';
    const slash = m.lastIndexOf('/');
    if (slash >= 0) m = m.slice(slash + 1);
    m = m.replace(/:latest$/, '');
    m = m.replace(/-a\d+b(-instruct)?$/i, '')
         .replace(/-instruct(-\d+)?$/i, '')
         .replace(/:free$/i, '');
    const cap = max || 26;
    if (m.length > cap) {
      const cut = m.slice(0, cap);
      const dash = cut.lastIndexOf('-');
      m = (dash > 8 ? cut.slice(0, dash) : cut) + '…';
    }
    return m;
  }

  function role(status, name) {
    const roles = (status && status.roles) || {};
    const r = roles[name] || {};
    return {
      provider: r.provider || '',
      model: r.model || '',
      active: r.active || '',
      serving: r.serving || '',
      active_model: r.active_model || '',
      serving_model: r.serving_model || '',
      fallback: !!r.fallback,
    };
  }

  /* Which provider is actually doing the work for a role. */
  function effective(roleData) {
    return roleData.serving || roleData.active || roleData.provider;
  }

  function providerState(status, name) {
    const all = (status && status.providers) || {};
    return all[name] || null;
  }

  /* Offline = nothing in either chain can serve a call. */
  function offline(status) {
    const p = role(status, 'planner');
    const b = role(status, 'builder');
    return !effective(p) && !effective(b);
  }

  /* The chip's colour tone: which "hands" are working. */
  function tone(status) {
    if (offline(status)) return 'offline';
    const b = role(status, 'builder');
    if (b.fallback) return 'fallback';
    const name = String(effective(b) || effective(role(status, 'planner'))).toLowerCase();
    if (name.indexOf('nim') === 0 || name.indexOf('nvidia') === 0) return 'nim';
    if (name.indexOf('gpt') === 0 || name.indexOf('openai') === 0) return 'gpt';
    return 'local';
  }

  /* A rejected key is a config problem, not a hiccup: it gets its own mark
   * so it cannot be mistaken for a rate-limit failover. */
  function primaryMark(status, name) {
    const st = providerState(status, name);
    if (!st) return ' ⛔';
    if (st.key_mismatch) return ' ⚠key';
    if (st.last_error_kind === 'auth_error') return ' ⚠key';
    return ' ⛔';
  }

  /* Main line: "NIM · nemotron-3-super" (+ "→ GPT" while failing over). */
  function chipLabel(status) {
    if (offline(status)) return 'no model';
    const b = role(status, 'builder');
    const active = effective(b);
    const model = (active === b.serving ? (b.serving_model || b.model)
                                        : (b.active_model || b.model)) || '';
    let out = providerLabel(active);
    const short = shortModel(model);
    if (short) out += ' · ' + short;
    if (b.fallback && b.provider !== active) {
      out = providerLabel(b.provider) + primaryMark(status, b.provider)
        + ' → ' + providerLabel(active);
      if (short) out += ' · ' + short;
    }
    return out;
  }

  /* Second line: the planner (small text under/next to the chip). */
  function plannerLine(status) {
    const p = role(status, 'planner');
    const active = effective(p);
    if (!active) return '';
    const short = shortModel(p.model);
    return 'planner ' + providerLabel(active) + (short ? ' · ' + short : '');
  }

  /* Quota readout for a provider, e.g. "28/40 RPM" or "" when unlimited. */
  function quotaLabel(status, name) {
    const st = providerState(status, name);
    if (!st || !st.rpm) return '';
    const used = (st.requests_in_window == null) ? 0 : st.requests_in_window;
    return used + '/' + st.rpm + ' RPM';
  }

  function builderQuota(status) {
    const b = role(status, 'builder');
    return quotaLabel(status, effective(b) || b.provider);
  }

  /* One line summarising a provider's live state. */
  function providerBits(status, name) {
    const st = providerState(status, name);
    if (!st) return '';
    const bits = ['state=' + (st.status || '?')];
    if (st.rpm) {
      bits.push(((st.requests_in_window == null) ? 0 : st.requests_in_window)
        + '/' + st.rpm + ' RPM');
    }
    if (st.cooldown_s) bits.push('cooldown ' + Math.ceil(st.cooldown_s) + 's');
    if (st.auth_blocked_s) {
      bits.push('key blocked ' + Math.ceil(st.auth_blocked_s) + 's');
    }
    if (st.last_error_kind) bits.push('last: ' + st.last_error_kind);
    return bits.join(' · ');
  }

  /* Tooltip: the whole truth in a handful of short lines. */
  function chipDetail(status) {
    if (!status) return 'NEX provider status unavailable';
    const lines = [];
    const p = role(status, 'planner');
    const b = role(status, 'builder');
    const activeName = effective(b) || b.provider;
    const builderModel = activeName === b.serving
      ? (b.serving_model || b.model) : (b.active_model || b.model);
    lines.push('🧠 planner: ' + providerLabel(effective(p)) +
               (p.model ? ' · ' + shortModel(p.model, 40) : ''));
    lines.push('🔨 builder: ' + providerLabel(activeName) +
               (builderModel ? ' · ' + shortModel(builderModel, 40) : ''));
    const primaryState = providerState(status, b.provider);
    if (primaryState && primaryState.key_mismatch) {
      lines.push('⚠ ' + providerLabel(b.provider) + ': the key belongs to '
        + (primaryState.key_host || '?') + ', the endpoint is another host — '
        + 'the key is NOT sent. Re-enter it to confirm the new endpoint.');
    }
    if (primaryState && primaryState.last_error_kind === 'auth_error') {
      lines.push('⚠ ' + providerLabel(b.provider) + ': API key rejected'
        + (primaryState.auth_blocked_s
           ? ' (blocked ' + Math.ceil(primaryState.auth_blocked_s) + 's)' : '')
        + ' — fix it in the settings');
    }
    if (b.fallback && b.provider && b.provider !== activeName) {
      lines.push('⚠ ' + providerLabel(b.provider) +
                 ' is unavailable — ' + providerLabel(activeName) +
                 ' builds instead (same plan)');
      const primary = providerBits(status, b.provider);
      if (primary) lines.push(providerLabel(b.provider) + ': ' + primary);
    }
    const active = providerBits(status, activeName);
    if (active) lines.push(providerLabel(activeName) + ': ' + active);
    lines.push('builder acts through MCP tools only');
    return lines.join('\n');
  }

  /* A key problem (rejected or bound to another host) is a config fault, not
   * a capacity problem — the chip marks it separately from a plain failover. */
  function keyProblem(status) {
    if (!status) return '';
    const b = role(status, 'builder');
    const names = [b.provider, effective(b)].filter(Boolean);
    for (const n of names) {
      const st = providerState(status, n);
      if (!st) continue;
      if (st.key_mismatch) return 'mismatch';
      if (st.last_error_kind === 'auth_error') return 'rejected';
    }
    return '';
  }

  /* Which provider just took over — used for the short "switched" flash. */
  function switchNote(prev, next) {
    const a = role(prev || {}, 'builder');
    const b = role(next || {}, 'builder');
    const from = providerLabel(effective(a));
    const to = providerLabel(effective(b));
    if (!from || !to || from === to) return '';
    return from + ' → ' + to;
  }

  /* ---- DOM (thin) ------------------------------------------------------ */
  function render(el, status) {
    if (!el) return;
    el.textContent = chipLabel(status);
    el.title = chipDetail(status);
    el.dataset.tone = tone(status);
    const key = keyProblem(status);
    if (key) {
      el.dataset.warn = key;
      el.title = '⚠ key ' + (key === 'rejected' ? 'rejected' : 'bound elsewhere')
        + '\n' + el.title;
    } else {
      delete el.dataset.warn;
    }
    const plan = el.parentNode &&
      el.parentNode.querySelector('[data-role="provider-planner"]');
    if (plan) {
      const line = plannerLine(status);
      plan.textContent = line;
      plan.hidden = !line;
    }
  }

  return {
    providerLabel: providerLabel,
    shortModel: shortModel,
    role: role,
    effective: effective,
    tone: tone,
    chipLabel: chipLabel,
    plannerLine: plannerLine,
    quotaLabel: quotaLabel,
    builderQuota: builderQuota,
    providerBits: providerBits,
    primaryMark: primaryMark,
    keyProblem: keyProblem,
    chipDetail: chipDetail,
    switchNote: switchNote,
    render: render,
  };
});
