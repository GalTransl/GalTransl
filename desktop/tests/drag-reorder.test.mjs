import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const { applySavedOrder, dragItemTransform, dragTargetIndex, moveKey, parseSavedOrder } =
  createTypeScriptLoader()(new URL('../src/lib/dragReorder.ts', import.meta.url));
// 模块跑在 vm 里，它构造的数组原型属于另一个 realm，比较前复制一份
const plain = (value) => [...value];

test('a dragged folder lands at the slot whose midpoint its center has crossed', () => {
  const mids = [10, 30, 50, 70];
  assert.equal(dragTargetIndex(mids, 0, 25), 0);
  assert.equal(dragTargetIndex(mids, 0, 55), 2);
  assert.equal(dragTargetIndex(mids, 3, 5), 0);
  assert.equal(dragTargetIndex(mids, 1, 31), 1);
});

test('folders between the start and drop slots shift by one place along the list axis', () => {
  const drag = { key: 'b', fromIndex: 1, toIndex: 3, offset: 42, shift: 20, axis: 'y' };
  assert.deepEqual([0, 1, 2, 3, 4].map((index) => dragItemTransform(drag, index)),
    [undefined, 'translateY(42px)', 'translateY(-20px)', 'translateY(-20px)', undefined]);
  // 窄屏下 Agent 侧栏横排时按横向位移
  const up = { ...drag, fromIndex: 3, toIndex: 1, offset: -40, axis: 'x' };
  assert.deepEqual([0, 1, 2, 3].map((index) => dragItemTransform(up, index)),
    [undefined, 'translateX(20px)', 'translateX(20px)', 'translateX(-40px)']);
  assert.equal(dragItemTransform(null, 0), undefined);
});

test('dropping moves the folder to its new slot', () => {
  assert.deepEqual(plain(moveKey(['a', 'b', 'c', 'd'], 'a', 2)), ['b', 'c', 'a', 'd']);
  assert.deepEqual(plain(moveKey(['a', 'b', 'c', 'd'], 'd', 0)), ['d', 'a', 'b', 'c']);
  assert.deepEqual(plain(moveKey(['a', 'b'], 'x', 0)), ['a', 'b']);
});

test('agent sidebars keep the dragged folder order and put folders not placed yet on top', () => {
  assert.deepEqual(plain(applySavedOrder(['a', 'b', 'c'], [])), ['a', 'b', 'c']);
  assert.deepEqual(plain(applySavedOrder(['a', 'b', 'c'], ['c', 'a', 'b'])), ['c', 'a', 'b']);
  // 新打开的 d 排最前；已关闭的 x 留在存储里不影响
  assert.deepEqual(plain(applySavedOrder(['d', 'a', 'b', 'c'], ['c', 'x', 'a', 'b'])), ['d', 'c', 'a', 'b']);
});

test('a corrupted saved order is treated as never reordered', () => {
  assert.deepEqual(plain(parseSavedOrder(null)), []);
  assert.deepEqual(plain(parseSavedOrder('{oops')), []);
  assert.deepEqual(plain(parseSavedOrder('{"a":1}')), []);
  assert.deepEqual(plain(parseSavedOrder('["a",2,"b"]')), ['a', 'b']);
});
