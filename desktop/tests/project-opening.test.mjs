import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import * as router from 'react-router-dom';
const { MemoryRouter, useLocation, useNavigate } = router;

function fixture(initial = {}, blocked = true, options = {}) {
  const values = new Map(Object.entries(initial));
  const listeners = new Map();
  const events = [];
  const mocks = {
    '@tauri-apps/api/core': { invoke: async () => ({}) },
    '@tauri-apps/plugin-dialog': { open: async () => options.selected ?? null },
    '@tauri-apps/api/webviewWindow': { getCurrentWebviewWindow: () => ({ onDragDropEvent: async () => () => {} }) },
    '../components/Button': { Button: props => h('button', props) },
    '../components/CustomSelect': { CustomSelect: () => null },
    '../components/GettingStarted': { GettingStarted: () => null },
    '../components/page-state': { InlineFeedback: () => null },
    ...options.mocks,
  };
  const window = {
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    setTimeout: () => 1, clearTimeout() {},
    setInterval: () => 1, clearInterval() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatchEvent(event) {
      events.push(event);
      for (const fn of listeners.get(event.type) || []) fn(event);
      return true;
    },
  };
  const localStorage = {
    get length() { return values.size; },
    key(index) { return Array.from(values.keys())[index] ?? null; },
    getItem: key => values.get(key) ?? null,
    setItem(key, value) {
      const text = String(value);
      if (blocked) throw new DOMException('Storage full', 'QuotaExceededError');
      const capacity = options.capacity ?? Infinity;
      if (Number.isFinite(capacity)) {
        let used = 0;
        for (const current of values.values()) used += current.length;
        const replaced = values.get(key)?.length ?? 0;
        if (used - replaced + text.length > capacity) {
          throw new DOMException('Storage full', 'QuotaExceededError');
        }
      }
      values.set(key, text);
    },
    removeItem(key) { values.delete(key); },
  };
  const load = createTypeScriptLoader({
    window,
    document: { documentElement: { dataset: {} } },
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    localStorage,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    TextEncoder, TextDecoder, btoa, atob,
  }, mocks);
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  const home = load(new URL('../src/pages/HomePage.tsx', import.meta.url));
  const agent = load(new URL('../src/pages/agent/storage.ts', import.meta.url));
  return { api, home, agent, window, events, values, load, mocks, unblock() { blocked = false; } };
}

function LocationProbe() { return h('location', { path: useLocation().pathname }); }

test('the home-page open action navigates to the selected YAML despite a quota error', async () => {
  const f = fixture({}, true, { selected: 'C:\\游戏项目\\custom.yml' });
  Object.assign(f.api, {
    fetchVersion: async () => 'test', fetchVersionCheck: async () => ({}), fetchJobs: async () => [],
  });
  const opened = [];
  let root;
  await act(async () => {
    root = create(h(MemoryRouter, null, h(f.home.HomePage, {
      onOpenProject(dir, cfg) { opened.push([dir, cfg]); f.api.addOpenProject(dir, cfg); },
    }), h(LocationProbe)));
  });
  try {
    const button = root.root.findAllByType('button').find(node => node.props.children === '打开项目');
    assert.ok(button);
    await act(async () => { await button.props.onClick(); });
    assert.deepEqual(opened, [['C:\\游戏项目', 'custom.yml']]);
    assert.equal(root.root.findByType('location').props.path, `/project/${f.api.encodeProjectDir('C:\\游戏项目')}/translate`);
    assert.equal(f.api.readConfigFileName('C:\\游戏项目'), 'custom.yml');
  } finally {
    act(() => root.unmount());
  }
});

test('the creation wizard reaches completion and opens the created project with full storage', async () => {
  const f = fixture();
  let projectCreated = false;
  Object.assign(f.api, {
    fetchProgramDir: async () => 'C:\\projects',
    fetchDefaultProjectConfigTemplate: async () => 'common: {}',
    fetchProjectConfig: async () => {
      if (!projectCreated) { projectCreated = true; throw new Error('Not created'); }
      return { config: {} };
    },
    fetchProjectFiles: async () => ({ input_files: [] }),
    fetchPlugins: async () => [], fetchTranslationGuidelines: async () => [],
    detectFilePlugin: async () => ({ counts: {}, unknown: [] }), updateProjectConfig: async () => ({}),
  });
  const { NewProjectWizard } = f.load(new URL('../src/pages/NewProjectWizard.tsx', import.meta.url));
  let opened = false;
  let root;
  await act(async () => {
    root = create(h(MemoryRouter, { initialEntries: ['/new-project'] }, h(NewProjectWizard, {
      active: true, onProjectNameChange() {},
      onOpenProject(dir, cfg) {
        f.api.addOpenProject(dir, cfg);
        f.home.addProjectToHistory(dir, cfg);
        opened = true;
      },
    }), h(LocationProbe)));
  });
  try {
    const name = root.root.findAllByType('input').find(node => node.props.value === '');
    await act(async () => { name.props.onChange({ target: { value: '游戏' } }); });
    for (let step = 0; step < 5; step++) {
      const nav = root.root.find(node => node.props.className === 'wizard-nav');
      const next = nav.findAllByType('button').at(-1);
      assert.equal(next.props.disabled, false);
      await act(async () => { next.props.onClick(); });
    }
    const finish = root.root.find(node => node.props.className === 'wizard-nav').findAllByType('button').at(-1);
    await act(async () => { finish.props.onClick(); });
    assert.equal(opened, true);
    assert.equal(root.root.findByType('location').props.path, `/project/${f.api.encodeProjectDir('C:\\projects\\游戏')}/translate`);
  } finally {
    act(() => root.unmount());
  }
});

