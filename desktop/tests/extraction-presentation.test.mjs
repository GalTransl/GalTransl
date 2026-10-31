import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader({ URLSearchParams });
const { extractionTimeline, extractionContextUsage, createExtractionTimelineBuilder } = load(new URL('../src/pages/extraction/presentation.ts', import.meta.url));
const { toolRowPhases } = load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
const user = { id: 'user-1', role: 'user', text: 'Inspect this isolated fixture' };
const tool = { id: 'tool-1', role: 'tool', tool: 'run_python', args: { code: 'print("fixture")' }, text: 'partial output' };

test('running extraction labels follow tool and text activity while preserving waiting and stopping states', () => {
  const componentLoad = createTypeScriptLoader({ URLSearchParams }, {
    '../../components/PageActivity': { usePageActive: () => false },
    '../../../components/PageActivity': { usePageActive: () => false },
  });
  const { Conversation } = componentLoad(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  const { workingLabel } = componentLoad(new URL('../src/pages/agent/timeline.ts', import.meta.url));
  const { statusLabel } = componentLoad(new URL('../src/pages/extraction/labels.ts', import.meta.url));
  const props = { rows: [user, tool], sessionId: 'working-label', running: true, status: 'running', pending: null, older: false, loadOlder() {} };
  let renderer;
  act(() => { renderer = create(h(Conversation, props)); });
  const label = () => renderer.root.findByProps({ className: 'agent-working__label' }).children.join('');
  assert.equal(label(), workingLabel(extractionTimeline(props.rows, true, null)));
  act(() => renderer.update(h(Conversation, { ...props, rows: [user, { id: 'a', role: 'assistant', text: 'Writing' }] })));
  assert.equal(label(), workingLabel(extractionTimeline([user, { id: 'a', role: 'assistant', text: 'Writing' }], true, null)));
  for (const status of ['awaiting_permission', 'awaiting_input', 'stopping']) {
    act(() => renderer.update(h(Conversation, { ...props, status })));
    assert.equal(label(), statusLabel(status));
  }
  act(() => renderer.unmount());
});

test('the shared queue supports immediate sending, editing, deletion and image-only entries', () => {
  const { MessageQueue } = load(new URL('../src/pages/agent/MessageQueue.tsx', import.meta.url));
  const calls = [];
  const queued = [{ id: 'q', text: 'Followup', images: [{ id: `${'a'.repeat(64)}.png`, name: 'fixture.png', width: 1, height: 1 }] }];
  let props = { queued, editing: null, scope: { sessionId: 'queue-parity', extraction: true },
    onEditingChange: editing => { props = { ...props, editing }; renderer.update(h(MessageQueue, props)); },
    onSave: () => calls.push(['save', props.editing.text]), onSend: id => calls.push(['send', id]), onDelete: id => calls.push(['delete', id]) };
  let renderer;
  act(() => { renderer = create(h(MessageQueue, props)); });
  assert.match(renderer.root.findByType('img').props.src, /agent=extraction/);
  act(() => renderer.root.findAllByType('button').find(button => button.props.className === 'agent-queue__act is-primary').props.onClick());
  assert.equal(calls[0].join(','), 'send,q');
  act(() => renderer.root.findAllByType('button').find(button => button.props['aria-label']?.includes('编辑')).props.onClick());
  act(() => renderer.root.findByType('input').props.onChange({ target: { value: '' } }));
  assert.equal(renderer.root.findByProps({ className: 'agent-queue__act is-primary' }).props.disabled, false);
  act(() => renderer.root.findByType('input').props.onKeyDown({ key: 'Enter', nativeEvent: { isComposing: true }, preventDefault() {} }));
  assert.equal(calls.length, 1);
  act(() => renderer.root.findByType('input').props.onKeyDown({ key: 'Enter', nativeEvent: {}, preventDefault() {} }));
  assert.equal(calls[1][0], 'save');
  act(() => renderer.root.findByType('input').props.onKeyDown({ key: 'Escape' }));
  act(() => renderer.root.findAllByType('button').find(button => button.props['aria-label']?.includes('删除')).props.onClick());
  assert.equal(calls[2].join(','), 'delete,q');
  act(() => renderer.update(h(MessageQueue, { ...props, busy: true })));
  assert.ok(renderer.root.findAllByType('button').filter(button => button.props.className?.startsWith('agent-queue__act')).every(button => button.props.disabled));
  act(() => renderer.unmount());
});

test('stream segments finish independently and replay retains durations across split history', () => {
  const { reduceEvents, prependHistory } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const events = [
    { type: 'user_message', text: 'Inspect' },
    { type: 'thinking_delta', messageId: 'a', text: 'Thinking' },
    { type: 'reasoning_end', messageId: 'a', duration_ms: 1200 },
    { type: 'assistant_delta', messageId: 'a', text: 'Progress' },
    { type: 'content_end', messageId: 'a', duration_ms: 100 },
    { type: 'thinking_delta', messageId: 'a', text: ' again' },
    { type: 'reasoning_end', messageId: 'a', duration_ms: 800 },
    { type: 'assistant_message', messageId: 'a', text: 'Progress', stopReason: 'toolUse' },
    { type: 'tool_start', toolCallId: 't', toolName: 'read' },
    { type: 'tool_end', toolCallId: 't', text: 'Read', duration_ms: 3000 },
  ].map((event, index) => ({ ...event, seq: index + 1 }));
  const segments = extractionTimeline(reduceEvents([], events.slice(0, 4)), true, null)[1].items;
  assert.equal(segments[0].streaming, false);
  assert.equal(segments[1].streaming, true);
  const replay = reduceEvents([], events);
  for (let split = 1; split < events.length; split += 1) {
    const restored = prependHistory(reduceEvents([], events.slice(split)), reduceEvents([], events.slice(0, split)));
    assert.equal(restored.find(row => row.id === 'a').thinkingDurationMs, 2000);
    assert.equal(restored.find(row => row.id === 'a').durationMs, 100);
  }
  const group = extractionTimeline(replay, false, null)[1];
  assert.equal(group.items.reduce((sum, item) => sum + (item.durationMs || 0), 0), 5100);
  assert.equal(group.items.at(-1).durationMs, 3000);
  assert.equal(reduceEvents(replay, [events[6]])[1].thinkingDurationMs, 2000);
});

test('compaction results use shared rows and do not inflate work step totals', () => {
  const { reduceEvents } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const { AgentGroupView } = load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
  const rows = reduceEvents([], [
    { type: 'user_message', seq: 1, text: 'Inspect' },
    { type: 'compacted', seq: 2, mode: 'microcompact', cleared: 4, removed: 0, tokens_before: 12000, tokens_after: 8000 },
    { type: 'compacted', seq: 3, removed: 8, summary_chars: 1000, tokens_before: 8000, tokens_after: 2000 },
    { type: 'tool_start', seq: 4, toolCallId: 't', toolName: 'read' },
    { type: 'tool_end', seq: 5, toolCallId: 't', text: 'OK', duration_ms: 2000 },
  ]);
  const group = extractionTimeline(rows, false, null)[1];
  assert.equal(group.items[0].cleared, 4);
  assert.equal(group.items[1].summaryChars, 1000);
  let renderer;
  act(() => { renderer = create(h(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'compact-parity' })); });
  assert.equal(renderer.root.findAllByProps({ className: 'agent-activity__meta' }).length, 0);
  act(() => renderer.root.findByProps({ className: 'agent-activity__header' }).props.onClick());
  const text = JSON.stringify(renderer.toJSON());
  assert.match(text, /12\.0K/); assert.match(text, /8\.0K/); assert.match(text, /2\.0K/);
  act(() => renderer.unmount());
});

