import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const fixture = (id, game_dir, status = 'idle') => ({ id, game_dir, title: `Session ${id}`, status });
const storage = stored => ({
  getItem: key => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, value),
  removeItem: key => stored.delete(key),
});

test('extraction folders group sessions by full game path and retain folds across reloads', () => {
  const stored = new Map(), selected = [];
  const load = createTypeScriptLoader({ localStorage: storage(stored) });
  const { SessionFolders, gameFolderKey } = load(new URL('../src/pages/extraction/SessionFolders.tsx', import.meta.url));
  const sessions = [fixture('a', 'C:\\Games\\同名游戏'), fixture('b', 'D:\\Games\\同名游戏'),
    fixture('c', 'c:/games/同名游戏/'), fixture('d', '/games/Other/')];
  const props = { sessions, selected: 'a', activeSession: { ...sessions[0], status: 'running' }, onSelect: id => selected.push(id) };
  assert.notEqual(gameFolderKey('/games/Case'), gameFolderKey('/games/case'));
  assert.equal(gameFolderKey('\\\\server\\share\\Game\\'), gameFolderKey('//SERVER/share/game'));
  let renderer;
  const mount = () => { renderer = create(h(SessionFolders, props)); };
  act(mount);
  const folders = () => renderer.root.findAllByProps({ className: 'agent-sessions__group-toggle' });
  assert.equal(folders().length, 3);
  assert.deepEqual(renderer.root.findAllByProps({ className: 'agent-sessions__group-name' }).map(node => node.children.join('')), ['同名游戏', '同名游戏', 'Other']);
  assert.equal(renderer.root.findAllByProps({ className: 'agent-sessions__group-count' })[0].children.join(''), '2');
  assert.equal(renderer.root.findAllByProps({ className: 'agent-session-item__time' })[0].children.join(''), '工作中');
  const sessionButton = id => renderer.root.findByProps({ title: `Session ${id}` });
  act(() => sessionButton('c').props.onClick());
  assert.deepEqual(selected, ['c']);
  act(() => folders()[0].props.onClick());
  assert.equal(folders()[0].props['aria-expanded'], false);
  assert.equal(renderer.root.findAllByProps({ title: 'Session a' }).length, 0);
  assert.equal(renderer.root.findAllByProps({ title: 'Session c' }).length, 0);
  assert.equal(renderer.root.findAllByProps({ title: 'Session b' }).length, 1);
  act(() => renderer.unmount());
  act(mount);
  assert.equal(folders()[0].props['aria-expanded'], false);
  act(() => renderer.update(h(SessionFolders, { ...props, selected: '' })));
  act(() => renderer.update(h(SessionFolders, { ...props, selected: 'c', activeSession: undefined })));
  assert.equal(folders()[0].props['aria-expanded'], true);
  assert.equal(sessionButton('c').props['aria-current'], 'true');
  act(() => renderer.unmount());
});

test('folder state tolerates invalid or unavailable browser storage', () => {
  for (const value of ['invalid JSON', '{}', '[null, 1, false]']) {
    const load = createTypeScriptLoader({ localStorage: { getItem: () => value, setItem() { throw Error('quota'); } } });
    const { SessionFolders } = load(new URL('../src/pages/extraction/SessionFolders.tsx', import.meta.url));
    let renderer;
    act(() => { renderer = create(h(SessionFolders, { sessions: [fixture('a', '/game')], selected: '', onSelect() {} })); });
    act(() => renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props.onClick());
    assert.equal(renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props['aria-expanded'], false);
    act(() => renderer.unmount());
  }
});

