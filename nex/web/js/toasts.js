/* Toasts — the error/notice design layer.
   Kinds: info | success | warn | error. Max 4 stacked, auto-dismiss. */

const ICONS = {
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M12 11v6"/>',
  success: '<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.4 2.4 4.6-5.3"/>',
  warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
};

const stack = () => document.getElementById('toasts');

export function toast(kind, title, sub = '', opts = {}) {
  const host = stack();
  if (!host) return;
  while (host.children.length >= 4) {
    host.firstChild.remove();
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.dataset.kind = kind;
  el.setAttribute('role', 'status');

  const dot = document.createElement('span');
  dot.className = 't-dot';
  const body = document.createElement('div');
  body.className = 't-body';
  const t = document.createElement('div');
  t.className = 't-title';
  t.textContent = title;
  body.appendChild(t);
  if (sub) {
    const s = document.createElement('div');
    s.className = 't-sub';
    s.textContent = sub;
    body.appendChild(s);
  }
  const close = document.createElement('button');
  close.className = 't-close';
  close.setAttribute('aria-label', 'Dismiss');
  close.innerHTML =
    '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  el.appendChild(dot);
  el.appendChild(body);
  el.appendChild(close);

  const ttl = opts.ttl != null ? opts.ttl
    : (kind === 'error' ? 9000 : 5200);
  let timer = null;
  const dismiss = () => {
    clearTimeout(timer);
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  };
  close.addEventListener('click', dismiss);
  if (ttl > 0) timer = setTimeout(dismiss, ttl);
  if (opts.onClick) {
    body.style.cursor = 'pointer';
    body.addEventListener('click', () => {
      opts.onClick();
      dismiss();
    });
  }
  host.appendChild(el);
  return dismiss;
}

/* Friendly names + suggested next actions for error codes. */
const CODE_LABELS = {
  user_error: 'Request problem',
  model_error: 'Model problem',
  mcp_error: 'Capability problem',
  network_error: 'Network problem',
  config_error: 'Configuration problem',
  internal_error: 'Nex problem',
  timeout: 'Timed out',
  auth: 'Authentication',
};

export function errorToast(err, opts = {}) {
  const label = CODE_LABELS[err.code] || 'Something went wrong';
  return toast('error', label, err.message || '', {
    sub: err.detail || '',
    ...opts,
  });
}