test('extraction approval passes the optional denial reason by button or Enter', () => {
  const { PendingRequest } = load(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  const answers = [];
  let renderer;
  act(() => { renderer = create(h(PendingRequest, { pending: { type: 'permission_request', requestId: 'deny-reason', toolName: 'bash', arguments: {} }, answer: body => answers.push(body), busy: false })); });
  const input = () => renderer.root.findByProps({ className: 'agent-perm__reason-input' });
  act(() => input().props.onChange({ target: { value: '  Read files only  ' } }));
  act(() => renderer.root.findAllByType('button').find(button => button.children.includes('拒绝')).props.onClick());
  assert.deepEqual(JSON.parse(JSON.stringify(answers[0])), { request_id: 'deny-reason', decision: 'deny', reason: 'Read files only' });
  act(() => input().props.onKeyDown({ key: 'Enter', preventDefault() {} }));
  assert.equal(answers[1].reason, 'Read files only');
  act(() => renderer.update(h(PendingRequest, { pending: { type: 'permission_request', requestId: 'deny-reason', toolName: 'bash' }, answer: body => answers.push(body), busy: true })));
  act(() => input().props.onKeyDown({ key: 'Enter', preventDefault() {} }));
  assert.equal(answers.length, 2);
  act(() => renderer.unmount());
});

test('consecutive extraction retries update the shared retry row and discard failed partial output in live and replayed turns', () => {
  const { reduceEvents } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const events = [
    { type: 'user_message', text: 'Inspect fixture' },
    { type: 'assistant_message', messageId: 'intro', text: 'Inspecting', stopReason: 'toolUse' },
    { type: 'tool_start', toolCallId: 'inspect', toolName: 'read', arguments: {} },
    { type: 'tool_end', toolCallId: 'inspect', text: 'verified' },
    { type: 'assistant_start', messageId: 'failed-1' },
    { type: 'thinking_delta', messageId: 'failed-1', text: 'discarded thinking' },
    { type: 'assistant_delta', messageId: 'failed-1', text: 'discarded reply' },
    { type: 'llm_retry_start', messageId: 'failed-1', attempt: 1, max_attempts: 10, delay_ms: 1000, ts: 100, code: 'TIMEOUT', reason: 'first failure' },
    { type: 'llm_retry_end', messageId: 'failed-1', attempt: 1 },
    { type: 'assistant_start', messageId: 'failed-2' },
    { type: 'assistant_delta', messageId: 'failed-2', text: 'discarded second reply' },
    { type: 'llm_retry_start', messageId: 'failed-2', attempt: 2, max_attempts: 10, delay_ms: 2000, ts: 200, code: 'PROVIDER_ERROR', reason: 'second failure' },
  ].map((event, index) => ({ ...event, seq: index + 1 }));
  const build = createExtractionTimelineBuilder();
  let rows = [], firstRetry;
  for (const event of events) {
    const previous = JSON.stringify(rows);
    const updated = reduceEvents(rows, [event]);
    assert.equal(JSON.stringify(rows), previous);
    rows = updated;
    const groups = build(rows, true, null);
    assert.deepEqual(groups, extractionTimeline(reduceEvents([], events.slice(0, event.seq)), true, null));
    if (event.type === 'llm_retry_start') {
      const retries = groups[1].items.filter(item => item.kind === 'retry');
      assert.equal(retries.length, 1);
      assert.equal(retries[0].attempt, event.attempt);
      assert.equal(retries[0].retryDone, false);
      if (!firstRetry) firstRetry = retries[0];
      assert.equal(retries[0].id, firstRetry.id);
      assert.equal(groups[1].items.map(item => item.kind).join(','), 'content,tool,retry');
      assert.equal(groups[1].items[0].content, 'Inspecting');
    }
  }
  const live = build(rows, true, null)[1];
  const item = live.items.at(-1);
  assert.equal(item.retryStartedAtMs, 200000);
  assert.equal(item.retryDelayMs, 2000);
  assert.equal(item.retryReason, 'second failure');
  const componentLoad = createTypeScriptLoader({}, {
    '../../../components/PageActivity': { usePageActive: () => false },
  });
  const { AgentGroupView } = componentLoad(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(AgentGroupView, { group: live, isLive: true, projectDir: '', persistKey: 'retry-parity' })); });
  assert.equal(renderer.root.findAllByProps({ className: 'agent-retry-note is-live' }).length, 1);
  assert.match(renderer.root.findByProps({ className: 'agent-retry-note__count' }).children.join(''), /2\/10/);
  assert.match(renderer.root.findByProps({ className: 'agent-retry-note__reason' }).children.join(''), /second failure/);
  const end = { type: 'llm_retry_end', seq: 13, messageId: 'failed-2', attempt: 2 };
  rows = reduceEvents(rows, [end]);
  const done = build(rows, true, null)[1];
  act(() => renderer.update(h(AgentGroupView, { group: done, isLive: true, projectDir: '', persistKey: 'retry-parity' })));
  assert.equal(renderer.root.findAllByProps({ className: 'agent-retry-note' }).length, 1);
  assert.equal(done.items.at(-1).retryDone, true);
  assert.match(renderer.root.findByProps({ className: 'agent-retry-note__text' }).children[0], /已重试/);
  act(() => renderer.unmount());
  const nextRequest = reduceEvents(rows, [
    { type: 'tool_start', seq: 14, toolCallId: 'next-tool', toolName: 'read' },
    { type: 'tool_end', seq: 15, toolCallId: 'next-tool', text: 'done' },
    { type: 'llm_retry_start', seq: 16, messageId: 'next-failure', attempt: 1, max_attempts: 10 },
  ]);
  assert.equal(extractionTimeline(nextRequest, true, null)[1].items.filter(item => item.kind === 'retry').length, 2);
  assert.ok(extractionTimeline(nextRequest, false, null)[1].items.filter(item => item.kind === 'retry').every(item => item.retryDone));
});

