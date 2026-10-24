import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const background = { imageDataUrl: '', imageName: '', opacity: 35, surfaceOpacity: 33 };

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
    click(label) {
      act(() => {
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
    assert.deepEqual(navLabels(view.root()), ['外观', '提示词', '翻译规范', '首页记忆', '插件清单', '后端连接', '关于']);
    assert.equal(activeNav(view.root()), 0);
    // 一次只渲染一个分区
    assert.deepEqual(panelTitles(view.root()), ['外观']);
  } finally {
    view.unmount();
  }
});

test('点二级菜单只切换当前分区', async () => {
  const view = await mount();
  try {
    view.click('关于');
    assert.equal(activeNav(view.root()), 6);
    assert.deepEqual(panelTitles(view.root()), ['关于']);

    view.click('插件清单');
    assert.equal(activeNav(view.root()), 4);
    assert.deepEqual(panelTitles(view.root()), ['插件清单']);

    // 提示词/翻译规范只是入口，内容在各自的子页面里
    view.click('提示词');
    assert.deepEqual(panelTitles(view.root()), ['提示词']);
  } finally {
    view.unmount();
  }
});

test('?section= 决定初始分区，刷新和返回都停在原来那页', async () => {
  const view = await mount('/settings?section=retention');
  try {
    assert.equal(activeNav(view.root()), 3);
    assert.deepEqual(panelTitles(view.root()), ['首页记忆保留']);
  } finally {
    view.unmount();
  }

  // 参数不认识时退回第一个分区，而不是白屏
  const fallback = await mount('/settings?section=nope');
  try {
    assert.equal(activeNav(fallback.root()), 0);
    assert.deepEqual(panelTitles(fallback.root()), ['外观']);
  } finally {
    fallback.unmount();
  }
});
