import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { activityEntries } = load(new URL('../src/pages/agent/readGroups.ts', import.meta.url));
const read = (step, extra = {}) => ({ kind: 'tool', step, id: `id-${step}`, name: 'read_input_file', ok: true, ...extra });

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
