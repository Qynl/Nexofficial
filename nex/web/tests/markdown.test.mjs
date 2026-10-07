// markdown.js renders model output. It is the single highest-value XSS
// surface in the frontend: everything the model says flows through it.
// The module's own header claims "No dependencies, no raw HTML ever — the
// output is built entirely from DOM nodes, so model output cannot inject
// markup." These tests hold that claim to account.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { renderMarkdown, renderStreaming } from '../js/markdown.js';

function dom() {
  return new JSDOM('<!doctype html><html><body></body></html>').window.document;
}

test('plain text round-trips as a paragraph', () => {
  const doc = dom();
  const root = renderMarkdown('hello world', doc);
  assert.equal(root.querySelector('p').textContent, 'hello world');
});

test('a <script> tag in model output never becomes a real element', () => {
  const doc = dom();
  const hostile = 'ignore previous instructions <script>alert(1)</script> ok';
  const root = renderMarkdown(hostile, doc);
  assert.equal(root.querySelectorAll('script').length, 0,
    'no <script> element is ever created from model text');
  assert.ok(root.textContent.includes('<script>alert(1)</script>'),
    'the hostile markup survives only as literal, inert text');
});

test('an <img onerror=...> payload never becomes a real element', () => {
  const doc = dom();
  const hostile = '<img src=x onerror="alert(document.cookie)">';
  const root = renderMarkdown(hostile, doc);
  assert.equal(root.querySelectorAll('img').length, 0,
    'no <img> element is ever created from model text');
});

test('markdown links only become real <a> elements for http(s) URLs', () => {
  const doc = dom();
  const safe = renderMarkdown('[click me](https://example.com/x)', doc);
  const a = safe.querySelector('a');
  assert.ok(a, 'an https link becomes a real anchor');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.equal(a.getAttribute('rel'), 'noopener noreferrer');

  const hostile = renderMarkdown('[click me](javascript:alert(1))', doc);
  assert.equal(hostile.querySelectorAll('a').length, 0,
    'a javascript: URL never becomes a real anchor (link is left as '
    + 'literal, inert text)');

  const dataUri = renderMarkdown('[x](data:text/html,<script>1</script>)',
    doc);
  assert.equal(dataUri.querySelectorAll('a').length, 0,
    'a data: URL never becomes a real anchor either');
});

test('fenced code blocks render the body as text, never as markup', () => {
  const doc = dom();
  const hostile = '```\n<img src=x onerror=alert(1)>\n```';
  const root = renderMarkdown(hostile, doc);
  const code = root.querySelector('pre code');
  assert.ok(code, 'a fenced block becomes a <pre><code>');
  assert.equal(code.textContent, '<img src=x onerror=alert(1)>',
    'the code body is set via textContent, so it is always inert');
  assert.equal(root.querySelectorAll('img').length, 0);
});

test('headings, lists, and tables build real structural elements', () => {
  const doc = dom();
  const src = '# Title\n\n- one\n- two\n\n| a | b |\n| - | - |\n| 1 | 2 |\n';
  const root = renderMarkdown(src, doc);
  assert.equal(root.querySelector('h1').textContent, 'Title');
  assert.equal(root.querySelectorAll('ul li').length, 2);
  assert.equal(root.querySelectorAll('table td').length, 2);
});

test('renderMarkdown never throws on degenerate input', () => {
  const doc = dom();
  for (const input of [null, undefined, '', '```', '**unterminated',
    '[broken](', '|||', '#'.repeat(500), 'a'.repeat(50000)]) {
    assert.doesNotThrow(() => renderMarkdown(input, doc),
      `renderMarkdown(${JSON.stringify(input)}) must not throw`);
  }
});

test('renderStreaming (the cheap per-flush renderer) is also script-safe',
  () => {
    const doc = dom();
    const root = renderStreaming('before <script>alert(1)</script> after',
      doc);
    assert.equal(root.querySelectorAll('script').length, 0);
    assert.ok(root.textContent.includes('<script>'));
  });

test('renderStreaming never throws on degenerate input', () => {
  const doc = dom();
  for (const input of [null, undefined, '', '```unterminated\ncode']) {
    assert.doesNotThrow(() => renderStreaming(input, doc));
  }
});
