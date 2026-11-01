import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const edit = (id, file = 'a.json', turn = 'turn') => ({ change_id: id, at: 1, session_id: 'session', turn_id: turn,
  actor_id: 'main', actor_label: '主 Agent', operation: 'patch_transl_cache', reason: '修订', status: 'applied',
  updated: 1, reverts: [], files: [{ filename: file, updated: 1, deleted: false, created: false }] });

const load = createTypeScriptLoader();
const { buildTimeline } = load(new URL('../src/pages/agent/timeline.ts', import.meta.url));

test('turn summaries follow the final reply once and include every child edit', () => {
  const events = [
    { type: 'user_message', step: 1, message: '修改' },
    { type: 'cache_edit', step: 2, edit: edit('main') },
    { type: 'cache_edit', step: 3, edit: { ...edit('child', 'b.json'), actor_id: 'child' } },
    { type: 'content', step: 4, content: '完成' },
    { type: 'finish', step: 5, summary: '完成' },
    { type: 'cache_edit_summary', step: 6, turn_id: 'turn', edits: [edit('main')] },
  ];
  const groups = buildTimeline(events);
  assert.deepEqual(Array.from(groups, group => group.type), ['user', 'activity', 'cache-edits']);
  assert.equal(groups[1].finalContent.content, '完成');
  assert.equal(groups[1].items[0].final, true);
  assert.deepEqual(Array.from(groups[2].edits, row => row.change_id), ['main', 'child']);
  assert.deepEqual(JSON.parse(JSON.stringify(buildTimeline(structuredClone(events)))), JSON.parse(JSON.stringify(groups)));
});

test('late child edits update their original stopped turn without closing the new turn', () => {
  const events = [
    { type: 'stopped', step: 1 },
    { type: 'cache_edit_summary', step: 2, turn_id: 'old', edits: [] },
    { type: 'user_message', step: 3, message: '继续' },
    { type: 'content_delta', step: 4, delta: '新回复' },
    { type: 'cache_edit', step: 5, edit: edit('late', 'b.json', 'old') },
  ];
  const groups = buildTimeline(events);
  assert.deepEqual(Array.from(groups, group => group.type), ['stopped', 'cache-edits', 'user', 'activity']);
  assert.equal(groups[1].edits[0].change_id, 'late');
  assert.equal(groups[3].items[0].streaming, true);
});

async function fixture({ undo, read } = {}) {
  const calls = [];
  const edits = [edit('one'), edit('two', 'b.json')];
  const loader = createTypeScriptLoader({ window: { dispatchEvent() {} }, CustomEvent: class {} }, {
    '../../../components/AgentCacheRef': { invalidateCacheFilesForToolResult() {} },
    '../../../lib/api': {
      listCacheHistory: read || (async () => ({ items: edits, total: 2, returned: 2, has_more: false })),
      diffCacheHistory: async (dir, ids, filename) => {
        calls.push({ action: 'diff', dir, ids, filename });
        return { items: [{ filename, diff: '-旧译文\n+新译文', added: 1, removed: 1, updated: 1 }] };
      },
      undoCacheHistory: undo || (async (dir, ids) => { calls.push({ action: 'undo', dir, ids }); return { status: 'reverted', updated: 2, conflicts: [] }; }),
    },
  });
  const { CacheEditSummary } = loader(new URL('../src/pages/agent/rows/CacheEditSummary.tsx', import.meta.url));
  let renderer;
  await act(async () => { renderer = create(createElement(CacheEditSummary, { edits, projectDir: 'project', sessionId: 'session', turnId: 'turn' })); });
  return { renderer, calls, button: text => renderer.root.findAllByType('button').find(button => button.children.includes(text)) };
}

test('collapsed summaries load history and reveal exact diffs before undoing every selected edit', async () => {
  const f = await fixture();
  try {
    assert.equal(f.renderer.root.findAllByType('pre').length, 0);
    await act(async () => f.renderer.root.findByProps({ 'aria-expanded': false }).props.onClick());
    await act(async () => f.button('查看差异').props.onClick());
    assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0])), { action: 'diff', dir: 'project', ids: ['one', 'two'], filename: 'a.json' });
    assert.equal(f.renderer.root.findByProps({ className: 'is-added' }).children[0], '+新译文');
    await act(async () => f.button('撤销').props.onClick());
    assert.deepEqual(Array.from(f.calls[1].ids), ['one', 'two']);
    assert.equal(f.button('已撤销').props.disabled, true);
  } finally { act(() => f.renderer.unmount()); }
});

test('conflicting undo remains actionable and displays the affected file and field', async () => {
  const f = await fixture({ undo: async () => ({ status: 'conflict', updated: 0, conflicts: [{ file: 'a.json', index: 1, field: 'pre_dst', reason: '已改变' }] }) });
  try {
    await act(async () => f.button('撤销').props.onClick());
    assert.equal(f.button('撤销').props.disabled, false);
    const alert = f.renderer.root.findByProps({ role: 'alert' });
    assert.match(JSON.stringify(alert.children.map(child => typeof child === 'string' ? child : child.children)), /a.json/);
  } finally { act(() => f.renderer.unmount()); }
});

test('restored summaries show durable undo state', async () => {
  const f = await fixture({ read: async () => ({ items: [{ ...edit('one'), reverted_by: 'undo' }], has_more: false }) });
  try { assert.equal(f.button('已撤销').props.disabled, true); }
  finally { act(() => f.renderer.unmount()); }
});

test('undoing a turn includes its own reversals so cancelled edits are not reapplied', async () => {
  const f = await fixture({ read: async () => ({ items: [{ ...edit('one'), reverted_by: 'two' }, { ...edit('two'), operation: 'revert', reverts: ['one'] }], has_more: false }) });
  try {
    await act(async () => f.button('撤销').props.onClick());
    assert.deepEqual(Array.from(f.calls[0].ids), ['one', 'two']);
  } finally { act(() => f.renderer.unmount()); }
});
