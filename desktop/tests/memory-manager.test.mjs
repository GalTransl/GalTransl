import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const entry = (id, scope, content = 'SJIS编码 <script>literal text</script>') => ({
  id, scope, content, importance: scope === 'global' ? 'critical' : 'normal',
  created_at: '2026-11-02T10:00:00+00:00', updated_at: '2026-11-02T10:30:00+00:00',
});
const deferred = () => {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
};
const setup = (fetchMemories, deleteMemory = async () => ({ success: true })) => {
  const load = createTypeScriptLoader({}, { '../lib/api': { fetchMemories, deleteMemory } });
  return load(new URL('../src/components/MemoryManager.tsx', import.meta.url)).MemoryManager;
};
const button = (view, text) => view.root.findAllByType('button').find(node => node.children.join('') === text);

test('complete memory entries stay visible until the confirmed deletion succeeds', async () => {
  const content = '完整记忆'.repeat(250);
  const global = entry('global-one', 'global', content);
  const project = entry('mem-one', 'project');
  const saving = deferred();
  const calls = [];
  const Component = setup(async projectDir => {
    assert.equal(projectDir, 'C:/game');
    return { entries: [global, project] };
  }, (projectDir, id) => { calls.push({ projectDir, id }); return saving.promise; });
  let view;
  await act(async () => { view = create(createElement(Component, { backendUrl: 'local', projectDir: 'C:/game' })); });
  try {
    const contentNodes = view.root.findAllByProps({ className: 'memory-manager__content' });
    assert.deepEqual(contentNodes.map(node => node.children.join('')), [content, project.content]);
    assert.equal(view.root.findAllByType('script').length, 0);
    assert.equal(view.root.findAllByType('li').length, 2);
    act(() => button(view, '删除记忆').props.onClick());
    assert.equal(calls.length, 0);
    assert.match(view.root.findByProps({ className: 'memory-manager__actions' }).findByType('p').children.join(''), /所有/);
    act(() => button(view, '确认删除').props.onClick());
    assert.equal(JSON.stringify(calls), '[{"projectDir":"C:/game","id":"global-one"}]');
    assert.equal(view.root.findAllByType('li').length, 2);
    assert.equal(button(view, '确认删除').props.disabled, true);
    await act(async () => saving.resolve({ success: true }));
    assert.equal(view.root.findAllByType('li').length, 1);
    assert.equal(view.root.findByType('code').children.join(''), 'mem-one');
  } finally { act(() => view.unmount()); }
});

test('failed loading and deletion remain visible and can be retried', async () => {
  let loads = 0, deletes = 0;
  const Component = setup(async () => {
    if (++loads === 1) throw new Error('load failed');
    return { entries: [entry('global-one', 'global')] };
  }, async () => {
    if (++deletes === 1) throw new Error('delete failed');
    return { success: true };
  });
  let view;
  await act(async () => { view = create(createElement(Component, { backendUrl: 'local', projectDir: '' })); });
  try {
    assert.match(view.root.findByProps({ role: 'alert' }).children.join(''), /load failed/);
    await act(async () => button(view, '刷新列表').props.onClick());
    act(() => button(view, '删除记忆').props.onClick());
    await act(async () => button(view, '确认删除').props.onClick());
    assert.match(view.root.findByProps({ role: 'alert' }).children.join(''), /delete failed/);
    assert.equal(view.root.findAllByType('li').length, 1);
    await act(async () => button(view, '确认删除').props.onClick());
    assert.equal(view.root.findAllByType('li').length, 0);
  } finally { act(() => view.unmount()); }
});

test('changing the selected project discards a late list response', async () => {
  const first = deferred();
  const Component = setup(projectDir => projectDir === 'first' ? first.promise
    : Promise.resolve({ entries: [entry('mem-second', 'project')] }));
  let view;
  await act(async () => { view = create(createElement(Component, { backendUrl: 'local', projectDir: 'first' })); });
  try {
    await act(async () => view.update(createElement(Component, { backendUrl: 'local', projectDir: 'second' })));
    await act(async () => first.resolve({ entries: [entry('mem-first', 'project')] }));
    assert.equal(view.root.findByType('code').children.join(''), 'mem-second');
  } finally { act(() => view.unmount()); }
});

test('global memory approval displays complete content and offers individual decisions', () => {
  const load = createTypeScriptLoader();
  const { PermissionCard } = load(new URL('../src/pages/agent/rows/PermissionCard.tsx', import.meta.url));
  const content = '记忆正文'.repeat(250);
  const decisions = [];
  let view;
  act(() => { view = create(createElement(PermissionCard, {
    item: { kind: 'tool', step: 1, name: 'mnemon_remember', permission: {
      name: 'mnemon_remember', label: '保存记忆', risk: 'global-memory', mode: 'auto', arguments: { content, scope: 'global' },
    } }, submitting: false, error: null, onDecide: decision => decisions.push(decision),
  })); });
  try {
    assert.equal(view.root.findByType('pre').children.join(''), content);
    assert.equal(view.root.findAllByType('button').length, 2);
    view.root.findAllByType('button').forEach(node => act(() => node.props.onClick()));
    assert.deepEqual(decisions, ['allow-once', 'deny']);
  } finally { act(() => view.unmount()); }
});
