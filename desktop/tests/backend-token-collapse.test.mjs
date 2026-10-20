import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { BackendConfigEditor } = load(new URL('../src/components/BackendConfigEditor.tsx', import.meta.url));

function config(tokens) {
  return { 'OpenAI-Compatible': { tokens } };
}

const TWO_TOKENS = [
  { token: 'sk-aaaaaaaa1111', endpoint: 'https://one.example.com/', modelName: 'model-one' },
  { token: 'sk-bbbbbbbb2222', endpoint: 'https://two.example.com/', modelName: 'model-two' },
];

/** 受控组件：把 onChange 的结果存下来手动回灌，等价于上层 setState。 */
function mount(initial) {
  let current = initial;
  let renderer;
  const onChange = (next) => { current = next; };
  const element = () => createElement(BackendConfigEditor, { config: current, onChange });
  act(() => { renderer = create(element()); });
  return {
    root: () => renderer.root,
    get config() { return current; },
    update() { act(() => renderer.update(element())); },
    unmount() { act(() => renderer.unmount()); },
  };
}

const cards = (root) => root.findAll((node) => typeof node.type === 'string' && node.props.className?.split(' ').includes('token-entry'), { deep: false });
const expandedFlags = (root) => root.findAll((node) => node.type === 'button' && node.props.className === 'token-entry__toggle', { deep: false }).map((node) => node.props['aria-expanded']);
const bodyCount = (root) => cards(root).filter((card) => card.findAll((node) => node.props.className === 'token-entry__body').length > 0).length;
const text = (node) => node.findAllByType('span').map((span) => span.children.join('')).join('|');

test('令牌默认只展开第一张，其余收起并给出摘要', () => {
  const view = mount(config(TWO_TOKENS));
  try {
    assert.equal(cards(view.root()).length, 2);
    assert.deepEqual(expandedFlags(view.root()), [true, false]);
    assert.equal(bodyCount(view.root()), 1);
    // 收起的摘要：模型名 · 去掉协议前缀的地址 · key 尾号
    assert.match(text(cards(view.root())[1]), /令牌 #2.*model-two · two\.example\.com · …2222/);
  } finally {
    view.unmount();
  }
});

test('点「添加令牌」只展开新加的那张，前面的全部收起', () => {
  const view = mount(config(TWO_TOKENS));
  try {
    const addBtn = view.root().find((node) => node.type === 'button' && node.props.className === 'token-list__add-btn');
    act(() => addBtn.props.onClick());
    view.update();

    assert.equal(view.config['OpenAI-Compatible'].tokens.length, 3);
    assert.deepEqual(expandedFlags(view.root()), [false, false, true]);
    assert.equal(bodyCount(view.root()), 1);
    // 被收起的旧卡片靠摘要仍然认得出是哪一条
    assert.match(text(cards(view.root())[0]), /令牌 #1.*model-one · one\.example\.com · …1111/);
  } finally {
    view.unmount();
  }
});

test('收起的空令牌显示占位文案而不是留空', () => {
  const view = mount(config([TWO_TOKENS[0], { token: '', endpoint: '', modelName: '' }]));
  try {
    assert.deepEqual(expandedFlags(view.root()), [true, false]);
    assert.match(text(cards(view.root())[1]), /令牌 #2.*尚未填写/);
  } finally {
    view.unmount();
  }
});

test('点收起的卡片能单独展开，不影响已经展开的', () => {
  const view = mount(config(TWO_TOKENS));
  try {
    const second = view.root().findAll((node) => node.type === 'button' && node.props.className === 'token-entry__toggle', { deep: false })[1];
    act(() => second.props.onClick());
    assert.deepEqual(expandedFlags(view.root()), [true, true]);

    // 再点一次收起自己，第一张保持展开
    const toggles = view.root().findAll((node) => node.type === 'button' && node.props.className === 'token-entry__toggle', { deep: false });
    act(() => toggles[1].props.onClick());
    assert.deepEqual(expandedFlags(view.root()), [true, false]);
  } finally {
    view.unmount();
  }
});

test('删掉展开的那张后，相邻的一张接手展开', () => {
  const view = mount(config(TWO_TOKENS));
  try {
    const removeBtn = view.root().findAll((node) => node.type === 'button' && node.props.className === 'token-entry__remove-btn', { deep: false })[0];
    act(() => removeBtn.props.onClick());
    view.update();

    assert.equal(view.config['OpenAI-Compatible'].tokens.length, 1);
    assert.equal(view.config['OpenAI-Compatible'].tokens[0].modelName, 'model-two');
    assert.deepEqual(expandedFlags(view.root()), [true]);
  } finally {
    view.unmount();
  }
});
