import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const store = createTypeScriptLoader()(new URL('../src/lib/agentActivity.ts', import.meta.url));
  const seen = {};
  function Probe({ kind }) {
    // Copy into this realm: the module runs in its own vm context (different Object prototype).
    seen[kind] = { indicator: store.useAgentIndicator(kind), unseen: { ...store.useUnseenAgentLights(kind) } };
    return null;
  }
  act(() => { create(h('div', null, h(Probe, { kind: 'translation' }), h(Probe, { kind: 'extraction' }))); });
  const run = (callback) => act(callback);
  return { store, seen, run };
}

test('a session that finishes while nobody is viewing it lights up until it is opened', () => {
  const { store, seen, run } = fixture();
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'running' }, { id: 'b', phase: 'done' }], 1));
  assert.equal(seen.extraction.indicator, 'running');
  assert.equal(seen.translation.indicator, null);
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'done' }, { id: 'b', phase: 'done' }], 2));
  assert.equal(seen.extraction.indicator, 'done');
  assert.deepEqual(seen.extraction.unseen, { a: 'done' });
  run(() => store.setViewingAgentSession('extraction', 'a'));
  assert.equal(seen.extraction.indicator, null);
  assert.deepEqual(seen.extraction.unseen, {});
});

test('the session being viewed never lights up, and failures take priority over completions', () => {
  const { store, seen, run } = fixture();
  run(() => store.setViewingAgentSession('translation', 'a'));
  run(() => store.reportAgentSessions('translation', ['a', 'b', 'c'].map(id => ({ id, phase: 'running' })), 1));
  run(() => store.reportAgentSessions('translation', [
    { id: 'a', phase: 'done' }, { id: 'b', phase: 'done' }, { id: 'c', phase: 'running' },
  ], 2));
  assert.equal(seen.translation.indicator, 'running');
  assert.deepEqual(seen.translation.unseen, { b: 'done' });
  run(() => store.reportAgentSessions('translation', [{ id: 'c', phase: 'failed' }], 3));
  assert.equal(seen.translation.indicator, 'failed');
  run(() => store.setViewingAgentSession('translation', 'c'));
  assert.equal(seen.translation.indicator, 'done');
});

test('late responses from older requests cannot re-trigger a finished session', () => {
  const { store, seen, run } = fixture();
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'running' }], 1));
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'done' }], 3));
  run(() => store.setViewingAgentSession('extraction', 'a'));
  run(() => store.setViewingAgentSession('extraction', ''));
  // A poll sent at t=2 (before the run finished) answers last.
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'running' }], 2));
  run(() => store.reportAgentSessions('extraction', [{ id: 'a', phase: 'done' }], 4));
  assert.equal(seen.extraction.indicator, null);
});

test('a run seen finishing on screen stays dark even if the list poll arrives after leaving', () => {
  const { store, seen, run } = fixture();
  run(() => store.reportAgentSessions('translation', [{ id: 'a', phase: 'running' }], 1));
  run(() => store.setViewingAgentSession('translation', 'a'));
  run(() => store.settleViewedAgentSession('translation', 'a'));
  assert.equal(seen.translation.indicator, null);
  run(() => store.setViewingAgentSession('translation', ''));
  run(() => store.reportAgentSessions('translation', [{ id: 'a', phase: 'done' }], Date.now() + 1));
  assert.equal(seen.translation.indicator, null);
  assert.deepEqual(seen.translation.unseen, {});
});

test('complete lists and explicit forgetting drop lights of deleted or hidden sessions', () => {
  const { store, seen, run } = fixture();
  run(() => store.reportAgentSessions('extraction', ['a', 'b'].map(id => ({ id, phase: 'running' })), 1));
  run(() => store.reportAgentSessions('extraction', ['a', 'b'].map(id => ({ id, phase: 'done' })), 2));
  assert.deepEqual(seen.extraction.unseen, { a: 'done', b: 'done' });
  run(() => store.forgetAgentSessions('extraction', ['a']));
  assert.deepEqual(seen.extraction.unseen, { b: 'done' });
  run(() => store.reportAgentSessions('extraction', [], 3, { complete: true }));
  assert.deepEqual(seen.extraction.unseen, {});
  assert.equal(seen.extraction.indicator, null);
});

test('translation statuses map onto the shared phases', () => {
  const { store } = fixture();
  assert.equal(store.translationSessionPhase('running'), 'running');
  assert.equal(store.translationSessionPhase('failed'), 'failed');
  for (const status of ['awaiting_input', 'stopped', '', undefined]) assert.equal(store.translationSessionPhase(status), 'done');
});