test('full browser storage cannot interrupt project history updates or lose the session history', () => {
  const f = fixture();
  assert.doesNotThrow(() => f.home.addProjectToHistory('C:\\游戏项目', 'custom.yml'));
  assert.equal(f.agent.readHistory()[0].projectDir, 'C:\\游戏项目');
  assert.equal(f.agent.readHistory()[0].configFileName, 'custom.yml');
  assert.doesNotThrow(() => f.home.removeProjectFromHistory('C:\\游戏项目'));
  assert.equal(f.agent.readHistory().length, 0);
});

test('opening an Agent project still updates subscribers and custom config when storage is full', () => {
  const f = fixture({ 'galtransl-open-projects': JSON.stringify(['old']) });
  let observed;
  f.window.addEventListener(f.api.OPEN_PROJECTS_CHANGE_EVENT, () => { observed = f.api.loadOpenProjects(); });
  f.api.addOpenProject('new', 'custom.yml');
  assert.deepEqual(Array.from(observed || []), ['new', 'old']);
  assert.equal(f.api.readConfigFileName('new'), 'custom.yml');
  f.api.addOpenProject('another');
  assert.deepEqual(Array.from(f.api.loadOpenProjects()), ['another', 'new', 'old']);
  f.api.saveOpenProjects(['old']);
  assert.deepEqual(Array.from(f.api.loadOpenProjects()), ['old']);
});

test('malformed project history and open-project metadata are ignored', () => {
  const f = fixture({
    'galtransl-project-history': JSON.stringify([null, { projectDir: 3 }, { projectDir: 'valid', configFileName: 'config.yaml', lastOpened: '' }]),
    'galtransl-open-projects': JSON.stringify({ invalid: true }),
  });
  assert.doesNotThrow(() => f.home.addProjectToHistory('new', 'config.yaml'));
  assert.deepEqual(Array.from(f.agent.readHistory(), entry => entry.projectDir), ['new', 'valid']);
  assert.doesNotThrow(() => f.api.addOpenProject('new'));
  assert.deepEqual(Array.from(f.api.loadOpenProjects()), ['new']);
});

test('successful writes recover from the session fallback and survive a reload', () => {
  const f = fixture();
  f.api.addOpenProject('new', 'custom.yml');
  f.home.addProjectToHistory('new', 'custom.yml');
  f.unblock();
  f.api.addOpenProject('another');
  f.home.addProjectToHistory('another', 'config.yaml');
  const reloaded = fixture(Object.fromEntries(f.values), false);
  assert.deepEqual(Array.from(reloaded.api.loadOpenProjects()), ['another', 'new']);
  assert.equal(reloaded.api.readConfigFileName('new'), 'custom.yml');
  assert.deepEqual(Array.from(reloaded.agent.readHistory(), entry => entry.projectDir), ['another', 'new']);
});

test('model settings and per-session choices remain usable when storage is full', async () => {
  const f = fixture();
  await f.api.createBackendProfile('saved', { 'OpenAI-Compatible': { tokens: [] } });
  assert.ok(f.api.getBackendProfile('saved'));
  f.api.setDefaultBackendProfile('saved');
  f.api.setAgentDefaultBackendProfile('saved');
  f.api.setGenDicDefaultBackendProfile('saved');
  f.api.setSubagentDefaultBackendProfile('saved');
  assert.equal(f.api.getDefaultBackendProfile(), 'saved');
  assert.equal(f.api.getAgentDefaultBackendProfile(), 'saved');
  assert.equal(f.api.getGenDicDefaultBackendProfile(), 'saved');
  assert.equal(f.api.getSubagentDefaultBackendProfile(), 'saved');
  f.api.setSelectedBackendProfile('p', '');
  assert.equal(f.api.getSelectedBackendProfile('p'), '');
  f.api.setSelectedBackendProfile('p', '__default__');
  assert.equal(f.api.getSelectedBackendProfile('p'), 'saved');
  f.agent.saveSessionBackends({ p: { s: 'saved' } });
  assert.equal(f.agent.loadSessionBackends().p.s, 'saved');
  f.agent.saveActiveSessionId('p', 's');
  assert.equal(f.agent.loadActiveSessionId('p'), 's');
  f.agent.saveActiveSessionId('p', '');
  assert.equal(f.agent.loadActiveSessionId('p'), '');
});

