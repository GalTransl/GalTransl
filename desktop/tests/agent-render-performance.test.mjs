import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const timers = new Map();
let timerId = 0;
const markdownRenders = new Map();
const load = createTypeScriptLoader({ window: {
  addEventListener: () => {},
  removeEventListener: () => {},
  setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
  clearTimeout: id => timers.delete(id),
  setInterval: () => ++timerId,
  clearInterval: () => {},
} }, {
  '../../../components/AgentCacheRef': {
    AgentMarkdown: ({ text }) => {
      markdownRenders.set(text, (markdownRenders.get(text) || 0) + 1);
      return createElement('div', { className: 'test-markdown' }, text);
    },
  },
  './TranslationJobCard': { TranslationJobCard: () => createElement('div', null, 'job') },
  '../../../components/UiMotion': {
    AnimatedCollapse: ({ open, children, className }) => open
      ? createElement('div', { className }, typeof children === 'function' ? children() : children) : null,
  },
});
const { buildTimeline, appendStreamEvents } = load(new URL('../src/pages/agent/timeline.ts', import.meta.url));
const { createTimelineBuilder } = load(new URL('../src/pages/agent/timelineCache.ts', import.meta.url));
const { AgentGroupView } = load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
const { manualOpenState } = load(new URL('../src/pages/agent/uiState.ts', import.meta.url));
const { ToolRow } = load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));

test('microcompact events render the cleared result count instead of a zero-message summary', () => {
  const timeline = buildTimeline([{ type: 'compacted', step: 1, mode: 'microcompact', cleared: 3,
    removed: 0, tokens_before: 20000, tokens_after: 8000 }]);
  const group = timeline.find(row => row.type === 'activity');
  assert.equal(group.items[0].compactMode, 'microcompact');
  assert.equal(group.items[0].cleared, 3);
  manualOpenState.set(`microcompact-ui::${group.id}`, true);
  try {
    const html = renderToStaticMarkup(createElement(AgentGroupView, {
      group, isLive: false, projectDir: '', persistKey: 'microcompact-ui',
    }));
    assert.match(html, /清理 3 条旧工具结果|3 old tool results cleared/);
    assert.doesNotMatch(html, /摘要 0 条|0 earlier messages summarized/);
  } finally {
    manualOpenState.delete(`microcompact-ui::${group.id}`);
  }
});

test('replace tool header updates wildcard scope to the actual changed file count on result', () => {
  let item = { kind: 'tool', step: 1, id: 'replace-count', name: 'patch_transl_cache',
    arguments: { action: 'replace', files: '*', query: 'old', replacement: 'new' } };
  const view = () => createElement(ToolRow, { item, phase: item.result ? undefined : 'running', persistKey: 'replace-count' });
  let renderer;
  act(() => { renderer = create(view()); });
  const summary = () => renderer.root.findByProps({ className: 'agent-tool__summary' }).children.join('');
  assert.equal(summary(), '* · old -> new');
  act(() => {
    item = { ...item, ok: true, result: { updated: 5, files: [
      { filename: 'a.json', updated: 5 }, { filename: 'b.json', updated: 0 },
    ], changes: [{ path: '#1.pre_dst', before: 'old', after: 'new', kind: 'replace' }] } };
    renderer.update(view());
  });
  assert.equal(summary(), '1 个文件 · old -> new');
  assert.equal(renderer.root.findAll(node => node.type === 'div' && String(node.props.className).includes('agent-changes__body')).length, 1);
  act(() => renderer.unmount());
});

test('read aggregation preserves expansion across streamed additions and remounts, with errors outside', () => {
  const read = (step, extra = {}) => ({ kind: 'tool', step, id: `read-${step}`, name: 'read_input_file', arguments: { filename: `file-${step}.json` }, ok: true, ...extra });
  let group = { type: 'activity', id: 'reads', items: [read(1), read(2)] };
  const view = () => createElement(AgentGroupView, { group, isLive: true, projectDir: '', persistKey: 'read-test' });
  let renderer;
  act(() => { renderer = create(view()); });
  const header = () => renderer.root.find(node => node.type === 'button' && String(node.props.className).includes('agent-read-group__header'));
  const rows = () => renderer.root.findAll(node => node.type === 'div' && node.props['data-agent-tool-id']);
  assert.equal(header().props['aria-expanded'], false);
  assert.equal(rows().length, 0);
  act(() => header().props.onClick());
  const original = header();
  assert.equal(rows().length, 2);
  act(() => {
    group = { ...group, items: [...group.items, read(3, { ok: undefined }), read(4, { ok: undefined })] };
    renderer.update(view());
  });
  assert.equal(header(), original);
  assert.equal(header().props['aria-expanded'], true);
  assert.equal(rows().length, 4);
  const states = renderer.root.findAll(node => node.type === 'span' && String(node.props.className).startsWith('agent-tool__state '));
  assert.ok(states.some(node => node.props.className.includes('is-running')));
  assert.equal(rows()[3].find(node => node.type === 'span' && node.props.className === 'agent-tool__state').children.at(-1), '排队中');
  act(() => {
    group = { ...group, items: [...group.items, read(5, { ok: false, error: 'read failed' })] };
    renderer.update(view());
  });
  const failure = rows().find(node => node.props['data-agent-tool-id'] === 'read-5');
  for (let node = failure.parent; node; node = node.parent) {
    assert.notEqual(node.props.className, 'agent-read-group__inner');
  }
  act(() => renderer.unmount());
  act(() => { renderer = create(view()); });
  assert.equal(header().props['aria-expanded'], true);
  assert.equal(rows().length, 5);
  act(() => renderer.unmount());
});

