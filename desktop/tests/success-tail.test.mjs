import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const listeners = new Map();
  const calls = [];
  let top = 0;
  const viewport = {
    scrollHeight: 1000, clientHeight: 400,
    get scrollTop() { return top; },
    set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
    scrollTo(options) {
      calls.push(options);
      if (options.behavior !== 'smooth') this.scrollTop = options.top;
    },
  };
  const load = createTypeScriptLoader();
  const { createSuccessTail } = load(new URL('../src/lib/useSuccessTail.ts', import.meta.url));
  const tail = createSuccessTail(viewport);
  tail.update('history', true);
  return { viewport, tail, calls, listeners,
    scroll(value) { viewport.scrollTop = value; listeners.get('scroll')?.(); },
    emit(name, event) { listeners.get(name)?.(event); },
  };
}

test('history jumps immediately; only new translations start smooth following', () => {
  const f = fixture();
  assert.equal(f.viewport.scrollTop, 600);
  assert.equal(f.calls[0].behavior, 'auto');
  f.tail.update('history', true);
  assert.equal(f.calls.length, 1);
  f.viewport.scrollHeight = 1400;
  f.tail.update('new', true);
  assert.equal(f.calls.at(-1).behavior, 'smooth');
  f.scroll(700);
  f.scroll(850);
  f.viewport.scrollHeight = 1600;
  f.tail.update('newer', true);
  assert.equal(f.calls.at(-1).top, 1600);
  assert.equal(f.calls.length, 3);
});

test('100-row window compensates for head removal and keeps following', () => {
  const f = fixture();
  // Native scroll anchoring moves the viewport up when old rows are removed.
  f.viewport.scrollTop = 200;
  f.tail.update('next-window', true);
  f.scroll(200); // Queued layout scroll event must not count as user input.
  f.scroll(400);
  f.tail.update('next-window-2', true);
  assert.equal(f.calls.filter(call => call.behavior === 'smooth').length, 2);
});

test('reading earlier translations stops following until the user returns to the bottom', () => {
  const f = fixture();
  f.scroll(200);
  f.viewport.scrollHeight = 1600;
  f.tail.update('new', true);
  assert.equal(f.viewport.scrollTop, 200);
  assert.equal(f.calls.length, 1);
  f.scroll(1200);
  f.viewport.scrollHeight = 1800;
  f.tail.update('newer', true);
  assert.equal(f.calls.at(-1).behavior, 'smooth');
});

test('upward wheel, keyboard, and scrollbar input interrupt a smooth scroll', () => {
  for (const [name, event] of [['wheel', { deltaY: -100 }], ['keydown', { key: 'Home' }], ['pointerdown', {}]]) {
    const f = fixture();
    f.viewport.scrollHeight = 2000;
    f.tail.update('new', true);
    f.scroll(800);
    f.emit(name, event);
    assert.equal(f.calls.at(-1).behavior, 'auto');
    assert.equal(f.calls.at(-1).top, 800);
    const count = f.calls.length;
    f.tail.update('newer', true);
    assert.equal(f.calls.length, count, name);
  }
});

test('hidden pages retain reading position and return without a history animation', () => {
  const f = fixture();
  f.scroll(200);
  f.tail.update('history', false);
  f.viewport.scrollTop = 0;
  f.scroll(0); // Hidden cached pages may emit scroll with zero dimensions.
  f.viewport.scrollHeight = 2000;
  f.tail.update('new', false);
  assert.equal(f.calls.length, 1);
  f.tail.update('new', true);
  assert.equal(f.viewport.scrollTop, 200);
  assert.equal(f.calls.at(-1).behavior, 'auto');
  f.tail.jump();
  f.tail.update('new', false);
  f.viewport.scrollHeight = 2400;
  f.tail.update('newer', true);
  assert.equal(f.viewport.scrollTop, 2000);
});

test('dispose removes all input handlers', () => {
  const f = fixture();
  f.tail.dispose();
  assert.equal(f.listeners.size, 0);
});