test('work totals include unloaded history and stay stable as split rows are restored', () => {
  const { reduceEvents, prependHistory } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const events = [
    { type: 'user_message', seq: 1, text: 'Inspect fixture' },
    { type: 'thinking_message', seq: 2, messageId: 'intro', text: 'Plan' },
    { type: 'assistant_message', seq: 3, messageId: 'intro', text: 'Inspecting', stopReason: 'toolUse' },
    { type: 'tool_start', seq: 4, toolCallId: 'bash', toolName: 'bash', arguments: { command: 'synthetic' } },
    { type: 'tool_end', seq: 5, toolCallId: 'bash', text: 'done' },
    { type: 'thinking_message', seq: 6, messageId: 'final', text: 'Verified' },
    { type: 'assistant_message', seq: 7, messageId: 'final', text: 'Finished', stopReason: 'stop' },
  ];
  const prefix = { anchor: 'user-1', item_keys: ['reasoning:intro', 'content:intro', 'tool:bash', 'reasoning:final'] };
  const assertTotal = rows => {
    const group = extractionTimeline(rows, false, null, prefix).find(group => group.type === 'activity');
    assert.equal(group.id, 'extract-user-1');
    assert.equal((group.hiddenStepCount || 0) + group.items.filter(item => !item.final).length, 4);
    let renderer;
    act(() => { renderer = create(h(load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url)).AgentGroupView,
      { group, isLive: false, projectDir: '', persistKey: 'history-count' })); });
    assert.match(renderer.root.findByProps({ className: 'agent-activity__meta' }).children.join(''), /4/);
    act(() => renderer.unmount());
  };
  for (let start = 1; start < events.length; start += 1) {
    const rows = reduceEvents([], events.slice(start));
    assertTotal(rows);
    const restored = prependHistory(rows, reduceEvents([], events.slice(0, start)));
    assert.deepEqual(JSON.parse(JSON.stringify(restored)), JSON.parse(JSON.stringify(reduceEvents([], events))));
    assertTotal(restored);
  }
});

