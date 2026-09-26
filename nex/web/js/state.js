/* Tiny observable store — no framework, no magic. */

export function createStore(initial = {}) {
  let state = { ...initial };
  const subs = new Map();
  let nextId = 1;

  function get() {
    return state;
  }

  function set(patch) {
    const prev = state;
    state = { ...state, ...patch };
    for (const fn of subs.values()) {
      try {
        fn(state, prev, Object.keys(patch));
      } catch (err) {
        console.error('[store] subscriber failed', err);
      }
    }
  }

  function update(fn) {
    set(fn(state));
  }

  function subscribe(fn) {
    const id = nextId++;
    subs.set(id, fn);
    return () => subs.delete(id);
  }

  return { get, set, update, subscribe };
}

/* Global app state shape:
   {
     conversations: [],
     activeId: null,
     messages: [],          // messages of active conversation
     servers: {servers, total, connected, tools},
     provider: {providers, roles, chains, role_models},
     generating: false,
     activeRun: null,       // run summary of active run (if any)
     view: 'chat' | 'servers',
     voice: {ttsEnabled, sttSupported, ttsSupported, recActive},
   }
*/
export const store = createStore({
  conversations: [],
  activeId: null,
  messages: [],
  servers: { servers: [], total: 0, connected: 0, tools: 0 },
  provider: null,
  generating: false,
  activeRun: null,
  view: 'chat',
  faceMode: 'hidden',
});
