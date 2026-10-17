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
    scrollHeight: 1000, clientHeight: 400, clientTop: 2,
    getBoundingClientRect() { return { top: 100 }; },
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
  const reply = (start, height) => ({
    start, height,
    getBoundingClientRect() { return { top: 102 + this.start - viewport.scrollTop, height: this.height }; },
  });
  return { viewport, content, observed, follower, states, reply, resize: () => resize(),
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

test('long final replies reveal their start and stay aligned throughout work-block collapse', () => {
  const f = fixture();
  f.viewport.scrollHeight = 2200;
  f.resize();
  const reply = f.reply(1200, 1000);
  f.follower.revealFinal(reply);
  assert.equal(f.viewport.scrollTop, 1184);
  assert.equal(f.states.at(-1), false);
  for (const start of [1000, 800, 600]) {
    reply.start = start;
    f.viewport.scrollHeight = start + reply.height;
    f.scroll(f.viewport.scrollTop); // Native layout scroll events must not resume tail following.
    f.resize();
    assert.equal(f.viewport.scrollTop, start - 16);
    assert.equal(reply.getBoundingClientRect().top, 118);
  }
});

test('short replies and readers away from the bottom are not repositioned at completion', () => {
  const f = fixture();
  f.follower.revealFinal(f.reply(700, 300));
  assert.equal(f.viewport.scrollTop, 600);
  assert.equal(f.follower.isFollowing(), true);
  f.scroll(200);
  f.follower.revealFinal(f.reply(600, 1000));
  f.resize();
  assert.equal(f.viewport.scrollTop, 200);
  assert.equal(f.follower.isFollowing(), false);
});

test('clearing the conversation releases a detached final reply', () => {
  const f = fixture();
  f.viewport.scrollHeight = 2200;
  f.resize();
  const reply = f.reply(1200, 1000);
  f.follower.revealFinal(reply);
  reply.isConnected = false;
  reply.start = 0;
  f.resize();
  assert.equal(f.viewport.scrollTop, 1184);
});

test('wheel, scrollbar, touch and keyboard input release the reply anchor', () => {
  for (const [name, event] of [['wheel', {}], ['pointerdown', {}], ['touchstart', {}], ['keydown', { key: 'PageDown' }]]) {
    const f = fixture();
    f.viewport.scrollHeight = 2200;
    f.resize();
    const reply = f.reply(1200, 1000);
    f.follower.revealFinal(reply);
    f.listeners.get(name)(event);
    f.scroll(900);
    reply.start = 1000;
    f.resize();
    assert.equal(f.viewport.scrollTop, 900, name);
    f.scroll(1800);
    assert.equal(f.follower.isFollowing(), true);
    f.follower.dispose();
  }
});

test('jump, page hiding and disposal clear completion anchors', () => {
  const f = fixture();
  f.viewport.scrollHeight = 2200;
  f.resize();
  const reply = f.reply(1200, 1000);
  f.follower.revealFinal(reply);
  f.follower.jump();
  assert.equal(f.viewport.scrollTop, 1800);
  f.follower.revealFinal(reply);
  f.follower.setActive(false);
  assert.equal(f.follower.isFollowing(), false);
  reply.start = 1000;
  f.resize();
  f.follower.setActive(true);
  assert.equal(f.viewport.scrollTop, 1184);
  f.follower.dispose();
  f.follower.revealFinal(reply);
  f.resize();
  assert.equal(f.viewport.scrollTop, 1184);
  assert.equal(f.listeners.size, 0);
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