test('new extraction buttons return home without opening a dialog or stopping the session; source selection stays explicit', async () => {
  const stored = new Map([['galtransl-extraction-session', 'live']]), calls = [], streams = [];
  const session = fixture('live', 'C:\\Games\\Example', 'running');
  let state;
  class Source {
    constructor() { streams.push(this); }
    close() { this.closed = true; }
  }
  const load = createTypeScriptLoader({ localStorage: storage(stored), EventSource: Source, window: { innerWidth: 1500 } }, {
    '../styles/pages/agent.css': {}, '../styles/pages/extraction.css': {},
    'react-router-dom': { useNavigate: () => () => {} },
    '../lib/api': { getBackendProfile() {} },
    './HomePage': { addProjectToHistory() {} },
    '../../components/PageActivity': { usePageActive: () => true, useRetainPage() {} },
    './api': { extractionUrl: () => 'fixture-stream', extractionApi: async (action, params, body) => {
      calls.push({ action, params, body });
      if (action === 'environment') return { ready: true };
      if (action === 'sessions') return { sessions: [session], total: 1, next_offset: null };
      if (action === 'session') return { session, events: [{ type: 'user_message', seq: 1, text: 'Inspect fixture' }],
        live_events: [], cursor: 1, pending: null, has_more: false, before_seq: null };
      throw Error(`Unexpected mutation: ${action}`);
    } },
    './extraction/useExtraction': { useExtraction: () => state },
    './extraction/useModelProfile': { useModelProfile: () => ({ models: ['fixture'], profile: 'fixture',
      boundProfile: 'fixture', defaultProfile: 'fixture', setProfile() {}, moveProfile() {} }) },
    './agent/images': { useImageInput: () => ({ images: [] }) },
    './extraction/Composer': { ExtractionComposer: props => h('textarea', { value: props.draft, onChange: props.setDraft }) },
    './extraction/Workspace': { Workspace: () => h('div', { className: 'test-workspace' }) },
    './extraction/Conversation': { Conversation: () => h('div', { className: 'test-conversation' }) },
    './extraction/Dialogs': { CreateDialog: () => h('div', { role: 'dialog' }) },
  });
  const { useExtraction } = load(new URL('../src/pages/extraction/useExtraction.ts', import.meta.url));
  const { ExtractionPage } = load(new URL('../src/pages/ExtractionPage.tsx', import.meta.url));
  const { saveMessageDraft } = load(new URL('../src/pages/agent/drafts.ts', import.meta.url));
  saveMessageDraft('galtransl-extraction', 'live', 'existing conversation draft');
  saveMessageDraft('galtransl-extraction', '', 'new conversation draft');
  function Harness() { state = useExtraction(); return h(ExtractionPage); }
  let renderer;
  await act(async () => { renderer = create(h(Harness)); });
  assert.equal(renderer.root.findAllByProps({ className: 'test-conversation' }).length, 1);
  assert.equal(renderer.root.findByType('textarea').props.value, 'existing conversation draft');
  await act(async () => renderer.root.findByProps({ className: 'agent-sessions__new' }).props.onClick());
  assert.equal(state.selected, '');
  assert.equal(state.view, null);
  assert.equal(stored.get('galtransl-extraction-session'), '');
  assert.equal(streams[0].closed, true);
  assert.equal(renderer.root.findAllByProps({ role: 'dialog' }).length, 0);
  assert.equal(renderer.root.findAllByProps({ className: 'test-conversation' }).length, 0);
  assert.equal(renderer.root.findAllByProps({ className: 'test-workspace' }).length, 0);
  assert.equal(renderer.root.findByProps({ className: 'agent-hero__title' }).children.join(''), '从游戏目录开始');
  assert.equal(renderer.root.findByType('textarea').props.value, 'new conversation draft');
  assert.equal(calls.some(call => ['stop', 'create'].includes(call.action)), false);
  await act(async () => renderer.root.findByProps({ title: session.title }).props.onClick());
  assert.equal(state.selected, 'live');
  assert.equal(renderer.root.findByType('textarea').props.value, 'existing conversation draft');
  const newButton = renderer.root.findAllByProps({ 'aria-label': '新建提取' }).find(node => node.type === 'button' && node.props.className === 'agent-console__icon-btn');
  await act(async () => newButton.props.onClick());
  assert.equal(state.selected, '');
  assert.equal(renderer.root.findAllByProps({ role: 'dialog' }).length, 0);
  act(() => renderer.root.findByProps({ className: 'agent-hero__action' }).props.onClick());
  assert.equal(renderer.root.findAllByProps({ role: 'dialog' }).length, 1);
  await act(async () => renderer.unmount());
});
