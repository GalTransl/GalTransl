import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const lib = createTypeScriptLoader()(new URL('../src/lib/virtualList.ts', import.meta.url));
// The loader runs modules in another realm; compare plain copies.
const plain = value => JSON.parse(JSON.stringify(value));
const computeOffsets = (...args) => plain(lib.computeOffsets(...args));
const pickRange = (...args) => plain(lib.pickRange(...args));
const { rangeCovers } = lib;

test('offsets use measured heights where known and the estimate elsewhere, gap included', () => {
  const offsets = computeOffsets(['a', 'b', 'c'], new Map([['b', 50]]), 100, 8);
  assert.deepEqual(offsets, [0, 108, 166, 274]);
});

test('the window covers the viewport plus overscan and never shrinks below the minimum item count', () => {
  const offsets = computeOffsets(Array.from({ length: 1000 }, (_, i) => String(i)), new Map(), 100, 0);
  assert.deepEqual(pickRange(offsets, 0, 400, 200, 3), { start: 0, end: 7 });
  assert.deepEqual(pickRange(offsets, 50_000, 400, 200, 3), { start: 498, end: 507 });
  // A hidden pane reports zero height; still render enough rows to fill it once shown.
  assert.deepEqual(pickRange(offsets, 0, 0, 0, 30), { start: 0, end: 30 });
  assert.deepEqual(pickRange(offsets, 99_900, 400, 200, 30), { start: 997, end: 1000 });
  assert.deepEqual(pickRange([0], 0, 400, 200, 30), { start: 0, end: 0 });
});

test('scrolling inside the margin keeps the current window; leaving it or shrinking the list does not', () => {
  const offsets = computeOffsets(Array.from({ length: 1000 }, (_, i) => String(i)), new Map(), 100, 0);
  const range = pickRange(offsets, 10_000, 400, 1200, 30);
  assert.equal(rangeCovers(range, offsets, 10_200, 400, 300), true);
  assert.equal(rangeCovers(range, offsets, 11_200, 400, 300), false);
  assert.equal(rangeCovers(range, offsets.slice(0, 50), 0, 400, 300), false);
  assert.equal(rangeCovers({ start: 0, end: 0 }, offsets, 0, 400, 300), false);
});
