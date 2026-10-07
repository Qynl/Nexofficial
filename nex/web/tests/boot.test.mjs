// End-to-end smoke test: load the REAL index.html into jsdom, load the
// real webgl.js/animations.js globals the page depends on, then import the
// real main.js and let it boot — exactly what a browser does. This is the
// test that would have caught the broken `face`/`RunCard` imports even if
// the static import-graph check had missed something: if the app cannot
// boot without throwing, this fails.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.join(HERE, '..');

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
  }
  close() {}
}

test('the real index.html + main.js boot without throwing', async () => {
  const html = fs.readFileSync(path.join(WEB_DIR, 'index.html'), 'utf8');
  const dom = new JSDOM(html, {
    url: 'http://localhost/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;

  // Globals main.js and its dependencies expect from a real browser tab.
  window.fetch = async () => ({
    ok: true, status: 200,
    json: async () => ({
      conversations: [], servers: { servers: [], total: 0, connected: 0,
                                     tools: 0 },
      providers: {}, state: {},
    }),
  });
  window.EventSource = FakeEventSource;
  window.requestAnimationFrame = () => 0;
  window.cancelAnimationFrame = () => {};
  window.scrollTo = () => {};
  Object.defineProperty(window.HTMLElement.prototype, 'scrollIntoView',
    { value: () => {}, configurable: true });

  const uncaught = [];
  window.addEventListener('error', (e) => uncaught.push(e.error || e.message));
  const originalConsoleError = window.console.error;
  window.console.error = (...args) => {
    uncaught.push(args.map(String).join(' '));
    originalConsoleError(...args);
  };

  global.window = window;
  global.document = window.document;
  // Node 21+ ships its own read-only `navigator` global (getter-only, for
  // fetch-API compatibility), so it cannot be reassigned with `=`.
  Object.defineProperty(global, 'navigator',
    { value: window.navigator, configurable: true });
  global.localStorage = window.localStorage;
  global.fetch = window.fetch;
  global.EventSource = window.EventSource;
  global.CustomEvent = window.CustomEvent;
  global.requestAnimationFrame = window.requestAnimationFrame;
  global.cancelAnimationFrame = window.cancelAnimationFrame;

  // webgl.js / animations.js are loaded as plain (non-module) <script>
  // tags by the real page, before main.js. jsdom's runScripts mode does
  // not execute inline module graphs for us, so load them the same way
  // the browser would: evaluate their source against the window.
  for (const rel of ['js/webgl.js', 'js/animations.js']) {
    const src = fs.readFileSync(path.join(WEB_DIR, rel), 'utf8');
    window.eval(src);
  }
  assert.equal(typeof window.NexGL, 'function',
    'webgl.js registers window.NexGL the way index.html depends on');
  assert.equal(typeof window.NexAnim, 'function',
    'animations.js registers window.NexAnim the way index.html depends on');

  let bootError = null;
  try {
    await import(path.join(WEB_DIR, 'js', 'main.js') + '?t=' + Date.now());
    // boot() runs async work (api.state(), etc.); give it a tick to settle.
    await new Promise((resolve) => setTimeout(resolve, 50));
  } catch (err) {
    bootError = err;
  }

  assert.equal(bootError, null,
    'main.js must import and boot without throwing: '
    + (bootError && bootError.stack));
  assert.deepEqual(uncaught, [],
    'no uncaught errors/console.error calls during boot: '
    + JSON.stringify(uncaught));

  // A handful of concrete, user-visible signs the app actually wired up.
  assert.ok(window.document.getElementById('btn-new-chat'),
    'the real page markup is present');
  assert.equal(typeof window.__nexFace, 'object',
    'the face singleton was created and exposed for debugging, exactly '
    + 'as boot() does on a real page load');
});
