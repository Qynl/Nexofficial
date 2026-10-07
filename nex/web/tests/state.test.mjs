// state.js's createStore() is the one piece of "framework" the whole
// frontend relies on — every view module reads/writes through it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../js/state.js';

test('get() returns the initial state', () => {
  const store = createStore({ a: 1 });
  assert.deepEqual(store.get(), { a: 1 });
});

test('set() merges a patch without mutating the previous snapshot', () => {
  const store = createStore({ a: 1, b: 2 });
  const before = store.get();
  store.set({ b: 3 });
  assert.deepEqual(before, { a: 1, b: 2 },
    'a previously-read snapshot is never mutated in place');
  assert.deepEqual(store.get(), { a: 1, b: 3 });
});

test('update() applies a reducer function', () => {
  const store = createStore({ n: 1 });
  store.update((s) => ({ n: s.n + 1 }));
  assert.equal(store.get().n, 2);
});

test('subscribers are notified with (state, prevState, changedKeys)', () => {
  const store = createStore({ a: 1, b: 2 });
  const calls = [];
  store.subscribe((state, prev, keys) => calls.push({ state, prev, keys }));
  store.set({ a: 9 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].state.a, 9);
  assert.equal(calls[0].prev.a, 1);
  assert.deepEqual(calls[0].keys, ['a']);
});

test('unsubscribe actually stops further notifications', () => {
  const store = createStore({ a: 1 });
  let count = 0;
  const unsub = store.subscribe(() => { count++; });
  store.set({ a: 2 });
  unsub();
  store.set({ a: 3 });
  assert.equal(count, 1, 'the unsubscribed callback is never called again');
});

test('a throwing subscriber does not break other subscribers or the store',
  () => {
    const store = createStore({ a: 1 });
    let secondRan = false;
    store.subscribe(() => { throw new Error('boom'); });
    store.subscribe(() => { secondRan = true; });
    assert.doesNotThrow(() => store.set({ a: 2 }));
    assert.ok(secondRan, 'a later subscriber still runs even if an '
      + 'earlier one throws');
    assert.equal(store.get().a, 2, 'the state update itself still applies');
  });

test('multiple independent stores do not share state', () => {
  const s1 = createStore({ a: 1 });
  const s2 = createStore({ a: 1 });
  s1.set({ a: 2 });
  assert.equal(s2.get().a, 1, 'stores are independent instances, not a '
    + 'shared module-level singleton');
});
