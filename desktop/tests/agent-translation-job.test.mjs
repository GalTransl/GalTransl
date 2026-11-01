import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function setup(status = 'running', currentJobId = 'job-1', options = {}) {
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  let snapshot = {
    job: { job_id: currentJobId, status, started_at: '2026-11-01T00:00:00Z', finished_at: '' },
    summary: { total: 100, translated: 8, percent: 8, translation_speed_lpm: 12, eta_seconds: 460 },
  };
  const boundary = {};
  const listeners = new Map(), storage = new Map();
  let pageActivity, mounted = 0, restored = 0;
  const document = {
    body: {}, documentElement: {}, hidden: false,
    activeElement: { focus() { restored++; } },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
  };
  const closeNode = { focus() { document.activeElement = closeNode; }, hasAttribute: () => false, getClientRects: () => [{}] };
  const lastNode = { ...closeNode, focus() { document.activeElement = lastNode; } };
  const dialogNode = { querySelectorAll: () => [closeNode, lastNode], contains: node => node === closeNode || node === lastNode };
  const load = createTypeScriptLoader({ TextEncoder, btoa, document, CustomEvent: class { constructor(type) { this.type = type; } }, localStorage: {
    getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key),
  }, window: {
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
    setInterval: callback => { timers.set(++timerId, callback); return timerId; },
    clearInterval: id => timers.delete(id),
  } }, {
    '../../../lib/api': boundary,
    'react-dom': { createPortal: children => children },
    '../ProjectTranslatePage': { ProjectTranslatePage: ({ ctx }) => {
      const [instance] = useState(() => ++mounted);
      return createElement('workbench', { ctx, active: pageActivity.usePageActive(), instance });
    } },
    '../ProjectDictionaryPage': { ProjectDictionaryPage: props => createElement('dictionary-editor', props) },
    '../ProjectCachePage': { ProjectCachePage: props => createElement('cache-editor', props) },
  });
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  Object.assign(boundary, api, {
    fetchProjectRuntime: async () => snapshot,
    fetchJobs: async () => [{ job_id: 'job-1', status }],
    stopProjectTranslation: projectId => {
      requests.push(projectId);
      return boundary.stopResponse;
    },
  });
  api.getDisableAnimationsPreference = () => options.animated !== true;
  api.addOpenProject('C:\\Games\\Example', 'custom.yml');
  pageActivity = load(new URL('../src/components/PageActivity.tsx', import.meta.url));
  const { ProjectTranslationPreviewProvider } = load(new URL('../src/pages/agent/ProjectTranslationPreview.tsx', import.meta.url));
  const { TranslationJobCard } = load(new URL('../src/pages/agent/rows/TranslationJobCard.tsx', import.meta.url));
  const item = { kind: 'tool', step: 1, ok: true, arguments: { translator: 'auto-translate', files: ['scene.json'] }, result: { job_id: 'job-1', status } };
  return { load, boundary, requests, timers, api, document, listeners, closeNode, lastNode, restored: () => restored,
    nodes: element => element.type === 'section' && element.props.className === 'agent-translation-preview'
      ? dialogNode : element.type === 'button' && element.props['aria-label'] === '关闭' ? closeNode : null,
    view: (active = true, cardVisible = true, directory = 'C:\\Games\\Example') => {
      const card = cardVisible ? createElement(TranslationJobCard, { item, projectDir: directory }) : null;
      return options.preview ? createElement(pageActivity.PageActivityContext.Provider, { value: active },
        createElement(ProjectTranslationPreviewProvider, null, card)) : card;
    },
    cancel: () => { snapshot = { ...snapshot, job: { ...snapshot.job, status: 'cancelled', finished_at: '2026-11-01T00:01:00Z' } }; },
  };
}

test('dictionary and cache diff entries open the matching mini editor with project, config, file and row', async () => {
  const f = setup();
  const { ProjectTranslationPreviewProvider } = f.load(new URL('../src/pages/agent/ProjectTranslationPreview.tsx', import.meta.url));
  const { ChangeListCard, extractChangeList } = f.load(new URL('../src/pages/agent/rows/ToolRow.tsx', import.meta.url));
  const directory = 'C:\\Games\\Example';
  const cache = extractChangeList({ changes: [
    { path: 'a.json#25.pre_dst', file: 'a.json', before: 'old', after: 'new' },
    { path: 'b.json#9.proofread_dst', file: 'b.json', before: 'old', after: 'new' },
  ] });
  const dictionary = extractChangeList({ file_key: '(project_dir)pre.txt', line_diff: { rows: [{ op: 'add', line: '猫\tcat' }] } });
  let renderer;
  await act(async () => { renderer = create(createElement(ProjectTranslationPreviewProvider, { projectDir: directory, configFileName: 'special.yml' },
    createElement(ChangeListCard, { data: cache }), createElement(ChangeListCard, { data: dictionary }))); });
  try {
    const cacheRows = () => renderer.root.findAllByProps({ className: 'agent-changes__item' });
    await act(async () => cacheRows()[1].props.onClick());
    const cacheEditor = renderer.root.findByType('cache-editor');
    assert.equal(cacheEditor.props.ctx.projectDir, directory);
    assert.equal(cacheEditor.props.ctx.configFileName, 'special.yml');
    assert.equal(cacheEditor.props.focusTarget.filename, 'b.json');
    assert.equal(cacheEditor.props.focusTarget.index, 9);
    const outside = {};
    act(() => renderer.root.findByProps({ className: 'agent-translation-preview-backdrop' }).props.onClick({ target: outside, currentTarget: outside }));
    assert.equal(renderer.root.findByType('cache-editor').props.active, false);
    let prevented = false;
    await act(async () => renderer.root.findByProps({ className: 'agent-changes__dline agent-changes__dline--add' }).props.onKeyDown({ key: 'Enter', preventDefault() { prevented = true; } }));
    assert.equal(prevented, true);
    const dictionaryEditor = renderer.root.findByType('dictionary-editor');
    assert.equal(dictionaryEditor.props.focusTarget.fileKey, '(project_dir)pre.txt');
    assert.equal(dictionaryEditor.props.focusTarget.line, '猫\tcat');
    await act(async () => dictionaryEditor.props.onOpenInCache('猫'));
    assert.equal(renderer.root.findByType('cache-editor').props.focusTarget.query, '猫');
  } finally { act(() => renderer.unmount()); }
});

