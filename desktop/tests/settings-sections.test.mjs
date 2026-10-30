import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const background = { imageDataUrl: '', imageName: '', opacity: 35, surfaceOpacity: 33 };
const promptOverrides = new Map();
const guidelineSaves = [];

const apiMock = {
  CACHE_BROWSER_FONT_SIZE_MIN: 10,
  CACHE_BROWSER_FONT_SIZE_MAX: 20,
  CUSTOM_BACKGROUND_OPACITY_MIN: 0,
  CUSTOM_BACKGROUND_OPACITY_MAX: 100,
  CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN: 0,
  CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX: 100,
  CUSTOM_BACKGROUND_CHANGE_EVENT: 'galtransl-background-change',
  DISABLE_ANIMATIONS_CHANGE_EVENT: 'galtransl-animations-change',
  HOME_LIST_LIMIT_MIN: 1,
  HOME_LIST_LIMIT_MAX: 50,
  fetchPlugins: async () => [],
  fetchVersion: async () => '8.4.3',
  fetchVersionCheck: async () => ({ version: '8.4.3', latest_version: '8.4.3', update_available: false }),
  getCacheBrowserFontSizePreference: () => 12,
  getCustomBackgroundPreference: () => background,
  getDisableAnimationsPreference: () => false,
  getHideBackendConsolePreference: () => true,
  getHomeHistoryRetentionLimit: () => 10,
  getHomeJobRetentionLimit: () => 10,
  getThemeModePreference: () => 'system',
  loadCustomBackgroundPreference: async () => background,
  setCustomBackgroundPreference: async () => background,
  setCacheBrowserFontSizePreference: () => 12,
  setDisableAnimationsPreference: () => false,
  setHideBackendConsolePreference: () => true,
  setHomeHistoryRetentionLimit: () => 10,
  setHomeJobRetentionLimit: () => 10,
  setThemeModePreference: () => 'system',
  clearCustomBackgroundPreference: async () => background,
  uploadCustomBackgroundImage: async () => background,
  fetchPromptTemplates: async () => ({ templates: [{
    name: 'ForGal', description: 'Test template',
    default_system_prompt: 'Default system', system_prompt: 'Default system', system_overridden: false,
    default_user_prompt: 'Default user', user_prompt: 'Default user', user_overridden: false, overridden: false,
  }] }),
  getPromptTemplateOverride: (name) => promptOverrides.get(name),
  setPromptTemplateOverride: (name, value) => promptOverrides.set(name, value),
  deletePromptTemplateOverride: (name) => promptOverrides.delete(name),
  fetchTranslationGuidelineManager: async () => ({
    dir: 'translation_guidelines', default: 'Basic.md',
    files: [{ name: 'Basic.md', size: 100, mtime: 1, builtin: true }],
  }),
  fetchTranslationGuidelineContent: async () => ({ content: 'Default guideline' }),
  saveTranslationGuideline: async (payload) => {
    guidelineSaves.push(payload);
  },
};

const load = createTypeScriptLoader({
  window: { addEventListener() {}, removeEventListener() {} },
  // useSearchParams 在组件里直接 new URLSearchParams(...)；vm 沙箱只放了部分全局量
  URLSearchParams,
}, {
  '../lib/api': apiMock,
  '../features/connection/ConnectionContext': {
    useConnection: () => ({
      backendUrl: 'http://127.0.0.1:12333',
      connectionPhase: 'online',
      connectionMessage: '已连接',
      connectionStep: 5,
      versionInfo: null,
      translators: [],
      loadingInitialData: false,
      refreshingJobs: false,
      loadInitialData: async () => {},
      loadJobs: async () => null,
    }),
    ConnectionProvider: ({ children }) => children,
  },
});

const { SettingsPage } = load(new URL('../src/pages/SettingsPage.tsx', import.meta.url));

const textOf = (node) => {
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (node && Array.isArray(node.children)) return textOf(node.children);
  return '';
};
const byClass = (root, className) => root.findAll((node) => typeof node.type === 'string'
  && String(node.props.className ?? '').split(' ').includes(className), { deep: false });
const navLabels = (root) => byClass(root, 'section-nav__label').map((node) => textOf(node.children));
const activeNav = (root) => byClass(root, 'section-nav__btn').findIndex((node) => node.props['aria-current'] === 'page');
const panelTitles = (root) => root.findAll((node) => node.type === 'h2', { deep: false }).map((node) => textOf(node.children));

async function mount(entry = '/settings') {
  let renderer;
  await act(async () => {
    renderer = create(createElement(MemoryRouter, { initialEntries: [entry] }, createElement(SettingsPage)));
  });
  return {
    root: () => renderer.root,
    async click(label) {
      await act(async () => {
        const button = byClass(renderer.root, 'section-nav__btn').find((node) => textOf(node.children).includes(label));
        assert.ok(button, `找不到二级菜单项「${label}」`);
        button.props.onClick();
      });
    },
    unmount: () => act(() => renderer.unmount()),
  };
}