test('one read keeps its original tool row without an aggregate disclosure', () => {
  const group = { type: 'activity', id: 'single-read', items: [{ kind: 'tool', step: 1, id: 'single', name: 'read_dict', ok: true }] };
  let renderer;
  act(() => { renderer = create(createElement(AgentGroupView, { group, isLive: true, projectDir: '', persistKey: 'single-read' })); });
  assert.equal(renderer.root.findAll(node => node.type === 'div' && node.props['data-agent-tool-id'] === 'single').length, 1);
  assert.equal(renderer.root.findAll(node => node.type === 'button' && String(node.props.className).includes('agent-read-group__header')).length, 0);
  act(() => renderer.unmount());
});

test('read aggregate summarizes every search in order, deduplicates and updates while collapsed', () => {
  const search = (step, query, name = 'read_transl_cache') => ({
    kind: 'tool', step, id: `search-${step}`, name, arguments: { query }, ok: true,
  });
  let group = { type: 'activity', id: 'search-summary', items: [search(1, '苦闷'), search(2, '煎熬', 'search_transl_cache')] };
  const view = () => createElement(AgentGroupView, { group, isLive: true, projectDir: '', persistKey: 'search-summary' });
  let renderer;
  act(() => { renderer = create(view()); });
  const header = () => renderer.root.find(node => node.type === 'button' && String(node.props.className).includes('agent-read-group__header'));
  const summary = () => header().findByProps({ className: 'agent-tool__summary' });
  assert.equal(summary().children.join(''), '搜索「苦闷」；搜索「煎熬」');
  act(() => {
    group = { ...group, items: [...group.items, search(3, '忍不住'), search(4, '苦闷')] };
    renderer.update(view());
  });
  const expected = '搜索「苦闷」；搜索「煎熬」；搜索「忍不住」';
  assert.equal(summary().children.join(''), expected);
  assert.equal(summary().props.title, expected);
  assert.equal(header().props['aria-expanded'], false);
  assert.equal(renderer.root.findAll(node => node.type === 'div' && node.props['data-agent-tool-id']).length, 0);
  act(() => header().props.onClick());
  assert.equal(summary().children.join(''), expected);
  act(() => renderer.unmount());
});

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

for (const source of ['stream', 'history']) {
  test(`${source} final reply keeps its node and markdown render across disclosure transitions`, () => {
    markdownRenders.clear();
    const events = [
      { type: 'reasoning_delta', step: 1, delta: 'thinking' },
      ...(source === 'stream'
        ? [{ type: 'content_delta', step: 2, delta: 'unique final answer' }, { type: 'content_end', step: 3, duration_ms: 1200 }]
        : [{ type: 'assistant_message', step: 3, parts: [{ type: 'reasoning', text: 'thinking' }, { type: 'text', text: 'unique final answer' }] }]),
      { type: 'finish', step: 4, summary: 'unique final answer' },
    ];
    const group = buildTimeline(events)[0];
    assert.equal(group.items.at(-1).final, true);
    let renderer;
    act(() => { renderer = create(createElement(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: source })); });
    const header = () => renderer.root.findByProps({ className: 'agent-activity__header' });
    const originalReply = renderer.root.findByProps({ 'data-agent-final-step': 4 });
    const check = () => {
      const replies = renderer.root.findAll(node => node.type === 'div' && node.props['data-agent-final-step'] === 4);
      assert.equal(replies.length, 1);
      let withinBody = false;
      for (let node = replies[0].parent; node; node = node.parent) {
        if (node.props.className === 'agent-activity__body') withinBody = true;
      }
      assert.equal(withinBody, false);
      assert.equal(replies[0], originalReply);
      assert.equal(markdownRenders.get('unique final answer'), 1);
      assert.equal(renderer.root.findAll(node => node.type === 'div' && node.props.className === 'test-markdown' && node.children[0] === 'unique final answer').length, 1);
    };
    try {
      check();
      act(() => header().props.onClick());
      check();
      const body = renderer.root.find(node => node.type === 'div' && node.props.className === 'agent-activity__collapse');
      assert.equal(header().props['aria-controls'], body.props.id);
      assert.equal(body.props.inert, undefined);
      act(() => header().props.onClick());
      // AnimatedDisclosure still retains its other children during the closing transition.
      assert.equal(renderer.root.findAllByProps({ className: 'agent-activity__body' }).length, 1);
      assert.equal(renderer.root.find(node => node.type === 'div' && node.props.className === 'agent-activity__collapse').props.inert, '');
      check();
      act(() => { for (const fn of [...timers.values()]) fn(); });
      assert.equal(renderer.root.findAllByProps({ className: 'agent-activity__body' }).length, 0);
      check();
      act(() => header().props.onClick());
      check();
    } finally {
      act(() => renderer.unmount());
      manualOpenState.delete(`${source}::${group.id}`);
    }
  });
}

