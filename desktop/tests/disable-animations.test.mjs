import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const KEY = 'galtransl-disable-animations';
const CHANGE_EVENT = 'galtransl:disable-animations-change';

class FakeCustomEvent {
  constructor(type, options) {
    this.type = type;
    this.detail = options?.detail;
  }
}

/** api.ts + lib/motion.ts 共享同一份 localStorage / window / document 桩。 */
function fixture(stored) {
  const store = new Map(stored === undefined ? [] : [[KEY, stored]]);
  const events = [];
  const listeners = new Set();
  const dataset = {};
  const load = createTypeScriptLoader({
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => { store.set(key, String(value)); },
    },
    window: {
      addEventListener: (type, fn) => listeners.add(fn),
      removeEventListener: (type, fn) => listeners.delete(fn),
      dispatchEvent: (event) => { events.push(event); for (const fn of listeners) fn(event); return true; },
    },
    document: { documentElement: { dataset } },
    CustomEvent: FakeCustomEvent,
  }, {
    '@tauri-apps/api/core': { invoke: () => Promise.resolve({}) },
  });
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  const motion = load(new URL('../src/lib/motion.ts', import.meta.url));
  return { api, motion, load, store, events, dataset };
}

test('animations stay on by default and the root dataset says so', () => {
  const f = fixture();
  assert.equal(f.api.getDisableAnimationsPreference(), false);
  assert.equal(f.api.applyAnimationsPreference(), false);
  assert.equal(f.dataset.animations, 'on');
  assert.equal(f.motion.prefersReducedMotion(), false);
});

test('enabling the setting persists, broadcasts, and flips the root dataset', () => {
  const f = fixture();
  assert.equal(f.api.setDisableAnimationsPreference(true), true);
  assert.equal(f.api.getDisableAnimationsPreference(), true);
  assert.equal(f.store.get(KEY), 'true');
  assert.equal(f.events.length, 1);
  assert.equal(f.events[0].type, CHANGE_EVENT);
  assert.equal(f.events[0].detail, true);
  assert.equal(f.api.applyAnimationsPreference(), true);
  assert.equal(f.dataset.animations, 'off');
  // JS 动效（发射动画、平滑滚动等）按同一开关走 reduced-motion 分支。
  assert.equal(f.motion.prefersReducedMotion(), true);
  // 关回去同样广播，dataset 复位。
  assert.equal(f.api.setDisableAnimationsPreference(false), false);
  assert.equal(f.events.at(-1).detail, false);
  f.api.applyAnimationsPreference();
  assert.equal(f.dataset.animations, 'on');
});

test('usePrefersReducedMotion follows the setting without a remount', () => {
  const f = fixture();
  function Probe() {
    return createElement('span', null, String(f.motion.usePrefersReducedMotion()));
  }
  let root;
  act(() => { root = create(createElement(Probe)); });
  assert.equal(root.toJSON().children[0], 'false');
  act(() => { f.api.setDisableAnimationsPreference(true); });
  assert.equal(root.toJSON().children[0], 'true');
  act(() => { f.api.setDisableAnimationsPreference(false); });
  assert.equal(root.toJSON().children[0], 'false');
});

test('invalid stored values fall back to the default (animations on)', () => {
  for (const stored of ['yes', '1', '', 'OFF']) {
    const f = fixture(stored);
    assert.equal(f.api.getDisableAnimationsPreference(), false, stored);
  }
  for (const stored of ['true', 'false']) {
    const f = fixture(stored);
    assert.equal(f.api.getDisableAnimationsPreference(), stored === 'true', stored);
  }
});

test('success tail drops smooth scrolling while animations are disabled', () => {
  const lastBehavior = (stored) => {
    const f = fixture(stored);
    const calls = [];
    let top = 0;
    const viewport = {
      scrollHeight: 1000,
      clientHeight: 400,
      get scrollTop() { return top; },
      set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
      addEventListener: () => {},
      removeEventListener: () => {},
      scrollTo(options) {
        calls.push(options);
        if (options.behavior !== 'smooth') this.scrollTop = options.top;
      },
    };
    const { createSuccessTail } = f.load(new URL('../src/lib/useSuccessTail.ts', import.meta.url));
    const tail = createSuccessTail(viewport);
    tail.update('history', true);
    viewport.scrollHeight = 1400;
    tail.update('new', true);
    return calls.at(-1).behavior;
  };
  assert.equal(lastBehavior('true'), 'auto');
  assert.equal(lastBehavior('false'), 'smooth');
});
