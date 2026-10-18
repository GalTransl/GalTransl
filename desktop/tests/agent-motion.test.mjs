import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const listeners = new Map();
  const reduced = { matches: false, addEventListener: (_, fn) => listeners.set('reduced', fn), removeEventListener: () => listeners.delete('reduced') };
  // i18n/core（经 lib/api 间接加载）会在模块加载时写 documentElement.lang。
  const document = { hidden: false, documentElement: { dataset: {} }, addEventListener: (_, fn) => listeners.set('visibility', fn), removeEventListener: () => listeners.delete('visibility') };
  const calls = [];
  const targets = new Map();
  let queries = 0;
  const target = (selector, options = {}) => {
    const node = {
      closest: () => options.hidden ? {} : null,
      getBoundingClientRect: () => ({ top: options.offscreen ? 500 : 30, bottom: options.offscreen ? 520 : 50, height: 20 }),
      animate(frames, timing) {
        const animation = { frames, timing, cancelled: false, cancel() { this.cancelled = true; } };
        calls.push(animation);
        return animation;
      },
    };
    targets.set(selector, [node]);
    return node;
  };
  const load = createTypeScriptLoader({ window: { matchMedia: () => reduced }, document, CSS: { escape: value => value } });
  const { createAgentMotion } = load(new URL('../src/pages/agent/motion.ts', import.meta.url));
  const motion = createAgentMotion({ getBoundingClientRect: () => ({ top: 0, bottom: 400 }) }, {
    querySelectorAll(selector) { queries++; return targets.get(selector) || []; },
  });
  return { motion, calls, target, listeners, document, reduced, queries: () => queries };
}

test('history alone does not animate; hundreds of alternating stream tokens animate each kind only once', () => {
  const f = fixture();
  f.motion.flush();
  assert.equal(f.queries(), 0);
  f.target('.agent-content.is-streaming .agent-content__text > :first-child');
  f.target('.agent-reasoning.is-streaming .agent-reasoning__icon');
  for (let step = 1; step <= 300; step++) {
    f.motion.observe({ type: step % 2 ? 'content_delta' : 'reasoning_delta', step, delta: 'text' });
    f.motion.flush();
    f.motion.observe({ type: step % 2 ? 'content_end' : 'reasoning_end', step });
  }
  assert.equal(f.calls.length, 2);
  assert.equal(f.queries(), 2);
  for (const animation of f.calls) {
    assert.ok(animation.timing.duration <= 280);
    assert.equal(animation.timing.fill, 'none');
    animation.onfinish();
    assert.equal(animation.cancelled, true);
  }
  f.motion.dispose();
});

test('parallel tool bursts cap both pending and active animations; completed animations release the budget', () => {
  const f = fixture();
  for (let step = 0; step < 100; step++) {
    f.target(`[data-agent-tool-id="tool-${step}"] .agent-tool__state-dot`);
    f.motion.observe({ type: 'tool_call', step, id: `tool-${step}` });
  }
  f.motion.flush();
  assert.equal(f.calls.length, 6);
  assert.equal(f.queries(), 6);
  f.motion.observe({ type: 'tool_result', step: 100, id: 'tool-99' });
  f.motion.flush();
  assert.equal(f.calls.length, 6);
  f.calls[0].onfinish();
  f.motion.observe({ type: 'tool_result', step: 101, id: 'tool-99' });
  f.motion.flush();
  assert.equal(f.calls.length, 7);
  f.motion.dispose();
  assert.ok(f.calls.every(animation => animation.cancelled));
  assert.equal(f.listeners.size, 0);
});

test('hidden pages, reduced motion, resets and disposal cancel motion and discard queued events', () => {
  const f = fixture();
  f.target('[data-agent-user-step="1"] .agent-bubble');
  const start = () => { f.motion.observe({ type: 'user_message', step: 1 }); f.motion.flush(); };
  start();
  f.motion.setActive(false);
  assert.equal(f.calls[0].cancelled, true);
  start();
  f.motion.setActive(true);
  f.motion.flush();
  assert.equal(f.calls.length, 1);
  start();
  f.document.hidden = true;
  f.listeners.get('visibility')();
  assert.equal(f.calls[1].cancelled, true);
  start();
  f.document.hidden = false;
  f.motion.flush();
  assert.equal(f.calls.length, 2);
  start();
  f.reduced.matches = true;
  f.listeners.get('reduced')();
  assert.equal(f.calls[2].cancelled, true);
  start();
  f.reduced.matches = false;
  f.motion.observe({ type: 'user_message', step: 1 });
  f.motion.reset();
  f.motion.flush();
  assert.equal(f.calls.length, 3);
  f.motion.dispose();
  start();
  assert.equal(f.calls.length, 3);
});

test('offscreen, collapsed and missing targets are skipped; final reply animates opacity without shifting its anchor', () => {
  const f = fixture();
  for (const [step, options] of [[1, { offscreen: true }], [2, { hidden: true }]]) {
    f.target(`[data-agent-user-step="${step}"] .agent-bubble`, options);
    f.motion.observe({ type: 'user_message', step });
  }
  f.motion.observe({ type: 'tool_call', step: 3, id: 'missing' });
  f.motion.flush();
  assert.equal(f.calls.length, 0);
  f.target('[data-agent-final-step="4"] .agent-final > :first-child');
  f.motion.observe({ type: 'finish', step: 4, summary: 'final answer' });
  let prepared = false;
  f.motion.flush(() => { prepared = true; });
  assert.equal(prepared, true);
  assert.equal(f.calls.length, 1);
  assert.ok(f.calls[0].frames.every(frame => !('transform' in frame)));
  f.motion.dispose();
});
