import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const { createCacheProblemFilter, entriesMatch, mergeCacheEntries } = createTypeScriptLoader()(new URL('../src/lib/cacheEntries.ts', import.meta.url));

test('restoring original text or recovering a deleted entry clears dirty state', () => {
  const clean = [{ index: 1, pre_dst: 'cat', name: ['A', 'B'] }];
  assert.equal(entriesMatch([{ ...clean[0], pre_dst: 'kitten', deleted: false }], clean), false);
  assert.equal(entriesMatch([{ ...clean[0], deleted: false }], clean), true);
  assert.equal(entriesMatch(clean, [{ ...clean[0], deleted: false }]), true);
  assert.equal(entriesMatch([{ ...clean[0], deleted: true }], clean), false);
  assert.equal(entriesMatch([{ ...clean[0], name: ['B', 'A'] }], clean), false);
  assert.equal(entriesMatch([], clean), false);
});

test('editing one cache entry preserves other card identities with problem filters enabled', () => {
  const filter = createCacheProblemFilter(['ignored']);
  const rows = [{ index: 1, problem: 'ignored, visible' }, { index: 2, problem: '' }];
  const first = rows.map(filter);
  const edited = [{ ...rows[0], pre_dst: 'new translation' }, rows[1]].map(filter);
  assert.equal(first[0].problem, 'visible');
  assert.equal(filter(rows[0]), first[0]);
  assert.notEqual(edited[0], first[0]);
  assert.equal(edited[1], first[1]);
  assert.equal(createCacheProblemFilter([])(rows[0]), rows[0]);
  assert.equal(createCacheProblemFilter(['visible'])(rows[0]).problem, 'ignored');
});

test('page revalidation reuses unchanged cards while accepting external edits and reordered rows', () => {
  const previous = [{ index: 1, name: ['A', 'B'], pre_dst: 'cat' }, { index: 2, problem: 'visible', proofread_comment: 'old' }];
  const snapshot = () => JSON.parse(JSON.stringify(previous));
  assert.equal(mergeCacheEntries(previous, snapshot()), previous);
  const changed = snapshot();
  changed[1].proofread_comment = 'new external comment';
  const merged = mergeCacheEntries(previous, changed);
  assert.equal(merged[0], previous[0]);
  assert.equal(merged[1], changed[1]);
  const reordered = mergeCacheEntries(previous, snapshot().reverse());
  assert.equal(reordered[0], previous[1]);
  assert.equal(reordered[1], previous[0]);
  const replaced = mergeCacheEntries(previous, [snapshot()[1], { index: 3, pre_dst: 'new row' }]);
  assert.equal(replaced[0], previous[1]);
  assert.equal(replaced[1].index, 3);
  assert.equal(replaced.length, 2);
  assert.deepEqual(mergeCacheEntries(previous, []), []);
  assert.deepEqual(previous, [{ index: 1, name: ['A', 'B'], pre_dst: 'cat' }, { index: 2, problem: 'visible', proofread_comment: 'old' }]);
});

test('revalidation compares speaker arrays, removed fields, and deletion flags', () => {
  const previous = [{ index: 1, name: ['A', 'B'], pre_dst: 'cat', problem: '' }];
  for (const change of [
    { ...previous[0], name: ['B', 'A'] },
    { index: 1, name: ['A', 'B'], pre_dst: 'cat' },
    { ...previous[0], deleted: true },
  ]) {
    assert.equal(mergeCacheEntries(previous, [change])[0], change);
  }
  assert.equal(mergeCacheEntries(previous, [{ ...previous[0], deleted: false }]), previous);
});
