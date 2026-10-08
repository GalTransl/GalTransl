import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader({}, {
  '../../../components/AgentCacheRef': {
    AgentMarkdown: ({ text }) => createElement('div', { className: 'test-markdown' }, text),
  },
  './TranslationJobCard': { TranslationJobCard: () => createElement('div', null, 'job') },
});
const { buildTimeline, appendStreamEvents } = load(new URL('../src/pages/agent/timeline.ts', import.meta.url));
const { createTimelineBuilder } = load(new URL('../src/pages/agent/timelineCache.ts', import.meta.url));
const { AgentGroupView } = load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
const { manualOpenState } = load(new URL('../src/pages/agent/uiState.ts', import.meta.url));

function history() {
  return [
    { type: 'user_message', step: 1, message: 'first' },
    { type: 'tool_call', step: 2, id: 'parent', name: 'run_subagents', arguments: {} },
    { type: 'subagent_start', step: 3, id: 'child', parent_id: 'parent' },
    { type: 'subagent_tool_call', step: 4, id: 'child', tool_call_id: 'edit', name: 'patch_transl_cache' },
    { type: 'stopped', step: 5, reason: 'stop' },
    { type: 'user_message', step: 6, message: 'second' },
    { type: 'tool_call', step: 7, id: 'wait', name: 'wait', arguments: {} },
    { type: 'wait_start', step: 8, id: 'wait', total_ms: 1000, remaining_ms: 1000 },
    { type: 'content_delta', step: 9, delta: 'hello' },
  ];
}

test('stream updates reuse history and unchanged tools within the live turn', () => {
  const build = createTimelineBuilder();
  const events = history();
  const before = build(events);
  const after = build(appendStreamEvents(events, [{ type: 'content_delta', step: 10, delta: ' world' }]));
  before.slice(0, -1).forEach((group, i) => assert.equal(after[i], group));
  assert.notEqual(after.at(-1), before.at(-1));
  assert.equal(after.at(-1).items[0], before.at(-1).items[0]);
  assert.equal(before.at(-1).items[1].content, 'hello');
  assert.equal(after.at(-1).items[1].content, 'hello world');
});

test('late child results update the original history without mutating earlier render models', () => {
  const build = createTimelineBuilder();
  const events = history();
  const before = build(events);
  const nextEvents = [...events,
    { type: 'subagent_tool_result', step: 10, id: 'child', tool_call_id: 'edit', ok: true, result: { updated: 1 } },
    { type: 'subagent_done', step: 11, id: 'child', status: 'done', modified_count: 1 },
  ];
  const after = build(nextEvents);
  assert.deepEqual(after, buildTimeline(nextEvents));
  assert.notEqual(after[1], before[1]);
  assert.equal(before[1].items[0].subagents[0].status, 'running');
  assert.equal(after[1].items[0].subagents[0].status, 'done');
  assert.equal(after.at(-1), before.at(-1));
});

test('snapshot replacement, empty history and same ids from another session remain authoritative', () => {
  const build = createTimelineBuilder();
  const events = history();
  build(events);
  const replacement = events.map(event => event.step === 1 ? { ...event, message: 'corrected' } : event);
  assert.deepEqual(build(replacement), buildTimeline(replacement));
  assert.equal(build(replacement), build(replacement.slice()));
  assert.equal(build([]).length, 0);
  assert.equal(build([{ type: 'user_message', step: 1, message: 'other session' }])[0].message, 'other session');
});

test('sharing does not walk large tool result payloads', () => {
  const result = { get expensive() { throw new Error('payload traversed'); } };
  const events = [
    { type: 'tool_call', step: 1, id: 'read', name: 'read_file' },
    { type: 'tool_result', step: 2, id: 'read', ok: true, result },
  ];
  const build = createTimelineBuilder();
  const before = build(events);
  assert.equal(build(events.slice()), before);
});

test('rebuilding old events preserves fallback retry and child timestamps', () => {
  let clock = 1000;
  const timedLoad = createTypeScriptLoader({ Date: class extends Date {
    static now() { return clock += 1000; }
  } });
  const { createTimelineBuilder: createBuilder } = timedLoad(new URL('../src/pages/agent/timelineCache.ts', import.meta.url));
  const build = createBuilder();
  const events = [...history(), { type: 'llm_retry_start', step: 10, attempt: 1, delay_ms: 5000 }];
  const before = build(events);
  const after = build(events.slice());
  assert.equal(after, before);
});

test('collapsed history mounts no tool bodies or reasoning markdown, while final answers stay visible', () => {
  const group = {
    type: 'activity', id: 'long',
    items: Array.from({ length: 250 }, (_, step) => ({ kind: 'tool', step, id: `tool-${step}`, name: 'read_file', result: 'large hidden output' })),
    finalContent: { kind: 'content', step: 251, content: 'visible answer', final: true },
  };
  const html = renderToStaticMarkup(createElement(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'perf' }));
  assert.doesNotMatch(html, /agent-tool__|large hidden output/);
  assert.match(html, /visible answer/);
  manualOpenState.set('perf::long', true);
  try {
    const openHtml = renderToStaticMarkup(createElement(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'perf' }));
    assert.equal((openHtml.match(/agent-tool__header/g) || []).length, 250);
  } finally {
    manualOpenState.delete('perf::long');
  }
  const reasoning = { type: 'activity', id: 'thinking', items: [{ kind: 'reasoning', step: 1, content: 'hidden reasoning', streaming: true }] };
  const liveHtml = renderToStaticMarkup(createElement(AgentGroupView, { group: reasoning, isLive: true, projectDir: '', persistKey: 'perf' }));
  assert.match(liveHtml, /agent-reasoning__header/);
  assert.doesNotMatch(liveHtml, /test-markdown/);
});