test('a full store evicts the oldest Agent transcript so project metadata still saves', () => {
  const f = fixture({
    'galtransl-agent-session:old:s': JSON.stringify({ projectDir: 'old', sessionId: 's', events: [], startedAt: 1, finishedAt: 2 }),
    'galtransl-agent-session:new:s': JSON.stringify({ projectDir: 'new', sessionId: 's', events: [], startedAt: 3, finishedAt: 4 }),
    'galtransl-open-projects': JSON.stringify(['old']),
  }, false, { capacity: 170 });
  f.api.addOpenProject('another', 'config.yaml');
  assert.deepEqual(Array.from(f.api.loadOpenProjects()), ['another', 'old']);
  assert.equal(f.api.readConfigFileName('another'), 'config.yaml');
  assert.equal(f.values.has('galtransl-agent-session:old:s'), false);
  assert.equal(f.values.has('galtransl-agent-session:new:s'), true);
});

test('successful transcript writes keep the Agent cache under its budget', () => {
  const f = fixture({}, false);
  const storage = f.load(new URL('../src/lib/projectStorage.ts', import.meta.url));
  const pad = 'x'.repeat(800_000);
  storage.writeProjectStorage('galtransl-agent-session:p:old', JSON.stringify({ startedAt: 1, finishedAt: 2, pad }));
  storage.writeProjectStorage('galtransl-agent-session:p:new', JSON.stringify({ startedAt: 3, finishedAt: 4, pad }));
  assert.equal(storage.AGENT_SESSION_CACHE_BUDGET, 1_500_000);
  assert.equal(f.values.has('galtransl-agent-session:p:old'), false);
  assert.equal(f.values.has('galtransl-agent-session:p:new'), true);
});

test('project metadata can drop a stale legacy background when nothing else can be freed', () => {
  const f = fixture({ 'galtransl-custom-background': 'x'.repeat(200) }, false, { capacity: 210 });
  f.api.addOpenProject('p', 'config.yaml');
  assert.equal(f.values.has('galtransl-custom-background'), false);
  assert.deepEqual(Array.from(f.api.loadOpenProjects()), ['p']);
  assert.equal(f.api.readConfigFileName('p'), 'config.yaml');
});

test('App broadcasts project opens and closes to already mounted subscribers', async () => {
  const mocks = {
    'react-router-dom': { ...router, HashRouter: props => h(MemoryRouter, props) },
    '../features/connection/ConnectionContext': {
      ConnectionProvider: ({ children }) => children,
      useConnection: () => ({ backendUrl: '', connectionPhase: 'offline' }),
    },
    '../features/connection/BootstrapGate': { BootstrapGate: ({ children }) => children },
    '../features/update/UpdateNotice': { UpdateNotice: () => null },
    '../components/Sidebar': { Sidebar: props => h('sidebar', props) },
    '../components/ProjectLayout': { ProjectLayout: () => h('project') },
    '../pages/NewProjectWizard': { NewProjectWizard: props => {
      const navigate = useNavigate();
      return h('wizard', { finish() { props.onOpenProject('created', 'config.yaml'); navigate('/project/created/translate'); } });
    } },
  };
  const f = fixture({}, true, { mocks });
  f.mocks['../pages/HomePage'] = {
    ...f.home,
    HomePage: props => {
      const navigate = useNavigate();
      return h('home', { open: props.onOpenProject, wizard: () => navigate('/new-project') });
    },
  };
  const { App } = f.load(new URL('../src/app/App.tsx', import.meta.url));
  let observed = [];
  f.window.addEventListener(f.api.OPEN_PROJECTS_CHANGE_EVENT, () => { observed = Array.from(f.api.loadOpenProjects()); });
  let root;
  await act(async () => { root = create(h(App)); });
  try {
    await act(async () => root.root.findByType('home').props.open('opened', 'custom.yml'));
    assert.deepEqual(observed, ['opened']);
    assert.deepEqual(Array.from(root.root.findByType('sidebar').props.openProjects), ['opened']);
    await act(async () => root.root.findByType('home').props.wizard());
    await act(async () => root.root.findByType('wizard').props.finish());
    assert.equal(root.root.findAllByType('wizard').length, 0);
    assert.deepEqual(observed, ['created', 'opened']);
    await act(async () => root.root.findByType('sidebar').props.onCloseAllProjects());
    assert.deepEqual(observed, []);
    assert.deepEqual(Array.from(root.root.findByType('sidebar').props.openProjects), []);
  } finally {
    act(() => root.unmount());
  }
});
