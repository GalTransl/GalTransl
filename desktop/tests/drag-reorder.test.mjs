import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const { applySavedOrder, dragItemTransform, dragTargetIndex, moveKey, parseSavedOrder } =
  createTypeScriptLoader()(new URL('../src/lib/dragReorder.ts', import.meta.url));
// 模块跑在 vm 里，它构造的数组原型属于另一个 realm，比较前复制一份
const plain = (value) => [...value];

test('a dragged folder lands at the slot whose midpoint its center has crossed', () => {
  const starts = [0, 20, 40, 60];
  const sizes = [20, 20, 20, 20];
  assert.equal(dragTargetIndex(starts, sizes, 0, 15), 0);
  assert.equal(dragTargetIndex(starts, sizes, 0, 45), 2);
  assert.equal(dragTargetIndex(starts, sizes, 3, -65), 0);
  assert.equal(dragTargetIndex(starts, sizes, 1, 1), 1);
});

test('an expanded sidebar folder grabbed by its header can be dragged to the first slot', () => {
  // 回归：第二、三个文件夹展开时拖不到首位。落点原先比的是中心，展开的文件夹有几百像素高，
  // 手抓的却是顶部标题行，中心要越过第一项的中心，指针得移到列表上方之外。
  // 两个折叠的文件夹（高 40）后面跟一个展开的（高 300），间距 4。
  const starts = [0, 44, 88];
  const sizes = [40, 40, 300];
  // 标题行拖到第一项的位置（位移 -88）就该落在首位；旧算法要 -218 以上
  assert.equal(dragTargetIndex(starts, sizes, 2, -40), 2);
  assert.equal(dragTargetIndex(starts, sizes, 2, -50), 1);
  assert.equal(dragTargetIndex(starts, sizes, 2, -89), 0);
  // 反过来，折叠的文件夹越过展开的那个，也只需要盖住自己的高度，不必拖过它的中点
  assert.equal(dragTargetIndex(starts, sizes, 1, 40), 1);
  assert.equal(dragTargetIndex(starts, sizes, 1, 45), 2);
  // 展开的文件夹在中间时同样能拖到首位
  assert.equal(dragTargetIndex([0, 44, 348], [40, 300, 40], 1, -45), 0);
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
