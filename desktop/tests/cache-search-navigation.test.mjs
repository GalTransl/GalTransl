import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

async function fixture() {
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
    },
  });
  const params = new URLSearchParams();
  const { ProjectCachePage } = load(new URL('../src/pages/ProjectCachePage.tsx', import.meta.url));
  await act(async () => {
    view = create(h(ProjectCachePage, { ctx: { projectId: 'fixture', configFileName: 'config.yaml' } }), {
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
  clickText('common:actions.search');
  clickText('projects:projectCachePage.addSearchCondition');
  const queries = () => view.root.findAllByProps({ className: 'cache-search cache-search--global' });
  act(() => queries()[0].props.onChange({ target: { value: '悶え' } }));
  act(() => queries()[1].props.onChange({ target: { value: '挣扎' } }));
  await flush(400);
  const jump = i => { act(() => view.root.findAllByType('button').find(node => node.props['data-search-idx'] === i).props.onClick()); flushFrames(); };
  return { view, pane, jump, resolveFile, flush, flushFrames, selectFile, clickText };
}

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
    const marks = () => card().findAllByType('mark').map(node => node.props.children);
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
