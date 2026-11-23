import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

/* 守护全局右键拦截的口径：WebView 自带菜单（在新窗口中打开链接、检查……）只在
   能编辑的文本和已选中的文字上保留，其余地方一律屏蔽。 */
const EDITABLE = 'input, textarea, [contenteditable]:not([contenteditable="false"])';
const element = (editable) => ({ closest: (selector) => (selector === EDITABLE && editable ? {} : null) });

function setup(selectedText = '') {
  const listeners = new Map();
  const document = {
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type); },
  };
  const window = { getSelection: () => ({ toString: () => selectedText }) };
  const module = createTypeScriptLoader({ document, window })(new URL('../src/lib/nativeContextMenu.ts', import.meta.url));
  const rightClick = (target) => {
    let prevented = false;
    listeners.get('contextmenu')?.({ target, preventDefault() { prevented = true; } });
    return prevented;
  };
  return { ...module, listeners, rightClick };
}

test('the native menu is blocked on links, buttons and blank areas', () => {
  const f = setup();
  f.installNativeContextMenuBlock();
  assert.equal(f.rightClick(element(false)), true);
  assert.equal(f.rightClick(null), true);
});

test('editable text keeps the native menu for cut, paste and undo', () => {
  const f = setup();
  f.installNativeContextMenuBlock();
  assert.equal(f.rightClick(element(true)), false);
});

test('selected text keeps the native menu so it can be copied', () => {
  const f = setup('选中的译文');
  f.installNativeContextMenuBlock();
  assert.equal(f.rightClick(element(false)), false);
  assert.equal(f.keepsNativeContextMenu(element(false), '  \n'), false);
});

test('uninstalling removes the listener', () => {
  const f = setup();
  f.installNativeContextMenuBlock()();
  assert.equal(f.listeners.size, 0);
});
