import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function render(onPick) {
  const document = { addEventListener() {}, removeEventListener() {}, activeElement: null };
  const load = createTypeScriptLoader({ document, requestAnimationFrame: () => 0 });
  const { QuickCommands } = load(new URL('../src/pages/agent/QuickCommands.tsx', import.meta.url));
  const commands = [
    { key: 'prep', label: '准备', prompt: '做一下翻译前准备' },
    { key: 'fix', label: '修复', prompt: '修一下问题' },
  ];
  let renderer;
  act(() => { renderer = create(createElement(QuickCommands, { label: '快捷指令', commands, onPick })); });
  return renderer;
}

test('quick commands chip opens a menu that hands the chosen prompt to the page without sending', () => {
  const picked = [];
  const renderer = render(prompt => picked.push(prompt));
  assert.equal(renderer.root.findAllByProps({ role: 'menu' }).length, 0);
  act(() => renderer.root.findByProps({ 'aria-haspopup': 'menu' }).props.onClick());
  const items = renderer.root.findAllByProps({ role: 'menuitem' });
  assert.deepEqual(items.map(item => item.props.children), ['准备', '修复']);
  act(() => items[1].props.onClick());
  assert.deepEqual(picked, ['修一下问题']);
  assert.equal(renderer.root.findAllByProps({ role: 'menu' }).length, 0);
  act(() => renderer.unmount());
});