test('设置页用二级菜单分区，默认停在外观', async () => {
  const view = await mount();
  try {
    assert.deepEqual(navLabels(view.root()), ['外观', '提示词', '翻译规范', '插件清单', '关于']);
    assert.equal(activeNav(view.root()), 0);
    // 一次只渲染一个分区；首页记忆并在外观里
    assert.deepEqual(panelTitles(view.root()), ['外观', '首页记忆保留']);
  } finally {
    view.unmount();
  }
});

test('点二级菜单只切换当前分区', async () => {
  const view = await mount();
  try {
    // 后端连接并在关于里
    await view.click('关于');
    assert.equal(activeNav(view.root()), 4);
    assert.deepEqual(panelTitles(view.root()), ['关于', '后端连接']);

    await view.click('插件清单');
    assert.equal(activeNav(view.root()), 3);
    assert.deepEqual(panelTitles(view.root()), ['插件清单']);

    // 编辑器直接显示在设置分区里，不再需要进入子页面。
    await view.click('提示词');
    assert.deepEqual(panelTitles(view.root()), ['默认提示词']);
  } finally {
    view.unmount();
  }
});

const button = (root, label) => root.findAllByType('button').find((node) => textOf(node.children) === label);

test('提示词分区直接编辑、保存和重置默认提示词', async () => {
  promptOverrides.clear();
  const view = await mount('/settings?section=prompt');
  try {
    assert.equal(activeNav(view.root()), 1);
    assert.equal(view.root().findAllByType('h1').length, 1);
    assert.equal(button(view.root(), '返回设置'), undefined);
    assert.equal(button(view.root(), '修改默认提示词'), undefined);
    const editors = () => view.root().findAllByType('textarea');
    assert.deepEqual(editors().map((node) => node.props.value), ['Default system', 'Default user']);
    act(() => editors()[0].props.onChange({ target: { value: 'Custom system' } }));
    assert.equal(button(view.root(), '保存修改').props.disabled, false);
    await act(async () => button(view.root(), '保存修改').props.onClick());
    assert.equal(promptOverrides.get('ForGal').system_prompt, 'Custom system');
    await act(async () => button(view.root(), '重置为默认提示词').props.onClick());
    assert.equal(promptOverrides.has('ForGal'), false);
    assert.equal(editors()[0].props.value, 'Default system');
  } finally {
    view.unmount();
  }
});

test('翻译规范分区直接显示文件编辑器并保存内容', async () => {
  guidelineSaves.length = 0;
  const view = await mount('/settings?section=guideline');
  try {
    assert.equal(activeNav(view.root()), 2);
    assert.deepEqual(panelTitles(view.root()), ['通用翻译规范']);
    assert.equal(view.root().findAllByType('h1').length, 1);
    assert.equal(button(view.root(), '返回设置'), undefined);
    assert.equal(button(view.root(), '管理通用翻译规范'), undefined);
    const editor = () => view.root().findByType('textarea');
    assert.equal(editor().props.value, 'Default guideline');
    act(() => editor().props.onChange({ target: { value: 'Custom guideline' } }));
    await act(async () => button(view.root(), '保存规范').props.onClick());
    assert.equal(guidelineSaves.length, 1);
    assert.equal(guidelineSaves[0].filename, 'Basic.md');
    assert.equal(guidelineSaves[0].content, 'Custom guideline');
    assert.equal(button(view.root(), '保存规范').props.disabled, true);
    assert.ok(button(view.root(), '新建'));
    assert.equal(button(view.root(), '删除').props.disabled, true);
  } finally {
    view.unmount();
  }
});

test('?section= 决定初始分区，刷新和返回都停在原来那页', async () => {
  const view = await mount('/settings?section=plugins');
  try {
    assert.equal(activeNav(view.root()), 3);
    assert.deepEqual(panelTitles(view.root()), ['插件清单']);
  } finally {
    view.unmount();
  }

  // 合并掉的旧分区参数落到合并后的分区
  for (const [entry, index, titles] of [
    ['/settings?section=retention', 0, ['外观', '首页记忆保留']],
    ['/settings?section=connection', 4, ['关于', '后端连接']],
  ]) {
    const legacy = await mount(entry);
    try {
      assert.equal(activeNav(legacy.root()), index);
      assert.deepEqual(panelTitles(legacy.root()), titles);
    } finally {
      legacy.unmount();
    }
  }

  // 参数不认识时退回第一个分区，而不是白屏
  const fallback = await mount('/settings?section=nope');
  try {
    assert.equal(activeNav(fallback.root()), 0);
    assert.deepEqual(panelTitles(fallback.root()), ['外观', '首页记忆保留']);
  } finally {
    fallback.unmount();
  }
});