test('loading more than 1000 history rows retains the oldest and latest messages', () => {
  const { reduceEvents, prependHistory } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const events = Array.from({ length: 1200 }, (_, index) => ({ type: 'user_message', seq: index + 1, text: String(index) }));
  const recent = reduceEvents([], events.slice(1100));
  const rows = prependHistory(recent, reduceEvents([], events.slice(0, 1100)));
  assert.equal(rows.length, 1200);
  assert.equal(rows[0].id, 'user-1');
  assert.equal(rows.at(-1).id, 'user-1200');
  assert.equal(rows.at(-1), recent.at(-1));
});

test('native read images render with the extraction session asset URL after replay', () => {
  const { ToolRow } = load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
  const image = { id: `${'a'.repeat(64)}.webp`, name: 'fixture.webp' };
  const group = extractionTimeline([user, { ...tool, tool: 'read', done: true, images: [image] }], false, null)[1];
  assert.deepEqual(group.items[0].images, [image]);
  let renderer;
  act(() => { renderer = create(h(ToolRow, { item: group.items[0], persistKey: 'native-image', imageScope: { sessionId: 'session', extraction: true }, autoExpand: false })); });
  assert.equal(renderer.root.findByProps({ className: 'agent-tool__header' }).props['aria-expanded'], false);
  assert.equal(renderer.root.findAllByType('img').length, 0);
  act(() => renderer.root.findByProps({ className: 'agent-tool__header' }).props.onClick());
  const source = renderer.root.findByType('img').props.src;
  assert.match(source, /agent=extraction/);
  assert.match(source, /image_id=a+\.webp/);
  act(() => renderer.unmount());
});

