import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const { previewSessions, SESSION_PREVIEW_LIMIT } = createTypeScriptLoader()(new URL('../src/lib/sessionPreview.ts', import.meta.url));
const ids = (count) => Array.from({ length: count }, (_, index) => `s${index}`);

test('sidebar groups list five sessions until "show more" is clicked, then offer to collapse', () => {
  assert.equal(SESSION_PREVIEW_LIMIT, 5);
  const folded = previewSessions(ids(8), false, () => false);
  assert.deepEqual(folded.visible, ids(5));
  assert.equal(folded.hidden, 3);
  assert.equal(folded.collapsible, false);
  const open = previewSessions(ids(8), true, () => false);
  // 模块跑在 vm 里，它自己构造的数组原型属于另一个 realm，复制一份再比
  assert.deepEqual([...open.visible], ids(8));
  assert.equal(open.hidden, 0);
  assert.equal(open.collapsible, true);
});

test('a group with five or fewer sessions shows neither "show more" nor "show less"', () => {
  for (const expanded of [false, true]) {
    const preview = previewSessions(ids(5), expanded, () => false);
    assert.equal(preview.hidden, 0);
    assert.equal(preview.collapsible, false);
  }
});

test('an old active or running session stays listed in place while the group is folded', () => {
  const preview = previewSessions(ids(9), false, (id) => id === 's7');
  assert.deepEqual(preview.visible, ['s0', 's1', 's2', 's3', 's4', 's7']);
  assert.equal(preview.hidden, 3);
});
