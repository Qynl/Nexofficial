// Every `import { X } from './y.js'` in nex/web/js must resolve to a real
// export of y.js. There is no bundler and no TypeScript here — nothing
// else in this project catches a stale/renamed export at build time, so a
// broken import is a hard ES-module link error that silently breaks the
// WHOLE page in a real browser (the entire module graph fails to load,
// not just the one broken call site).
//
// This exact class of bug was found live in this codebase: chat.js and
// composer.js imported a `face` singleton that face.js never exported
// (only the `Face` class existed), and chat.js imported a `RunCard` that
// runview.js never exported (it exports `buildRunCard`). Either one alone
// would have made main.js's entire import chain fail to link, so nothing
// in the app would boot at all. Both are fixed; this test exists so a
// regression like it fails loudly and immediately instead of silently
// shipping a blank page.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const JS_DIR = path.join(HERE, '..', 'js');

function exportsOf(file) {
  const src = fs.readFileSync(file, 'utf8');
  const names = new Set();
  for (const m of src.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)) {
    names.add(m[1]);
  }
  for (const m of src.matchAll(/export\s+(?:const|let)\s+(\w+)/g)) {
    names.add(m[1]);
  }
  for (const m of src.matchAll(/export\s+class\s+(\w+)/g)) {
    names.add(m[1]);
  }
  for (const m of src.matchAll(/export\s*\{([^}]+)\}/g)) {
    for (const n of m[1].split(',')) {
      const name = n.trim();
      if (name) names.add(name.split(' as ').pop().trim());
    }
  }
  return names;
}

function namedImports(file) {
  const src = fs.readFileSync(file, 'utf8');
  const out = [];
  for (const m of src.matchAll(/import\s*\{([^}]+)\}\s*from\s*'(\.[^']+)'/g)) {
    const names = m[1].split(',').map((n) => n.trim().split(' as ')[0].trim())
      .filter(Boolean);
    out.push({ target: m[2], names });
  }
  return out;
}

test('every named import across nex/web/js resolves to a real export', () => {
  const files = fs.readdirSync(JS_DIR).filter((f) => f.endsWith('.js'));
  const exportCache = new Map(
    files.map((f) => [f, exportsOf(path.join(JS_DIR, f))]));

  const problems = [];
  for (const file of files) {
    for (const { target, names } of namedImports(path.join(JS_DIR, file))) {
      const targetFile = path.normalize(target);
      const targetExports = exportCache.get(targetFile);
      if (!targetExports) {
        problems.push(`${file}: imports from '${target}', which does not `
          + 'exist in nex/web/js');
        continue;
      }
      for (const name of names) {
        if (!targetExports.has(name)) {
          problems.push(`${file}: imports '${name}' from '${target}', but `
            + `${target} has no such export (this would be a hard `
            + 'ES-module link error in a real browser)');
        }
      }
    }
  }

  assert.deepEqual(problems, [],
    'broken import(s) found:\n' + problems.join('\n'));
});