test('all extraction tool details stay folded through approvals, streamed results and grouped image reads', () => {
  const componentLoad = createTypeScriptLoader({ URLSearchParams, window: { setTimeout, clearTimeout,
    addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: true }) } }, {
    '../../components/PageActivity': { usePageActive: () => false },
    '../../../components/PageActivity': { usePageActive: () => false },
  });
  const { Conversation } = componentLoad(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  const { ToolRow } = componentLoad(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
  const names = ['read', 'read', 'bash', 'edit', 'write', 'run_python', 'ask_user', 'read_history_archive'];
  const rows = [user, ...names.map((name, index) => ({ ...tool, id: `fold-${index}`, tool: name,
    done: index !== 2, text: index === 2 ? 'live command output' : 'result',
  }))];
  const props = { rows, sessionId: 'default-folded-tools', running: true, status: 'awaiting_permission', older: false,
    loadOlder() {}, pending: { type: 'permission_request', requestId: 'approval', toolCallId: 'fold-2' } };
  let renderer;
  const mount = () => { renderer = create(h(Conversation, props)); };
  act(mount);
  const readGroup = () => renderer.root.findByProps({ className: 'agent-tool__header agent-read-group__header' });
  assert.equal(readGroup().props['aria-expanded'], false);
  act(() => readGroup().props.onClick());
  const headers = () => renderer.root.findAllByProps({ className: 'agent-tool__header' });
  assert.equal(headers().length, names.length);
  assert.ok(headers().every(header => header.props['aria-expanded'] === false));
  assert.ok(renderer.root.findAllByType(ToolRow).every(row => row.props.autoExpand === false));
  const bash = () => renderer.root.findByProps({ 'data-agent-tool-id': 'fold-2' }).findByProps({ className: 'agent-tool__header' });
  act(() => bash().props.onClick());
  assert.equal(bash().props['aria-expanded'], true);
  const image = { id: `${'b'.repeat(64)}.png`, name: 'late-image.png' };
  props.rows = rows.map(row => row.id === 'fold-0' ? { ...row, images: [image] }
    : row.id === 'fold-2' ? { ...row, done: true, error: true, text: 'command failed' } : row);
  props.pending = null;
  act(() => renderer.update(h(Conversation, props)));
  assert.equal(bash().props['aria-expanded'], true);
  assert.equal(headers()[0].props['aria-expanded'], false);
  assert.equal(renderer.root.findAllByType('img').length, 0);
  act(() => renderer.unmount());
  act(mount);
  assert.equal(bash().props['aria-expanded'], true);
  assert.equal(headers()[0].props['aria-expanded'], false);
  act(() => renderer.unmount());
});

test('extraction approval arguments start folded while decision buttons remain available', () => {
  const { PendingRequest } = load(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(PendingRequest, { pending: { type: 'permission_request', requestId: 'approval',
    toolName: 'run_python', arguments: { code: 'print("fixture")' } }, busy: false, answer() {} })); });
  assert.equal(Boolean(renderer.root.findByType('details').props.open), false);
  assert.equal(renderer.root.findAllByType('button').length, 3);
  act(() => renderer.unmount());
});

test('extraction approvals show intent and the localized tool name before opening arguments', () => {
  const { PendingRequest } = load(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(PendingRequest, { pending: { type: 'permission_request', requestId: 'intent-approval',
    toolName: 'run_python', arguments: { code: 'print("fixture")', reason: '验证剧本提取结果' } }, busy: false, answer() {} })); });
  assert.equal(renderer.root.findByProps({ className: 'agent-perm__reason' }).children.join(''), '原因：验证剧本提取结果');
  assert.equal(renderer.root.findByType('summary').children.join(''), '运行 Python');
  assert.equal(Boolean(renderer.root.findByType('details').props.open), false);
  act(() => renderer.unmount());
});

test('intent stays visible in folded extraction tools while running, finished, failed and replayed', () => {
  const { reduceEvents } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const { ToolRow } = load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
  for (const name of ['read', 'write', 'edit', 'bash', 'run_python']) {
    const reason = '检查剧本文件格式';
    const start = { type: 'tool_start', toolCallId: `intent-${name}`, toolName: name, arguments: { reason } };
    for (const state of ['running', 'finished', 'failed']) {
      const events = [start, ...(state === 'running' ? [] : [{ type: 'tool_end', toolCallId: start.toolCallId, text: 'tool output', isError: state === 'failed' }])];
      const replayed = reduceEvents([], events);
      const streamed = events.reduce((rows, event) => reduceEvents(rows, [event]), []);
      assert.deepEqual(streamed, replayed);
      const item = extractionTimeline(replayed, state === 'running', null)[0].items[0];
      let renderer;
      act(() => { renderer = create(h(ToolRow, { item, phase: state === 'running' ? 'running' : undefined,
        persistKey: `intent-${name}-${state}`, autoExpand: false })); });
      const summary = renderer.root.findByProps({ className: 'agent-tool__summary' });
      assert.equal(summary.children.join(''), reason);
      assert.equal(summary.props.title, reason);
      assert.equal(renderer.root.findByProps({ className: 'agent-tool__header' }).props['aria-expanded'], false);
      act(() => renderer.unmount());
    }
  }
});