test('translation card requests stop for its project, disables the button in flight and retains progress after cancellation', async () => {
  const f = setup();
  let resolveStop;
  f.boundary.stopResponse = new Promise(resolve => { resolveStop = resolve; });
  let renderer;
  await act(async () => { renderer = create(f.view()); });
  const button = () => renderer.root.findAllByType('button').find(node => node.props.className.includes('agent-tjob__stop'));
  assert.equal(button().props.disabled, false);
  act(() => button().props.onClick());
  assert.deepEqual(f.requests, [f.api.encodeProjectDir('C:\\Games\\Example')]);
  assert.equal(button().props.disabled, true);
  assert.equal(button().children.at(-1), '停止中…');
  await act(async () => { f.cancel(); resolveStop({ job_id: 'job-1', status: 'cancelled' }); });
  assert.equal(button(), undefined);
  assert.equal(renderer.root.findByProps({ className: 'agent-tjob__header' }).props['aria-expanded'], true);
  assert.equal(renderer.root.findByProps({ role: 'progressbar' }).props['aria-valuenow'], 8);
  assert.equal(renderer.root.findByProps({ className: 'agent-tjob__state is-done' }).children.at(-1), '已取消');
  act(() => renderer.unmount());
  assert.equal(f.timers.size, 0);
});

test('the header only toggles progress; the expanded body opens the workbench and outside clicks shrink it', async () => {
  const f = setup('running', 'job-1', { preview: true });
  let renderer;
  await act(async () => { renderer = create(f.view(), { createNodeMock: f.nodes }); });
  const surface = () => renderer.root.findByProps({ className: 'agent-tjob__body' });
  const backdrop = () => renderer.root.findByProps({ className: 'agent-translation-preview-backdrop' });
  try {
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    assert.equal(renderer.root.findByProps({ className: 'agent-tjob is-open is-live' }).props.onClick, undefined);
    act(() => renderer.root.findByProps({ className: 'agent-tjob__header' }).props.onClick());
    assert.equal(renderer.root.findByProps({ className: 'agent-tjob__header' }).props['aria-expanded'], false);
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    act(() => renderer.root.findByProps({ className: 'agent-tjob__header' }).props.onClick());
    assert.equal(renderer.root.findByProps({ className: 'agent-tjob__header' }).props['aria-expanded'], true);
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    await act(async () => surface().props.onClick({ target: { closest: () => null } }));
    const workbench = () => renderer.root.findByType('workbench').props;
    assert.equal(workbench().ctx.projectDir, 'C:\\Games\\Example');
    assert.equal(workbench().ctx.projectId, f.api.encodeProjectDir('C:\\Games\\Example'));
    assert.equal(workbench().ctx.configFileName, 'custom.yml');
    assert.equal(workbench().active, true);
    assert.equal(backdrop().props.style.pointerEvents, 'auto');
    const instance = workbench().instance;
    const outside = {};
    act(() => backdrop().props.onClick({ target: {}, currentTarget: outside }));
    assert.equal(workbench().active, true);
    act(() => backdrop().props.onClick({ target: outside, currentTarget: outside }));
    assert.equal(workbench().active, false);
    assert.equal(backdrop().props['aria-hidden'], true);
    assert.equal(backdrop().props.style.pointerEvents, 'none');
    assert.equal(f.requests.length, 0);
    assert.equal(f.restored(), 1);
    await act(async () => surface().props.onClick({ target: { closest: () => null } }));
    assert.equal(workbench().instance, instance);
    assert.equal(workbench().active, true);
    // Work records collapsing cannot destroy the open preview.
    await act(async () => renderer.update(f.view(true, false)));
    assert.equal(workbench().active, true);
    await act(async () => renderer.update(f.view(false, false)));
    assert.equal(workbench().active, false);
    await act(async () => renderer.update(f.view(true)));
    assert.equal(workbench().active, false);
    await act(async () => renderer.update(f.view(true, true, 'D:\\Another')));
    await act(async () => surface().props.onClick({ target: { closest: () => null } }));
    assert.equal(workbench().ctx.projectDir, 'D:\\Another');
    assert.equal(workbench().ctx.configFileName, 'config.yaml');
    assert.notEqual(workbench().instance, instance);
  } finally { act(() => renderer.unmount()); }
  assert.equal(f.timers.size, 0);
  assert.equal([...f.listeners.values()].some(set => set.size), false);
});

