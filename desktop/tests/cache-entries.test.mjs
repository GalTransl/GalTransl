import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const { createCacheProblemFilter, entriesMatch } = createTypeScriptLoader()(new URL('../src/lib/cacheEntries.ts', import.meta.url));

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
