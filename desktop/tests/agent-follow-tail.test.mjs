import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  let resize;
  let disconnected = false;
  const listeners = new Map();
  const observed = [];
  let top = 0;
  const viewport = {
    scrollHeight: 1000, clientHeight: 400,
    get scrollTop() { return top; },
    set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
    scrollTo({ top: next, behavior }) { if (behavior !== 'smooth') this.scrollTop = next; },
  };
  const loader = createTypeScriptLoader({ ResizeObserver: class {
    constructor(callback) { resize = callback; }
    observe(element) { observed.push(element); }
    disconnect() { disconnected = true; }
  } });
  const { followTail } = loader(new URL('../src/pages/agent/followTail.ts', import.meta.url));
  const states = [];
  const content = {};
  const follower = followTail(viewport, content, (value) => states.push(value));
  return { viewport, content, observed, follower, states, resize: () => resize(),
    scroll: (value) => { viewport.scrollTop = value; listeners.get('scroll')?.(); },
    disconnected: () => disconnected, listeners };
}

test('restored activity expanding over several frames stays at the bottom without new events', () => {
  const f = fixture();
  assert.deepEqual(f.observed, [f.content, f.viewport]);
  assert.equal(f.viewport.scrollTop, 600);
  for (const height of [1150, 1320, 1600]) {
    f.viewport.scrollHeight = height;
    f.scroll(f.viewport.scrollTop); // Layout can emit scroll before ResizeObserver.
    f.resize();
    assert.equal(f.viewport.scrollTop, height - 400);
    assert.equal(f.states.at(-1), true);
  }
});

test('reading earlier messages is preserved while content expands and messages arrive', () => {
  const f = fixture();
  f.scroll(200);
  assert.equal(f.states.at(-1), false);
  f.viewport.scrollHeight = 1800;
  f.resize();
  f.follower.follow();
  assert.equal(f.viewport.scrollTop, 200);
  f.scroll(1400);
  f.viewport.scrollHeight = 2000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 1600);
});

test('smooth jump remains in follow mode during intermediate scroll events', () => {
  const f = fixture();
  f.scroll(100);
  f.follower.jump('smooth');
  for (const top of [150, 300, 450, 600]) {
    f.scroll(top);
    assert.equal(f.states.at(-1), true);
  }
  f.viewport.clientHeight = 300;
  f.resize();
  assert.equal(f.viewport.scrollTop, 700);
});

test('unmount disconnects observers and does not scroll detached content', () => {
  const f = fixture();
  f.follower.dispose();
  assert.equal(f.disconnected(), true);
  assert.equal(f.listeners.size, 0);
  f.viewport.scrollHeight = 2000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 600);
});

test('hidden pages pause scrolling and retain the user’s reading/following mode', () => {
  const f = fixture();
  f.scroll(200);
  f.follower.setActive(false);
  f.viewport.scrollHeight = 1800;
  f.resize();
  assert.equal(f.viewport.scrollTop, 200);
  f.follower.setActive(true);
  assert.equal(f.viewport.scrollTop, 200);
  f.follower.jump();
  f.follower.setActive(false);
  f.viewport.scrollHeight = 2200;
  f.resize();
  assert.equal(f.viewport.scrollTop, 1400);
  f.follower.setActive(true);
  assert.equal(f.viewport.scrollTop, 1800);
});