test('finish only claims the last matching reply and preserves distinct narration and tool boundaries', () => {
  const build = createTimelineBuilder();
  const events = [
    { type: 'content', step: 1, content: 'answer' },
    { type: 'tool_call', step: 2, id: 'read', name: 'read_file' },
    { type: 'tool_result', step: 3, id: 'read', ok: true, result: 'result' },
    { type: 'content_delta', step: 4, delta: 'answer' },
  ];
  const before = build(events);
  const after = build([...events, { type: 'finish', step: 5, summary: 'answer' }]);
  assert.equal(after[0].items[0].final, undefined);
  assert.equal(after[0].items.at(-1).final, true);
  assert.equal(before[0].items.at(-1).final, undefined);
  assert.equal(after[0].items.at(-1).streaming, false);
  const distinct = buildTimeline([...events, { type: 'finish', step: 5, summary: 'different answer' }]);
  assert.equal(distinct[0].items.at(-1).final, undefined);
  const toolBoundary = buildTimeline([...events.slice(0, 3), { type: 'finish', step: 5, summary: 'answer' }]);
  assert.equal(toolBoundary[0].items[0].final, undefined);
});

test('automatic completion places the final reply outside immediately and manual expansion survives remount', () => {
  const props = { isLive: true, projectDir: '', persistKey: 'live-final' };
  const events = [{ type: 'reasoning_delta', step: 1, delta: 'thinking' }, { type: 'content_delta', step: 2, delta: 'answer' }];
  let renderer;
  act(() => { renderer = create(createElement(AgentGroupView, { ...props, group: buildTimeline(events)[0] })); });
  const group = buildTimeline([...events, { type: 'finish', step: 3, summary: 'answer' }])[0];
  try {
    act(() => renderer.update(createElement(AgentGroupView, { ...props, isLive: false, group })));
    const header = renderer.root.findByProps({ className: 'agent-activity__header' });
    assert.equal(header.props['aria-expanded'], false);
    assert.equal(renderer.root.findAllByProps({ 'data-agent-final-step': 3 }).length, 1);
    act(() => header.props.onClick());
    assert.equal(renderer.root.findByProps({ className: 'agent-activity__header' }).props['aria-expanded'], true);
    assert.equal(renderer.root.findByProps({ className: 'agent-activity__body' })
      .findAll(node => node.type === 'div' && node.props['data-agent-final-step'] === 3).length, 0);
    act(() => renderer.unmount());
    act(() => { renderer = create(createElement(AgentGroupView, { ...props, isLive: false, group })); });
    assert.equal(renderer.root.findByProps({ className: 'agent-activity__header' }).props['aria-expanded'], true);
    assert.equal(renderer.root.findAllByProps({ 'data-agent-final-step': 3 }).length, 1);
  } finally {
    act(() => renderer.unmount());
    manualOpenState.delete('live-final::a-1');
  }
});

test('a plain final response has no empty execution disclosure', () => {
  const group = buildTimeline([
    { type: 'content_delta', step: 1, delta: 'plain reply' },
    { type: 'finish', step: 2, summary: 'plain reply' },
  ])[0];
  const html = renderToStaticMarkup(createElement(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'plain' }));
  assert.match(html, /plain reply/);
  assert.doesNotMatch(html, /agent-activity__header/);
});
