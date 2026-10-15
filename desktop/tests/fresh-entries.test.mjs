import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(initial = ['history']) {
  let now = 0, nextTimer = 0, fresh;
  const timers = new Map();
  const window = {
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, deadline: now + delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  };
  const load = createTypeScriptLoader({ window, Date: class extends Date { static now() { return now; } } });
  const { useFreshEntries } = load(new URL('../src/lib/useFreshEntries.ts', import.meta.url));
  function View(props) { fresh = useFreshEntries(props.scope, props.entries, props.active); return null; }
  let props = { scope: 'project:job', entries: initial.map(id => ({ id })), active: true };
  let renderer;
  act(() => { renderer = create(h(View, props)); });
  return {
    timers,
    fresh: () => [...fresh].sort(),
    value: () => fresh,
    update(ids, changes = {}) {
      props = { ...props, entries: ids.map(id => ({ id })), ...changes };
      act(() => renderer.update(h(View, props)));
    },
    advance(ms) {
      const target = now + ms;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.deadline <= target)
          .sort((a, b) => a[1].deadline - b[1].deadline)[0];
        if (!next) break;
        now = next[1].deadline;
        timers.delete(next[0]);
        act(() => next[1].callback());
      }
      now = target;
    },
    dispose: () => act(() => renderer.unmount()),
  };
}

test('initial history loads without animation; new rows expire while snapshots arrive every second', () => {
  const f = fixture([]);
  f.update(['history']);
  assert.deepEqual(f.fresh(), []);
  f.update(['history', 'a']);
  f.advance(1000);
  f.update(['history', 'a', 'b']);
  f.advance(1000);
  f.update(['history', 'a', 'b', 'c']);
  assert.deepEqual(f.fresh(), ['a', 'b', 'c']);
  f.advance(200);
  assert.deepEqual(f.fresh(), ['b', 'c']);
  f.advance(1000);
  assert.deepEqual(f.fresh(), ['c']);
  f.advance(1000);
  assert.deepEqual(f.fresh(), []);
  assert.equal(f.timers.size, 0);
  f.dispose();
});

test('unchanged polls preserve set identity and do not extend freshness', () => {
  const f = fixture();
  f.update(['history', 'new']);
  const value = f.value();
  for (let i = 0; i < 20; i++) {
    f.advance(100);
    f.update(['history', 'new']);
    assert.deepEqual(f.fresh(), ['new']);
    assert.equal(f.value(), value);
    assert.equal(f.timers.size, 1);
  }
  f.advance(200);
  assert.deepEqual(f.fresh(), []);
  f.dispose();
});

test('rolling snapshots release removed rows and keep only one expiry timer', () => {
  const f = fixture();
  for (let i = 0; i < 500; i++) {
    f.advance(10);
    f.update([String(i)]);
    assert.deepEqual(f.fresh(), [String(i)]);
    assert.equal(f.timers.size, 1);
  }
  f.update([]);
  assert.deepEqual(f.fresh(), []);
  assert.equal(f.timers.size, 0);
  f.dispose();
});

test('hidden pages and new jobs discard old highlights; unmount releases timers', () => {
  const f = fixture();
  f.update(['history', 'a']);
  f.update(['history', 'a'], { active: false });
  assert.deepEqual(f.fresh(), []);
  assert.equal(f.timers.size, 0);
  f.update(['a', 'b']);
  f.update(['a', 'b'], { active: true });
  assert.deepEqual(f.fresh(), []);
  f.update(['a', 'b', 'c']);
  assert.deepEqual(f.fresh(), ['c']);
  f.update(['different-history'], { scope: 'project:new-job' });
  assert.deepEqual(f.fresh(), []);
  f.update(['different-history', 'd']);
  assert.equal(f.timers.size, 1);
  f.dispose();
  assert.equal(f.timers.size, 0);
});
