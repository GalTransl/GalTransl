import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(options = {}) {
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
  const follower = followTail(viewport, content, (value) => states.push(value), options);
  const reply = (start, height) => ({
    start, height,
    getBoundingClientRect() { return { top: 102 + this.start - viewport.scrollTop, height: this.height }; },
  });
  return { viewport, content, observed, follower, states, reply, resize: () => resize(),
    scroll: (value, user = true) => {
      if (user) listeners.get('wheel')?.({ deltaY: value - viewport.scrollTop });
      viewport.scrollTop = value;
      listeners.get('scroll')?.();
    },
    disconnected: () => disconnected, listeners };
}

test('restored activity expanding over several frames stays at the bottom without new events', () => {
  const f = fixture();
  assert.deepEqual(f.observed, [f.content, f.viewport]);
  assert.equal(f.viewport.scrollTop, 600);
  for (const height of [1150, 1320, 1600]) {
    f.viewport.scrollHeight = height;
    f.scroll(f.viewport.scrollTop, false); // Layout can emit scroll before ResizeObserver.
    f.resize();
    assert.equal(f.viewport.scrollTop, height - 400);
    assert.equal(f.states.at(-1), true);
  }
});

test('prepending history restores the reading position and later output does not pull it to the bottom', () => {
  let saved;
  const f = fixture({ onPosition: position => { saved = position; } });
  f.scroll(100);
  f.viewport.scrollHeight = 1600;
  f.follower.restoreReading(700);
  assert.equal(f.viewport.scrollTop, 700);
  assert.equal(saved.scrollTop, 700);
  assert.equal(saved.following, false);
  f.viewport.scrollHeight = 1900;
  f.resize();
  assert.equal(f.viewport.scrollTop, 700);
  f.follower.jump();
  assert.equal(f.viewport.scrollTop, 1500);
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
    f.scroll(f.viewport.scrollTop, false); // Native layout scroll events must not resume tail following.
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
  for (const name of ['wheel', 'pointerdown', 'touchmove', 'keydown']) {
    const f = fixture();
    f.viewport.scrollHeight = 2200;
    f.resize();
    const reply = f.reply(1200, 1000);
    f.follower.revealFinal(reply);
    if (name === 'touchmove') f.listeners.get('touchstart')({ touches: [{ clientY: 100 }] });
    const event = name === 'wheel' ? { deltaY: -100 }
      : name === 'pointerdown' ? { target: f.viewport }
      : name === 'touchmove' ? { touches: [{ clientY: 200 }] }
      : { key: 'PageUp' };
    f.listeners.get(name)(event);
    f.scroll(900, false);
    if (name === 'pointerdown') f.listeners.get('pointerup')();
    if (name === 'touchmove') f.listeners.get('touchend')();
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

test('following state is reported once while content keeps growing', () => {
  const f = fixture();
  // 构造时 follow 已到底，之后每一帧的高度变化都不该再上报一次（否则每帧一次 setState）。
  assert.deepEqual(f.states, [true]);
  for (const height of [1200, 1500, 1800]) {
    f.viewport.scrollHeight = height;
    f.resize();
    f.listeners.get('scroll')?.(); // 自己写 scrollTop 引起的回声
  }
  assert.deepEqual(f.states, [true]);
  assert.equal(f.viewport.scrollTop, 1400);
});

test('programmatic scroll echoes never resume or drop following', () => {
  const f = fixture();
  f.scroll(200);
  assert.deepEqual(f.states, [true, false]);
  // 回声：位置没变，不该重算跟随态
  f.listeners.get('scroll')?.();
  f.listeners.get('scroll')?.();
  assert.deepEqual(f.states, [true, false]);
  assert.equal(f.follower.isFollowing(), false);
});

test('repeated layout passes without movement do not rewrite the reading position', () => {
  const positions = [];
  const f = fixture({ onPosition: (position) => positions.push(position) });
  f.resize();
  f.resize();
  f.resize();
  assert.equal(positions.length, 1);
  // 位置对象来自加载器所在的 realm，逐字段比而不是 deepEqual。
  assert.equal(positions[0].scrollTop, 600);
  assert.equal(positions[0].following, true);
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
    f.scroll(top, false);
    assert.equal(f.states.at(-1), true);
  }
  f.viewport.clientHeight = 300;
  f.resize();
  assert.equal(f.viewport.scrollTop, 700);
});

test('terminal height clamping before ResizeObserver never detaches tail following', () => {
  const f = fixture();
  f.viewport.scrollHeight = 650;
  f.scroll(250, false);
  f.resize();
  assert.equal(f.follower.isFollowing(), true);
  f.viewport.scrollHeight = 1000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 600);
});

test('upward input wins over same-frame resize before a scroll event arrives', () => {
  const f = fixture();
  f.listeners.get('wheel')({ deltaY: -200 });
  f.viewport.scrollHeight = 1500;
  f.resize();
  assert.equal(f.viewport.scrollTop, 600);
  assert.equal(f.follower.isFollowing(), false);
});

test('opening history releases following without pushing the disclosure header out of view', () => {
  const f = fixture();
  f.listeners.get('click')({ target: { closest: selector => selector === 'button[aria-expanded]' ? {} : null } });
  f.viewport.scrollHeight = 1800;
  f.resize();
  assert.equal(f.viewport.scrollTop, 600);
  assert.equal(f.follower.isFollowing(), false);
  assert.equal(f.states.at(-1), false);
  f.follower.jump();
  assert.equal(f.viewport.scrollTop, 1400);
});

test('editing a field or clicking response text does not release the completion anchor', () => {
  const f = fixture();
  f.viewport.scrollHeight = 2200;
  f.resize();
  const reply = f.reply(1200, 1000);
  f.follower.revealFinal(reply);
  f.listeners.get('keydown')({ key: 'Home', target: { closest: () => ({}) } });
  f.listeners.get('pointerdown')({ target: {} });
  f.listeners.get('wheel')({ deltaY: 0, deltaX: 100 });
  reply.start = 1000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 984);
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

test('session reading targets survive short cached history and subsequent authoritative layout', () => {
  let saved;
  const f = fixture({ initial: { scrollTop: 1500, following: false }, onPosition: value => { saved = value; } });
  assert.equal(f.viewport.scrollTop, 600);
  f.scroll(600, false);
  assert.equal(saved.scrollTop, 1500);
  f.viewport.scrollHeight = 2500;
  f.resize();
  assert.equal(f.viewport.scrollTop, 1500);
  assert.equal(f.follower.isFollowing(), false);
  f.viewport.scrollHeight = 3000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 1500);
});

test('reader input cancels pending restore before delayed history arrives', () => {
  const f = fixture({ initial: { scrollTop: 1500, following: false } });
  f.scroll(100);
  f.viewport.scrollHeight = 2500;
  f.resize();
  assert.equal(f.viewport.scrollTop, 100);
});

test('returning followers follow background growth; hidden initial history waits for activation', () => {
  const f = fixture({ initial: { scrollTop: 100, following: true }, active: false });
  assert.equal(f.viewport.scrollTop, 0);
  f.viewport.scrollHeight = 2500;
  f.follower.setActive(true);
  assert.equal(f.viewport.scrollTop, 2100);
  f.viewport.scrollHeight = 3000;
  f.resize();
  assert.equal(f.viewport.scrollTop, 2600);
});

test('A to B to A retains independent positions without measuring mutated DOM on disposal', () => {
  const { createReadingMemory, readingKey } = createTypeScriptLoader()(new URL('../src/pages/agent/readingMemory.ts', import.meta.url));
  const memory = createReadingMemory();
  const keyA = readingKey('project', 'a');
  const keyB = readingKey('project', 'b');
  const enter = key => fixture({ initial: memory.read(key), onPosition: value => memory.save(key, value) });
  const a = enter(keyA);
  a.scroll(125);
  a.viewport.scrollHeight = 400; // React removed history before layout cleanup.
  a.viewport.scrollTop = 0;
  a.follower.dispose();
  const b = enter(keyB);
  b.follower.dispose();
  const returned = enter(keyA);
  assert.equal(returned.viewport.scrollTop, 125);
  assert.equal(returned.follower.isFollowing(), false);
  assert.equal(memory.read(keyB).following, true);
});

test('reading memory is project scoped, bounded by recency, copied and clearable', () => {
  const { createReadingMemory, readingKey } = createTypeScriptLoader()(new URL('../src/pages/agent/readingMemory.ts', import.meta.url));
  const memory = createReadingMemory(2);
  const a = readingKey('one', 'same');
  const b = readingKey('two', 'same');
  const value = { scrollTop: 123, following: false };
  memory.save(a, value);
  value.scrollTop = 999;
  memory.save(b, value);
  assert.equal(memory.read(a).scrollTop, 123);
  memory.save('c', value);
  assert.equal(memory.read(b), undefined);
  memory.delete(a);
  assert.equal(memory.read(a), undefined);
});
