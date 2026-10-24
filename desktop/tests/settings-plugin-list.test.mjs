import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const PLUGINS = [
  { name: 'file_epub_epub', display_name: 'Epub', version: '1.0.0', author: '', description: '', type: 'file', module: 'file_epub_epub', settings: {} },
  { name: 'text_sakura', display_name: 'Sakura', version: '1.0.0', author: '', description: '', type: 'text', module: 'text_sakura', settings: {} },
  { name: 'problem_common', display_name: 'Problem', version: '1.0.0', author: '', description: '', type: 'problem', module: 'problem_common', settings: {} },
];

const load = createTypeScriptLoader({}, {
  '../lib/api': { fetchPlugins: async () => PLUGINS },
});
const { PluginListSection } = load(new URL('../src/pages/SettingsPage.tsx', import.meta.url));

/** react-test-renderer 的 children 可能是嵌套数组或子节点，这里只把字符串拼起来。 */
const textOf = (node) => {
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (node && Array.isArray(node.children)) return textOf(node.children);
  return '';
};
const byClass = (root, className) => root.findAll((node) => typeof node.type === 'string'
  && String(node.props.className ?? '').split(' ').includes(className), { deep: false });

async function mount() {
  let renderer;
  await act(async () => { renderer = create(createElement(PluginListSection)); });
  return {
    root: () => renderer.root,
    clickTab: (label) => act(() => {
      const tab = byClass(renderer.root, 'plugin-tab').find((node) => textOf(node.children).includes(label));
      assert.ok(tab, `找不到「${label}」这个筛选页签`);
      tab.props.onClick();
    }),
    unmount: () => act(() => renderer.unmount()),
  };
}

test('设置页的插件清单给问题插件单独的筛选页签与徽标', async () => {
  const view = await mount();
  try {
    const tabs = byClass(view.root(), 'plugin-tab').map((node) => textOf(node.children));
    assert.deepEqual(tabs, ['全部 (3)', '文件插件 (1)', '文本插件 (1)', '问题插件 (1)']);

    // 卡片徽标按类型走，问题插件不再是「文本」
    const badges = byClass(view.root(), 'plugin-card__type');
    assert.deepEqual(badges.map((node) => textOf(node.children)), ['文件', '文本', '问题']);
    assert.match(badges[2].props.className, /plugin-card__type--problem/);
  } finally {
    view.unmount();
  }
});

test('问题插件页签只留下问题插件', async () => {
  const view = await mount();
  try {
    view.clickTab('问题插件');
    const cards = byClass(view.root(), 'plugin-card');
    assert.equal(cards.length, 1);
    // 卡片名走 localizePlugin（内置 problem_common 会被翻成「常规问题检测」），用模块名认卡片
    assert.match(textOf(cards[0].children), /problem_common/);
    assert.equal(textOf(byClass(cards[0], 'plugin-card__type')[0].children), '问题');
  } finally {
    view.unmount();
  }
});