test('both Agent timelines render localized billion-context tool names', () => {
  const { buildTimeline } = load(new URL('../src/pages/agent/timeline.ts', import.meta.url));
  const { ToolRow } = load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
  for (const [name, label] of [
    ['compress', '压缩上下文'], ['decompress', '还原上下文'],
    ['search_context', '搜索上下文'], ['acp_status', '查看上下文状态'],
  ]) {
    const translation = buildTimeline([{ type: 'tool_call', step: 1, id: name, name, arguments: {} }]);
    const extraction = extractionTimeline([{ id: name, role: 'tool', tool: name, args: {}, text: '' }], true, null);
    for (const [agent, timeline] of [['translation', translation], ['extraction', extraction]]) {
      let renderer;
      act(() => { renderer = create(h(ToolRow, { item: timeline[0].items[0], phase: 'running',
        persistKey: `billion-${agent}-${name}`, autoExpand: false })); });
      assert.equal(renderer.root.findByProps({ className: 'agent-tool__name is-running' }).children.join(''), label);
      act(() => renderer.unmount());
    }
  }
});

test('live and replayed user events render the shared user message bubbles in turn order', () => {
  const { reduceEvents } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const { AgentGroupView } = load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
  const events = [
    { seq: 1, type: 'user_message', text: '检查这个目录\n保留原文件' },
    { seq: 2, type: 'assistant_message', messageId: 'first-answer', text: '已检查', stopReason: 'stop' },
    { seq: 3, type: 'user_message', text: '继续' },
  ];
  const replayed = reduceEvents([], events);
  const streamed = events.reduce((rows, event) => reduceEvents(rows, [event]), []);
  assert.deepEqual(streamed, replayed);
  const groups = extractionTimeline(replayed, true, null);
  assert.equal(groups.map(group => group.type).join(','), 'user,activity,user');
  for (const group of groups.filter(group => group.type === 'user')) {
    let renderer;
    act(() => { renderer = create(h(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'synthetic' })); });
    assert.equal(renderer.root.findByProps({ className: 'agent-bubble__text' }).children.join(''), group.message);
    assert.equal(renderer.root.findByProps({ className: 'agent-bubble agent-bubble--user' }).type, 'div');
    act(() => renderer.unmount());
  }
});

test('stream updates retain historical groups and unchanged tool render models', () => {
  const build = createExtractionTimelineBuilder();
  const rows = [user, { ...tool, done: true }, { id: 'second-user', role: 'user', text: 'Continue' },
    { ...tool, id: 'second-tool' }, { id: 'assistant-live', role: 'assistant', text: 'Working' }];
  const before = build(rows, true, null);
  const after = build([...rows.slice(0, -1), { ...rows.at(-1), text: 'Working on it' }], true, null);
  assert.equal(before[0], after[0]);
  assert.equal(before[1], after[1]);
  assert.equal(before[3].items[0], after[3].items[0]);
  assert.notEqual(before[3].items[1], after[3].items[1]);
});

test('live tool output stays pending; approval and interrupted history use the shared tool phases', () => {
  const rows = [user, tool];
  const original = JSON.stringify(rows);
  let group = extractionTimeline(rows, true, null)[1];
  assert.equal(group.items[0].result, undefined);
  assert.equal(group.items[0].progressText, 'partial output');
  assert.equal(toolRowPhases(group.items, true)[0], 'running');
  group = extractionTimeline(rows, true, { type: 'permission_request', toolCallId: tool.id, requestId: 'approval' })[1];
  assert.equal(toolRowPhases(group.items, true)[0], 'awaiting');
  assert.equal(toolRowPhases(group.items, false)[0], 'stale');
  assert.equal(JSON.stringify(rows), original);
});

