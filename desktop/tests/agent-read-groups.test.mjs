import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('merged reads group while writes and waiting remain separate', () => {
  const items = [
    ['dictionary', { action: 'read' }], ['input', { action: 'search' }],
    ['dictionary', { action: 'set' }], ['problems', { action: 'list', target: 'filter' }],
    ['problems', { action: 'delete', target: 'filter' }], ['translation', { action: 'wait' }],
  ].map(([name, args], index) => ({ kind: 'tool', id: String(index), step: index, name, arguments: args }));
  const entries = activityEntries(items, false);
  assert.deepEqual(Array.from(entries, entry => [Array.from(entry.indices), entry.reading]), [
    [[0, 1], true], [[2], false], [[3], true], [[4], false], [[5], false],
  ]);
});

const load = createTypeScriptLoader();
const { activityEntries } = load(new URL('../src/pages/agent/readGroups.ts', import.meta.url));
const read = (step, extra = {}) => ({ kind: 'tool', step, id: `id-${step}`, name: 'read_input_file', ok: true, ...extra });

test('cache searches and replacement previews group as reads while execution stays separate', () => {
  const items = [
    read(1, { name: 'search_replace_cache', arguments: { action: 'search' } }),
    read(2, { name: 'search_replace_cache', arguments: { action: 'replace', dry_run: true } }),
    read(3, { name: 'search_replace_cache', arguments: { action: 'replace' } }),
  ];
  const entries = activityEntries(items, false);
  assert.deepEqual(Array.from(entries, entry => [Array.from(entry.indices), entry.reading]), [
    [[0, 1], true], [[2], false],
  ]);
});

test('consecutive known reads group without reordering or mutating events', () => {
  const items = [read(1), read(2), read(3, { name: 'save_dict' }), read(4), read(5)];
  const before = JSON.stringify(items);
  const entries = activityEntries(items, true);
  assert.deepEqual(Array.from(entries, entry => Array.from(entry.indices)), [[0, 1], [2], [3, 4]]);
  assert.equal(entries[0].live, false);
  assert.equal(entries[2].live, true);
  assert.equal(JSON.stringify(items), before);
});

test('narration, reasoning, writes, approvals, failures, maintenance and unknown tools are boundaries', () => {
  for (const boundary of [
    { kind: 'content', step: 3, content: 'narration' },
    { kind: 'reasoning', step: 3, content: 'thinking' },
    { kind: 'compact', step: 3 }, { kind: 'retry', step: 3 },
    read(3, { name: 'create_dict_file' }), read(3, { name: 'get_unknown' }),
    read(3, { name: 'manage_problem_filter', arguments: { action: 'add' } }),
    read(3, { permission: { id: 'permission' } }), read(3, { ok: false, error: 'failure' }),
    read(3, { error: 'failure without ok' }),
  ]) {
    const entries = activityEntries([read(1), read(2), boundary, read(4), read(5)], true);
    assert.deepEqual(Array.from(entries, entry => Array.from(entry.indices)), [[0, 1], [2], [3, 4]], JSON.stringify(boundary));
    assert.equal(entries[1].reading, false);
  }
});

test('retired read aliases group while retired write alias stays separate', () => {
  const entries = activityEntries([
    read(1, { name: 'list_transl_cache' }), read(2, { name: 'search_transl_cache' }),
    read(3, { name: 'create_dict_file' }),
  ], false);
  assert.equal(entries[0].indices.length, 2);
  assert.equal(entries[1].reading, false);
});

test('live read stage survives completed-child gaps and retains first-call identity as calls stream in', () => {
  const before = activityEntries([read(1), read(2)], true)[0];
  const after = activityEntries([read(1), read(2), read(3, { ok: undefined })], true)[0];
  assert.equal(before.live, true);
  assert.equal(after.live, true);
  assert.equal(before.key, after.key);
  assert.equal(activityEntries([read(1), read(2)], false)[0].live, false);
  assert.equal(activityEntries([read(1), read(2), { kind: 'content', step: 3, final: true }], true)[0].live, false);
  assert.equal(activityEntries([read(1)], true)[0].indices.length, 1);
});

test('consecutive extraction commands group apart from reads, with failures and approvals left alone', () => {
  const run = (step, extra = {}) => read(step, { name: 'bash', arguments: { command: 'ls' }, ...extra });
  const items = [read(1, { name: 'read' }), read(2, { name: 'read' }), run(3), run(4, { name: 'run_python' }), run(5, { ok: undefined }),
    run(6, { ok: false, error: 'exit 1' }), run(7, { permission: { id: 'permission' } }), run(8), read(9, { name: 'write' }), run(10)];
  const entries = activityEntries(items, true);
  assert.deepEqual(Array.from(entries, entry => [Array.from(entry.indices), entry.group]), [
    [[0, 1], 'read'], [[2, 3, 4], 'command'], [[5], undefined], [[6], undefined], [[7], 'command'], [[8], undefined], [[9], 'command'],
  ]);
  assert.equal(entries[1].reading, false);
  assert.equal(entries[1].live, false);
  assert.equal(entries.at(-1).live, true);
  assert.equal(activityEntries([run(1), run(2)], true)[0].live, true);
  assert.equal(activityEntries([run(1), run(2)], true)[0].key, activityEntries([run(1), run(2), run(3)], true)[0].key);
});
