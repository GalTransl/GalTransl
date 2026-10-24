import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

async function fixture({ setupSearch = true, api = {} } = {}) {
  const timers = new Map(), frames = new Map();
  let sequence = 0, view;
  const setTimer = (callback, delay) => { const id = ++sequence; timers.set(id, { callback, delay }); return id; };
  const events = { addEventListener() {}, removeEventListener() {} };
  const entries = [
    { index: 1, pre_src: '猫', post_src: '猫', pre_dst: 'cat', problem: 'test' },
    { index: 25, pre_src: '原文 悶え', post_src: '悶え source', pre_dst: '译文 挣扎', proofread_dst: '校对 挣扎', post_dst_preview: '预览 挣扎' },
  ];
  const pendingLoads = new Map();
  const pane = {
    scrollTop: 0, clientTop: 1, clientHeight: 400, scrollHeight: 4000,
    getBoundingClientRect: () => ({ top: 150 }),
    querySelector(selector) {
      const index = Number(selector.match(/"(\d+)"/)[1]);
      if (!view.root.findAllByProps({ 'data-cache-index': index }).length) return null;
      return {
        getBoundingClientRect: () => ({ top: 151 + index * 100 - pane.scrollTop, bottom: 251 + index * 100 - pane.scrollTop, height: 100 }),
        classList: { add() {}, remove() {} },
        scrollIntoView() { assert.fail('must not scroll outer containers'); },
      };
    },
  };
  const load = createTypeScriptLoader({
    document: events, window: events,
    setTimeout: setTimer, clearTimeout: id => timers.delete(id),
    requestAnimationFrame: callback => { const id = ++sequence; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
  }, {
    '../i18n': { t: key => key, message: key => key, useUiLanguage: () => 'en', useMessageState: useState },
    '../i18n/core': { t: key => key },
    '../components/PageActivity': { useRetainPage() {} },
    '../lib/useUiAnimations': { useUiAnimations: () => false },
    '../components/Button': { Button: ({ children, ...props }) => h('button', props, children) },
    '../components/Icon': { Icon: () => null },
    '../components/Panel': { Panel: ({ children, actions }) => h('section', null, actions, children) },
    '../components/PageHeader': { PageHeader: () => null },
    '../components/page-state': { EmptyState: () => null, InlineFeedback: () => null, LoadingState: () => null },
    './CustomSelect': { CustomSelect: ({ children, ...props }) => h('select', props, children) },
    '../lib/useNameDict': { useNameDict: () => ({ nameDict: new Map() }), resolveSpeakerName: value => value },
    'react-router-dom': { useSearchParams: () => [params] },
    '@tauri-apps/api/core': { invoke: async () => {} },
    '../lib/api': {
      getCacheBrowserFontSizePreference: () => 14,
      fetchProjectCache: async () => ({ files: ['a.json', 'b.json'].map(name => ({ name, is_file: true, size: 1 })) }),
      fetchProjectProblems: async () => ({ problems: [], filter_keys: [] }),
      fetchCacheFile: async (_project, file) => new Promise(resolve => pendingLoads.set(file, () => resolve({ entries, has_cache: true }))),
      searchCache: async () => ({ total: 2, results: ['a.json', 'b.json'].map(filename => ({ filename, ...entries[1], src: entries[1].post_src, dst: entries[1].pre_dst })) }),
      ...api,
    },
  });
  const params = new URLSearchParams();
  const { ProjectCachePage } = load(new URL('../src/pages/ProjectCachePage.tsx', import.meta.url));
  let pageProps = { ctx: { projectId: 'fixture', configFileName: 'config.yaml' }, active: true };
  await act(async () => {
    view = create(h(ProjectCachePage, pageProps), {
      createNodeMock: element => String(element.props.className).split(' ').includes('cache-card-list') ? pane : null,
    });
  });
  const buttons = () => view.root.findAllByType('button');
  const clickText = text => act(() => buttons().find(node => node.props.children === text || Array.isArray(node.props.children) && node.props.children.includes(text)).props.onClick());
  const resolveFile = async file => { await act(async () => pendingLoads.get(file)()); flushFrames(); };
  const flush = async delay => {
    await act(async () => {
      for (const [id, timer] of [...timers]) if (timer.delay === delay) { timers.delete(id); timer.callback(); }
    });
  };
  const flushFrames = () => {
    for (let i = 0; i < 3; i++) act(() => { for (const [id, callback] of [...frames]) { frames.delete(id); callback(); } });
  };
  const selectFile = file => {
    clickText('projects:projectCachePage.cacheSidebarTabs_button_file');
    act(() => buttons().find(node => node.props.className?.startsWith('cache-file-item') && node.findAllByProps({ className: 'cache-file-item__name' })[0]?.props.children[1] === file)
      .props.onClick({ ctrlKey: false, metaKey: false, shiftKey: false }));
  };
  if (setupSearch) {
    clickText('common:actions.search');
    clickText('projects:projectCachePage.addSearchCondition');
    const queries = () => view.root.findAllByProps({ className: 'cache-search cache-search--global' });
    act(() => queries()[0].props.onChange({ target: { value: '悶え' } }));
    act(() => queries()[1].props.onChange({ target: { value: '挣扎' } }));
    await flush(400);
  }
  const updatePage = async (props) => {
    pageProps = { ...pageProps, ...props };
    await act(async () => { view.update(h(ProjectCachePage, pageProps)); });
  };
  const jump = i => { act(() => view.root.findAllByType('button').find(node => node.props['data-search-idx'] === i).props.onClick()); flushFrames(); };
  return { view, pane, jump, resolveFile, flush, flushFrames, selectFile, clickText, updatePage };
}

test('problem tabs reuse their snapshot and page returns conditionally check without hiding the list', async () => {
  const calls = [];
  let finish, hold = false;
  const f = await fixture({ setupSearch: false, api: {
    fetchProjectProblems: async (...args) => {
      calls.push(args);
      if (hold) return new Promise(resolve => { finish = resolve; });
      if (args[2]) return { unchanged: true, revision: args[2] };
      return { problems: [{ filename: 'a.json', index: 1, problem: 'old problem' }], filter_keys: [], revision: 'v1' };
    },
  } });
  try {
    await act(async () => f.clickText('projects:projectCachePage.cacheSidebarTabs_button_problem'));
    const labels = () => f.view.root.findAllByProps({ className: 'cache-problems-group__type' }).map(node => node.props.children);
    const loading = () => f.view.root.findAllByProps({ className: 'cache-problems-loading' }).length;
    assert.deepEqual(labels(), ['old problem']);
    f.clickText('common:actions.search');
    await act(async () => f.clickText('projects:projectCachePage.cacheSidebarTabs_button_problem'));
    f.clickText('projects:projectCachePage.cacheSidebarTabs_button_problem');
    assert.equal(calls.length, 3);
    assert.ok(calls.slice(1).every(args => args[2] === 'v1'));
    hold = true;
    await f.updatePage({ active: false });
    await f.updatePage({ active: true });
    assert.equal(calls.length, 4);
    assert.equal(calls[3][2], 'v1');
    assert.equal(loading(), 0);
    assert.deepEqual(labels(), ['old problem']);
    await f.updatePage({ active: false });
    await f.updatePage({ active: true });
    assert.equal(calls.length, 4); // A pending check is reused.
    await act(async () => finish({ unchanged: true, revision: 'v1' }));
    assert.deepEqual(labels(), ['old problem']);
    await f.updatePage({ active: false });
    await f.updatePage({ active: true });
    await act(async () => finish({ problems: [], filter_keys: ['old problem'], revision: 'v2' }));
    assert.equal(loading(), 0);
    assert.deepEqual(labels(), []);
    hold = false;
    f.clickText('common:actions.search');
    await act(async () => f.clickText('projects:projectCachePage.cacheSidebarTabs_button_problem'));
    assert.equal(calls.at(-1)[2], 'v2'); // A valid empty snapshot must also be cached.
    assert.equal(loading(), 0);
  } finally { act(() => f.view.unmount()); }
});

test('saving cache entries bypasses the old problem revision and refreshes the snapshot', async () => {
  const calls = [];
  const f = await fixture({ setupSearch: false, api: {
    fetchProjectProblems: async (...args) => {
      calls.push(args);
      return { problems: [], filter_keys: [], revision: `v${calls.length}` };
    },
    saveCacheFile: async (_project, _file, entries) => ({ entries }),
  } });
  try {
    f.selectFile('a.json');
    await f.resolveFile('a.json');
    const card = f.view.root.findByProps({ 'data-cache-index': 25 });
    act(() => card.findByProps({ className: 'cache-card__expand' }).props.onClick());
    act(() => card.findAllByType('textarea')[0].props.onChange({ target: { value: 'edited translation' } }));
    await act(async () => f.clickText('common:actions.save'));
    assert.equal(calls.length, 2);
    assert.equal(calls[1][2], undefined);
  } finally { act(() => f.view.unmount()); }
});

test('problem snapshots are scoped to the project/config and late responses cannot replace the current list', async () => {
  const calls = [];
  const f = await fixture({ setupSearch: false, api: {
    fetchProjectProblems: (...args) => new Promise(resolve => calls.push({ args, resolve })),
  } });
  try {
    f.clickText('projects:projectCachePage.cacheSidebarTabs_button_problem');
    assert.equal(calls.length, 1); // Initial load and early tab click share a request.
    await f.updatePage({ ctx: { projectId: 'fixture', configFileName: 'other.yaml' } });
    assert.equal(calls.length, 2);
    assert.equal(calls[1].args[2], undefined);
    await act(async () => calls[1].resolve({ problems: [{ filename: 'a.json', index: 1, problem: 'new config' }], revision: 'new' }));
    await act(async () => calls[0].resolve({ problems: [{ filename: 'a.json', index: 1, problem: 'stale' }], revision: 'stale' }));
    assert.deepEqual(f.view.root.findAllByProps({ className: 'cache-problems-group__type' }).map(node => node.props.children), ['new config']);
    await f.updatePage({ ctx: { projectId: 'other-project', configFileName: 'other.yaml' } });
    assert.equal(calls.length, 3);
    assert.equal(calls[2].args[2], undefined);
  } finally { act(() => f.view.unmount()); }
});

test('result jumps wait for the file, center only the editor and override saved positions', async () => {
  const f = await fixture();
  try {
    f.jump(0);
    assert.equal(f.pane.scrollTop, 0);
    await f.resolveFile('a.json');
    await f.flush(100);
    f.flushFrames();
    assert.equal(f.pane.scrollTop, 2350);
    f.pane.scrollTop = 420;
    f.view.root.findAll(node => typeof node.type === 'string' && node.props.className?.split?.(' ').includes('cache-card-list'))[0].props.onScroll();
    f.jump(1);
    await f.resolveFile('b.json');
    assert.equal(f.pane.scrollTop, 2350);
    f.pane.scrollTop = 200;
    f.jump(0); // a.json is cached; its saved position must not override the result jump.
    f.flushFrames();
    assert.equal(f.pane.scrollTop, 2350);
    f.pane.scrollTop = 700;
    f.jump(0); // Repeat the very same selected result.
    assert.equal(f.pane.scrollTop, 2350);
  } finally { act(() => f.view.unmount()); }
});

test('jump reveals targets hidden by local filters and all search terms reach collapsed and expanded fields', async () => {
  const f = await fixture();
  try {
    f.jump(0);
    await f.resolveFile('a.json');
    const card = () => f.view.root.findByProps({ 'data-cache-index': 25 });
    const marks = () => card().findAllByType('mark').filter(node => {
      for (let parent = node.parent; parent; parent = parent.parent) {
        if (parent.props['aria-hidden'] === true) return false;
      }
      return true;
    }).map(node => node.props.children);
    assert.deepEqual(marks(), ['悶え', '挣扎']);
    act(() => f.view.root.findByProps({ className: 'cache-search' }).props.onChange({ target: { value: 'cat' } }));
    act(() => f.view.root.findByProps({ className: 'cache-filter' }).findByType('input').props.onChange({ target: { checked: true } }));
    assert.equal(f.view.root.findAllByProps({ 'data-cache-index': 25 }).length, 0);
    f.jump(0);
    assert.equal(f.view.root.findByProps({ className: 'cache-search' }).props.value, '');
    assert.equal(f.view.root.findByProps({ className: 'cache-filter' }).findByType('input').props.checked, false);
    assert.equal(f.pane.scrollTop, 2350);
    act(() => card().findByProps({ className: 'cache-card__expand' }).props.onClick());
    assert.deepEqual(marks(), ['悶え', '悶え', '挣扎', '挣扎', '挣扎']);
    const textarea = card().findAllByType('textarea')[0];
    act(() => textarea.props.onChange({ target: { value: '挣扎 edited' } }));
    assert.equal(card().findAllByType('textarea')[0].props.value, '挣扎 edited');
    assert.ok(marks().includes('挣扎'));
  } finally { act(() => f.view.unmount()); }
});

test('ordinary file selection keeps its remembered position', async () => {
  const f = await fixture();
  try {
    f.jump(0);
    await f.resolveFile('a.json');
    f.pane.scrollTop = 600;
    f.selectFile('b.json');
    await f.resolveFile('b.json');
    f.flushFrames();
    assert.equal(f.pane.scrollTop, 0);
    f.pane.scrollTop = 100;
    f.selectFile('a.json');
    f.flushFrames();
    assert.equal(f.pane.scrollTop, 600);
  } finally { act(() => f.view.unmount()); }
});

test('container scroll math handles pane transforms, borders, nearest alignment and bounds', () => {
  const load = createTypeScriptLoader();
  const { scrollWithinContainer } = load(new URL('../src/lib/scrollWithinContainer.ts', import.meta.url));
  const pane = { scrollTop: 100, clientTop: 2, clientHeight: 400, scrollHeight: 1000, getBoundingClientRect: () => ({ top: 158 }) };
  const target = (top, height = 100) => ({ getBoundingClientRect: () => ({ top, bottom: top + height, height }) });
  scrollWithinContainer(pane, target(260), 'nearest');
  assert.equal(pane.scrollTop, 100);
  scrollWithinContainer(pane, target(510), 'nearest');
  assert.equal(pane.scrollTop, 150);
  scrollWithinContainer(pane, target(140), 'nearest');
  assert.equal(pane.scrollTop, 130);
  scrollWithinContainer(pane, target(-500));
  assert.equal(pane.scrollTop, 0);
  scrollWithinContainer(pane, target(2000));
  assert.equal(pane.scrollTop, 600);
});
