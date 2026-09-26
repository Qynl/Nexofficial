/* REST + SSE client. Every mutating request carries the X-Nex header
   (CSRF defense — cross-site pages cannot set custom headers). */

const HEADERS = {
  'Content-Type': 'application/json',
  'X-Nex': '1',
};

async function request(method, path, body) {
  const opts = { method, headers: { ...HEADERS } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    throw { code: 'network_error', message: 'Nex is unreachable.',
            detail: String(err) };
  }
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent('nex:unauthorized'));
    throw { code: 'auth', message: 'Authentication required.' };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok) {
    const err = data && data.error ? data.error : {};
    throw {
      code: err.code || 'internal_error',
      message: err.message || ('Request failed (' + res.status + ')'),
      detail: err.detail || '',
    };
  }
  return data;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body || {}),
  patch: (path, body) => request('PATCH', path, body || {}),
  del: (path) => request('DELETE', path),

  // -- state / conversations
  state: () => request('GET', '/api/state'),
  conversations: () => request('GET', '/api/conversations'),
  newConversation: () => request('POST', '/api/conversations'),
  messages: (cid) => request('GET', `/api/conversations/${cid}/messages`),
  renameConversation: (cid, title) =>
    request('PATCH', `/api/conversations/${cid}`, { title }),
  deleteConversation: (cid) => request('DELETE', `/api/conversations/${cid}`),
  search: (q) => request('GET',
    `/api/conversations?` + new URLSearchParams({ q })),

  // -- chat
  send: (cid, message) => request('POST', '/api/chat',
    { conversation_id: cid, message }),
  regenerate: (cid) => request('POST',
    `/api/conversations/${cid}/regenerate`),

  // -- runs
  resolveRun: (runId, approved, always) =>
    request('POST', `/api/runs/${runId}/resolve`, { approved, always }),
  cancelRun: (runId) => request('POST', `/api/runs/${runId}/cancel`),

  // -- servers
  servers: () => request('GET', '/api/servers'),
  serverTools: (name) => request('GET', `/api/servers/${name}/tools`),
  addServer: (entry) => request('POST', '/api/servers', entry),
  removeServer: (name) => request('DELETE', `/api/servers/${name}`),
  serverAction: (name, action, body) =>
    request('POST', `/api/servers/${name}/${action}`, body),

  // -- providers
  providers: () => request('GET', '/api/providers'),
  saveProviders: (patch) => request('POST', '/api/providers', patch),
  providerModels: (name) =>
    request('POST', '/api/providers/models', { provider: name }),
  testProvider: (name) =>
    request('POST', '/api/providers/test', { provider: name }),

  // -- misc
  audit: () => request('GET', '/api/audit'),
  login: (token) => request('POST', '/api/auth/session', { token }),
};

/* SSE — one multiplexed event stream for the whole app. */
export function connectEvents(onEvent, onStatus) {
  let es = null;
  let closed = false;
  let retryMs = 1500;

  function connect() {
    if (closed) return;
    es = new EventSource('/api/events');
    es.onopen = () => {
      retryMs = 1500;
      onStatus && onStatus('connected');
    };
    es.onmessage = (ev) => {
      try {
        onEvent(JSON.parse(ev.data));
      } catch {
        /* ignore malformed frames */
      }
    };
    es.onerror = () => {
      onStatus && onStatus('reconnecting');
      es.close();
      if (!closed) {
        setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 1.7, 15000);
      }
    };
  }
  connect();
  return () => {
    closed = true;
    es && es.close();
  };
}
