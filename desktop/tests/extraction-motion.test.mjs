import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const source = file => new URL(`../src/pages/extraction/${file}`, import.meta.url);

test('extraction live batches use shared motion after rendering, while history and background updates stay still', () => {
  let active = true;
  const calls = [], selectors = [], listeners = new Map();
  const reduced = { matches: false, addEventListener() {}, removeEventListener() {} };
  const target = {
    closest: () => null,
    getBoundingClientRect: () => ({ top: 30, bottom: 50, height: 20 }),
    animate(frames, timing) {
      const animation = { frames, timing, cancel() { this.cancelled = true; } };
      calls.push(animation); return animation;
    },
  };
  const viewport = { getBoundingClientRect: () => ({ top: 0, bottom: 400 }) };
  const content = { querySelectorAll(selector) { selectors.push(selector); return [target]; } };
  const load = createTypeScriptLoader({
    window: { matchMedia: () => reduced }, CSS: { escape: value => value },
    document: { hidden: false, documentElement: { dataset: {} },
      addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
  }, {
    '../../components/PageActivity': { usePageActive: () => active },
    '../agent/rows/AgentGroupView': { AgentGroupView: () => null },
    '../agent/followTail': { followTail: () => ({ follow() {}, setActive() {}, dispose() {}, revealFinal() {} }) },
  });
  const { Conversation } = load(source('Conversation.tsx'));
  const { reduceEvents } = load(source('transcript.ts'));
  const { extractionStep } = load(source('presentation.ts'));
  const history = [{ type: 'user_message', seq: 1, text: 'History' }];
  let props = { rows: reduceEvents([], history), motionEvents: [], sessionId: 'fixture', running: true,
    status: 'running', pending: null, older: false, loadOlder() {} };
  let renderer;
  act(() => { renderer = create(h(Conversation, props), {
    createNodeMock: element => element.props.className === 'agent-thread' ? content : viewport,
  }); });
  assert.equal(calls.length, 0);
  const update = events => {
    props = { ...props, rows: reduceEvents(props.rows, events), motionEvents: events };
    act(() => renderer.update(h(Conversation, props)));
  };
  update([
    { type: 'user_message', seq: 2, text: 'New message' },
    { type: 'thinking_delta', messageId: 'answer', text: 'Think' },
    { type: 'assistant_delta', messageId: 'answer', text: 'Respond' },
    { type: 'tool_start', toolCallId: 'read-1', toolName: 'read' },
    { type: 'tool_end', toolCallId: 'read-1', text: 'Done' },
    { type: 'assistant_message', messageId: 'answer', text: 'Final answer', stopReason: 'stop' },
  ]);
  assert.ok(selectors.includes(`[data-agent-user-step="${extractionStep('user-2')}"] .agent-bubble`));
  assert.ok(selectors.includes('[data-agent-tool-id="read-1"] .agent-tool__state-dot'));
  assert.ok(selectors.includes('.agent-reasoning.is-streaming .agent-reasoning__icon'));
  assert.ok(selectors.includes('.agent-content.is-streaming .agent-content__text > :first-child'));
  assert.ok(selectors.includes(`[data-agent-final-step="${extractionStep('answer')}"] .agent-final > :first-child`));
  assert.equal(calls.length, 5);
  assert.ok(calls.every(call => call.timing.duration <= 280 && call.timing.fill === 'none'));
  active = false;
  act(() => renderer.update(h(Conversation, props)));
  assert.ok(calls.every(call => call.cancelled));
  update([{ type: 'user_message', seq: 3, text: 'Hidden update' }]);
  active = true;
  act(() => renderer.update(h(Conversation, props)));
  assert.equal(calls.length, 5);
  // Loading a snapshot or older history clears cues instead of replaying arrivals.
  props = { ...props, rows: [...reduceEvents([], [{ type: 'user_message', seq: 0, text: 'Older' }]), ...props.rows], motionEvents: [] };
  act(() => renderer.update(h(Conversation, props)));
  assert.equal(calls.length, 5);
  act(() => renderer.unmount());
  assert.equal(listeners.size, 0);
});

test('snapshots stay still except the new turn just submitted; SSE and reconnects keep motion scoped', async () => {
  let state, stream, sent = false;
  const history = { type: 'user_message', seq: 1, text: 'Old message' };
  const sentMessage = { type: 'user_message', seq: 2, text: 'New message' };
  class Source { constructor() { stream = this; } close() {} }
  const load = createTypeScriptLoader({ EventSource: Source, localStorage: { getItem: () => 'fixture', setItem() {} } }, {
    '../../components/PageActivity': { usePageActive: () => true, useRetainPage() {} },
    './api': { extractionUrl: () => 'synthetic-stream', extractionApi: async action => {
      if (action === 'environment') return { ready: true, settings: { skill_dir: '', python_path: '' }, skill: { installed: false, current_commit: '', latest_commit: '', update_available: false } };
      if (action === 'sessions') return { sessions: [], total: 0, next_offset: null };
      if (action === 'message') { sent = true; return { ok: true }; }
      if (action === 'session') return {
        session: { id: 'fixture', status: sent ? 'running' : 'idle' }, events: sent ? [history, sentMessage] : [history],
        live_events: [], cursor: sent ? 2 : 1, pending: null, has_more: false, before_seq: null,
      };
      throw Error(`Unexpected ${action}`);
    } },
  });
  const { useExtraction } = load(source('useExtraction.ts'));
  function Probe() { state = useExtraction(); return null; }
  let renderer;
  await act(async () => { renderer = create(h(Probe)); });
  assert.equal(state.view.motionEvents.length, 0);
  await act(async () => { await state.mutate('message', { message: 'New message' }); });
  assert.equal(state.view.motionEvents.length, 1);
  assert.equal(state.view.motionEvents[0].seq, 2);
  await act(async () => {
    stream.onmessage({ data: JSON.stringify({ type: 'assistant_delta', seq: 3, messageId: 'answer', text: 'Live' }) });
    await new Promise(resolve => setTimeout(resolve, 100));
  });
  assert.equal(state.view.rows.at(-1).text, 'Live');
  assert.equal(state.view.motionEvents[0].type, 'assistant_delta');
  await act(async () => state.reload());
  assert.equal(state.view.motionEvents.length, 0);
  await act(async () => renderer.unmount());
});