test('card controls keep their actions and never open the project preview', async () => {
  const f = setup('running', 'job-1', { preview: true });
  f.boundary.stopResponse = Promise.resolve({ status: 'cancelled' });
  let renderer;
  await act(async () => { renderer = create(f.view()); });
  try {
    const raw = renderer.root.findByProps({ className: 'agent-tjob__raw-toggle' });
    act(() => raw.props.onClick());
    const surface = renderer.root.findByProps({ className: 'agent-tjob__body' });
    act(() => surface.props.onClick({ target: { closest: () => raw } }));
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    assert.equal(raw.props['aria-expanded'], true);
    const stop = renderer.root.findAllByType('button').find(node => node.props.className?.includes('agent-tjob__stop'));
    await act(async () => { f.cancel(); stop.props.onClick(); });
    assert.equal(f.requests.length, 1);
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    act(() => renderer.root.findByProps({ className: 'agent-tjob__header' }).props.onClick());
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    assert.equal(renderer.root.findByProps({ className: 'agent-tjob__header' }).props['aria-expanded'], false);
  } finally { act(() => renderer.unmount()); }
});

for (const animated of [false, true]) {
test(`preview supports keyboard focus, Escape and the animation setting (${animated})`, async () => {
  const f = setup('completed', 'job-1', { preview: true, animated });
  let renderer;
  await act(async () => { renderer = create(f.view(), { createNodeMock: f.nodes }); });
  try {
    await act(async () => renderer.root.findByProps({ className: 'agent-tjob__header' }).props.onClick());
    assert.equal(renderer.root.findAllByType('workbench').length, 0);
    const body = renderer.root.findByProps({ className: 'agent-tjob__body' });
    const element = {};
    await act(async () => body.props.onKeyDown({ key: 'Enter', target: element, currentTarget: element, preventDefault() {} }));
    assert.equal(f.document.activeElement, f.closeNode);
    const dialog = renderer.root.findByProps({ className: 'agent-translation-preview' });
    assert.equal(dialog.props.transition.duration, animated ? 0.2 : 0);
    const press = (key, shiftKey = false) => {
      const event = { key, shiftKey, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      act(() => { for (const fn of f.listeners.get('keydown') || []) fn(event); });
      return event;
    };
    assert.equal(press('Tab', true).defaultPrevented, true);
    assert.equal(f.document.activeElement, f.lastNode);
    assert.equal(press('Tab').defaultPrevented, true);
    assert.equal(f.document.activeElement, f.closeNode);
    press('Escape');
    assert.equal(renderer.root.findByType('workbench').props.active, false);
    assert.equal(f.restored(), 1);
    assert.equal(f.requests.length, 0);
    assert.equal(f.listeners.get('keydown').size, 0);
  } finally { act(() => renderer.unmount()); }
});
}

test('failed stop keeps the job running, shows an error and allows retry', async () => {
  const f = setup();
  let rejectStop;
  f.boundary.stopResponse = new Promise((_, reject) => { rejectStop = reject; });
  let renderer;
  await act(async () => { renderer = create(f.view()); });
  const button = () => renderer.root.findAllByType('button').find(node => node.props.className.includes('agent-tjob__stop'));
  act(() => button().props.onClick());
  await act(async () => rejectStop(new Error('backend rejected stop')));
  assert.equal(button().props.disabled, false);
  assert.equal(renderer.root.findByProps({ role: 'alert' }).children.join(''), 'backend rejected stop');
  f.boundary.stopResponse = Promise.resolve({ job_id: 'job-1', status: 'cancelled' });
  await act(async () => { f.cancel(); button().props.onClick(); });
  assert.equal(f.requests.length, 2);
  assert.equal(renderer.root.findAllByProps({ role: 'alert' }).length, 0);
  act(() => renderer.unmount());
});

test('completed cards have no stop button and a different current job cannot be stopped from an old card', async () => {
  for (const status of ['completed', 'failed', 'cancelled', 'running']) {
    const f = setup(status, 'job-2');
    let renderer;
    await act(async () => { renderer = create(f.view()); });
    if (status !== 'running') act(() => renderer.root.findByProps({ className: 'agent-tjob__header' }).props.onClick());
    const button = renderer.root.findAllByType('button').find(node => node.props.className.includes('agent-tjob__stop'));
    assert.equal(status === 'running' ? button.props.disabled : button === undefined, true);
    assert.equal(f.requests.length, 0);
    act(() => renderer.unmount());
  }
});