test('final replies stay outside collapsed work, earlier narration stays in order, and steps survive prepended history', () => {
  const rows = [user, { id: 'intro', role: 'assistant', text: 'Inspecting files', done: true, stopReason: 'toolUse' },
    { ...tool, done: true, text: 'fixture verified' },
    { id: 'final', role: 'assistant', text: 'The fixture is ready.', thinking: 'Checked the result.', done: true, stopReason: 'stop' }];
  const group = extractionTimeline(rows, false, null)[1];
  assert.equal(group.items.map(item => item.kind).join(','), 'content,tool,reasoning,content');
  assert.equal(group.finalContent.content, 'The fixture is ready.');
  assert.equal(group.items.filter(item => item.final).length, 1);
  assert.equal(group.items[0].final, undefined);
  const restored = extractionTimeline([{ ...user, id: 'older' }, ...rows], false, null).at(-1);
  assert.equal(restored.id, group.id);
  assert.equal(restored.items[1].step, group.items[1].step);
  const partial = extractionTimeline([user, { id: 'partial', role: 'assistant', text: 'Unfinished reply', done: true, partial: true }], false, null)[1];
  assert.equal(partial.finalContent.content, 'Unfinished reply');
});

test('error output stays literal and is not reported as a successful tool result', () => {
  const group = extractionTimeline([user, { ...tool, done: true, error: true, text: '<error>\n# literal stderr' }], false, null)[1];
  assert.equal(group.items[0].ok, false);
  assert.equal(group.items[0].error, '<error>\n# literal stderr');
  assert.equal(group.items[0].resultFormat, 'text');
});

