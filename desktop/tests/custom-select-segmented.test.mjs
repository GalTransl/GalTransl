import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { CustomSelect } = load(new URL('../src/components/CustomSelect.tsx', import.meta.url));

function render(props, values) {
  let view;
  act(() => {
    view = create(h(CustomSelect, props, values.map((value) => h('option', { key: value, value }, value.toUpperCase()))));
  });
  return view;
}

const radios = (view) => view.root.findAllByProps({ role: 'radio' });

test('segmented select shows fewer than four options as pills and reports the clicked value', () => {
  const changes = [];
  const view = render({ segmented: true, value: 'dark', onChange: (event) => changes.push(event.target.value) }, ['light', 'dark', 'system']);
  try {
    assert.deepEqual(radios(view).map((node) => [node.props.children, node.props['aria-checked']]),
      [['LIGHT', false], ['DARK', true], ['SYSTEM', false]]);
    act(() => radios(view)[2].props.onClick());
    // 已选中的那一项再点不重复通知
    act(() => radios(view)[1].props.onClick());
    assert.deepEqual(changes, ['system']);
    // 方向键和单选组一样直接切到相邻项，并首尾循环
    act(() => radios(view)[1].props.onKeyDown({ key: 'ArrowLeft', preventDefault() {} }));
    act(() => radios(view)[2].props.onKeyDown({ key: 'ArrowRight', preventDefault() {} }));
    assert.deepEqual(changes, ['system', 'light', 'light']);
  } finally { act(() => view.unmount()); }
});

test('segmented select stays a dropdown when pills could not represent it', () => {
  // 四个及以上选项、当前值不在选项里（下拉框还能显示原值）、没开 segmented，都保持下拉框
  for (const [props, values] of [
    [{ segmented: true, value: 'a' }, ['a', 'b', 'c', 'd']],
    [{ segmented: true, value: 'legacy' }, ['a', 'b']],
    [{ value: 'a' }, ['a', 'b']],
  ]) {
    const view = render(props, values);
    try {
      assert.equal(radios(view).length, 0);
      assert.equal(view.root.findAllByProps({ 'aria-haspopup': 'listbox' }).length, 1);
    } finally { act(() => view.unmount()); }
  }
});

test('disabled segmented select ignores clicks and keys', () => {
  const changes = [];
  const view = render({ segmented: true, disabled: true, value: 'a', onChange: (event) => changes.push(event.target.value) }, ['a', 'b']);
  try {
    act(() => radios(view)[1].props.onClick());
    act(() => radios(view)[0].props.onKeyDown({ key: 'ArrowRight', preventDefault() {} }));
    assert.deepEqual(changes, []);
    assert.deepEqual(radios(view).map((node) => node.props.tabIndex), [-1, -1]);
  } finally { act(() => view.unmount()); }
});