test('context rings use the SDK context measurement, never request totals or stale values after compaction', () => {
  assert.equal(extractionContextUsage({ type: 'usage', total: 25000 }), null);
  assert.equal(extractionContextUsage({ type: 'context_usage', used_tokens: null, window_tokens: 128000 }), null);
  assert.equal(extractionContextUsage({ type: 'context_usage', used_tokens: -1, window_tokens: 128000 }), null);
  assert.equal(extractionContextUsage({ type: 'context_usage', used_tokens: NaN, window_tokens: 128000 }), null);
  const usage = extractionContextUsage({ type: 'context_usage', used_tokens: 32000, window_tokens: 128000 });
  const { ContextMeter } = load(new URL('../src/pages/agent/composer.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(ContextMeter, { usage })); });
  assert.match(renderer.root.findByProps({ role: 'img' }).props.title, /25\.0%/);
  act(() => renderer.unmount());
});

test('composer preserves IME and multiline shortcuts and keeps stop available while a draft is edited', () => {
  let sends = 0, stops = 0, draft = '';
  const componentLoad = createTypeScriptLoader({ window: { matchMedia: () => ({ matches: true }) } }, {
    '../agent/ComposerSelect': { ComposerSelect: () => null },
  });
  const { ExtractionComposer } = componentLoad(new URL('../src/pages/extraction/Composer.tsx', import.meta.url));
  const props = { draft: 'Inspect a fixture', setDraft: text => { draft = text; }, textarea: { current: null },
    session: { game_dir: 'fixture', status: 'idle' }, running: false, busy: false, ready: true,
    profile: 'synthetic', setProfile() {}, models: ['synthetic'], mode: 'ask', setMode() {},
    send: () => sends++, stop: () => stops++, chooseSource() {}, configure() {} };
  let renderer;
  act(() => { renderer = create(h(ExtractionComposer, props)); });
  const key = extra => renderer.root.findByType('textarea').props.onKeyDown({ key: 'Enter', nativeEvent: {}, preventDefault() {}, ...extra });
  key({ nativeEvent: { isComposing: true } }); key({ shiftKey: true }); key({ ctrlKey: true }); key({ metaKey: true });
  assert.equal(sends, 0);
  key({}); assert.equal(sends, 1);
  act(() => renderer.update(h(ExtractionComposer, { ...props, running: true })));
  key({}); assert.equal(sends, 2);
  assert.match(renderer.root.findByType('textarea').props.placeholder, /本轮做完自动/);
  act(() => renderer.root.findByType('textarea').props.onChange({ target: { value: 'next task' } }));
  assert.equal(draft, 'next task');
  const stop = renderer.root.findByProps({ className: 'agent-composer__stop' });
  assert.equal(stop.props.disabled, false);
  act(() => stop.props.onClick()); assert.equal(stops, 1);
  act(() => renderer.update(h(ExtractionComposer, { ...props, running: true, session: { game_dir: 'fixture', status: 'stopping' } })));
  assert.equal(renderer.root.findByProps({ className: 'agent-composer__stop' }).props.disabled, true);
  act(() => renderer.unmount());
});

test('model labels match translation profiles and permissions remain selectable during execution', () => {
  const profiles = { primary: { 'OpenAI-Compatible': { tokens: [{ modelName: 'actual-model' }] } }, empty: {} };
  const picks = [];
  const componentLoad = createTypeScriptLoader({
    window: { matchMedia: () => ({ matches: true }) },
    document: { documentElement: {}, addEventListener() {}, removeEventListener() {} },
  }, { '../../lib/api': { getBackendProfile: name => profiles[name] } });
  const { ExtractionComposer } = componentLoad(new URL('../src/pages/extraction/Composer.tsx', import.meta.url));
  const { ComposerSelect } = componentLoad(new URL('../src/pages/agent/ComposerSelect.tsx', import.meta.url));
  const props = { draft: '', setDraft() {}, textarea: { current: null }, session: { game_dir: 'fixture', status: 'running' },
    running: true, busy: false, ready: true, profile: 'primary', setProfile: value => picks.push(value), models: ['primary', 'empty'],
    mode: 'auto', setMode: value => picks.push(value), send() {}, stop() {}, chooseSource() {}, configure() {} };
  let renderer;
  act(() => { renderer = create(h(ExtractionComposer, props)); });
  let [model, permission] = renderer.root.findAllByType(ComposerSelect);
  assert.equal(model.props.options[0].label, 'primary/actual-model');
  assert.equal(model.props.options[1].label, 'empty');
  assert.equal(model.props.disabled, true);
  assert.equal(permission.props.disabled, false);
  assert.equal(permission.props.options.map(option => option.value).join(','), 'ask,accept-edits,auto,auto-quiet');
  act(() => permission.findByType('button').props.onClick());
  const quiet = permission.findAllByProps({ role: 'menuitemradio' }).find(item => item.props.children[0].props.children[0] === '全自动-零打断');
  assert.ok(quiet);
  act(() => quiet.props.onClick());
  assert.deepEqual(picks, ['auto-quiet']);
  profiles.primary['OpenAI-Compatible'].tokens[0].modelName = 'updated-model';
  act(() => renderer.update(h(ExtractionComposer, { ...props, running: false })));
  [model] = renderer.root.findAllByType(ComposerSelect);
  assert.equal(model.props.options[0].label, 'primary/updated-model');
  act(() => model.findByType('button').props.onClick());
  act(() => model.findAllByProps({ role: 'menuitemradio' })[1].props.onClick());
  assert.deepEqual(picks, ['auto-quiet', 'empty']);
  act(() => renderer.unmount());
});

test('extraction questions reuse the translation choice card and submit the selected answer', () => {
  const { PendingRequest } = load(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  let answer, renderer;
  const pending = { type: 'ask_request', requestId: 'question', toolCallId: 'tool', questions: [
    { question: 'Choose a fixture', options: ['A', 'B'], recommended: 'B' },
  ] };
  act(() => { renderer = create(h(PendingRequest, { pending, busy: false, answer: value => { answer = value; } })); });
  assert.equal(renderer.root.findAllByProps({ className: 'agent-ask__tag' }).length, 1);
  act(() => renderer.root.findAllByProps({ role: 'radio' })[1].props.onClick());
  assert.deepEqual(JSON.parse(JSON.stringify(answer)), { request_id: 'question', answers: [['B']] });
  act(() => renderer.unmount());
});

test('extraction model picker exposes the main Agent default as a distinct selectable binding', () => {
  let selected;
  const Picker = () => null;
  const componentLoad = createTypeScriptLoader({}, {
    '../../lib/api': { getBackendProfile: () => ({ 'OpenAI-Compatible': { tokens: [{ modelName: 'actual-model' }] } }) },
    '../agent/ComposerSelect': { ComposerSelect: Picker },
  });
  const { ExtractionComposer } = componentLoad(new URL('../src/pages/extraction/Composer.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(ExtractionComposer, { draft: '', setDraft() {}, textarea: { current: null },
    running: false, busy: false, ready: true, profile: 'primary', profileSelection: '', defaultProfile: 'primary',
    setProfile: value => { selected = value; }, models: ['primary'], mode: 'auto', setMode() {},
    send() {}, stop() {}, chooseSource() {}, configure() {} })); });
  const picker = renderer.root.findAllByType(Picker)[0];
  assert.equal(picker.props.value, '');
  assert.match(picker.props.options[0].label, /主 Agent 默认.*primary\/actual-model/);
  assert.equal(picker.props.options[0].value, '');
  assert.equal(picker.props.options[1].value, 'primary');
  act(() => picker.props.onChange(''));
  assert.equal(selected, '');
  act(() => renderer.unmount());
});
