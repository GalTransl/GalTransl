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
  const stored = new Map(), selected = [], created = [];
  const load = createTypeScriptLoader({ localStorage: storage(stored) });
  const { SessionFolders, gameFolderKey } = load(new URL('../src/pages/extraction/SessionFolders.tsx', import.meta.url));
  const sessions = [fixture('a', 'C:\\Games\\同名游戏'), fixture('b', 'D:\\Games\\同名游戏'),
    fixture('c', 'c:/games/同名游戏/'), fixture('d', '/games/Other/')];
  const props = { sessions, selected: 'a', activeSession: { ...sessions[0], status: 'running' }, onSelect: id => selected.push(id),
    onCreateInFolder: directory => created.push(directory), busy: false };
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
  const newButtons = () => renderer.root.findAllByProps({ className: 'agent-sessions__group-new' });
  assert.equal(newButtons().length, 3);
  act(() => newButtons()[0].props.onClick());
  act(() => newButtons()[1].props.onClick());
  assert.deepEqual(created, ['C:\\Games\\同名游戏', 'D:\\Games\\同名游戏']);
  assert.equal(folders()[0].props['aria-expanded'], false);
  assert.deepEqual(selected, ['c']);
  act(() => renderer.update(h(SessionFolders, { ...props, busy: true })));
  assert.equal(newButtons().every(button => button.props.disabled), true);
  // Keep the body mounted for the same grid/opacity closing transition as translation.
  const collapse = () => renderer.root.findAllByProps({ className: 'agent-sessions__group-collapse' })[0];
  assert.equal(collapse().props['aria-hidden'], true);
  assert.equal(collapse().props.inert, '');
  assert.equal(collapse().props.hidden, undefined);
  assert.equal(renderer.root.findAllByProps({ title: 'Session a' }).length, 1);
  assert.equal(renderer.root.findAllByProps({ title: 'Session c' }).length, 1);
  assert.equal(renderer.root.findAllByProps({ title: 'Session b' }).length, 1);
  act(() => renderer.unmount());
  act(mount);
  assert.equal(folders()[0].props['aria-expanded'], false);
  act(() => renderer.update(h(SessionFolders, { ...props, selected: '' })));
  act(() => renderer.update(h(SessionFolders, { ...props, selected: 'c', activeSession: undefined })));
  assert.equal(folders()[0].props['aria-expanded'], true);
  assert.equal(collapse().props['aria-hidden'], false);
  assert.equal(collapse().props.inert, undefined);
  assert.equal(sessionButton('c').props['aria-current'], 'true');
  act(() => renderer.unmount());
});

test('folder state tolerates invalid or unavailable browser storage', () => {
  for (const value of ['invalid JSON', '{}', '[null, 1, false]']) {
    const load = createTypeScriptLoader({ localStorage: { getItem: () => value, setItem() { throw Error('quota'); } } });
    const { SessionFolders } = load(new URL('../src/pages/extraction/SessionFolders.tsx', import.meta.url));
    let renderer;
    act(() => { renderer = create(h(SessionFolders, { sessions: [fixture('a', '/game')], selected: '', onSelect() {}, onCreateInFolder() {}, busy: false })); });
    act(() => renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props.onClick());
    assert.equal(renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props['aria-expanded'], false);
    act(() => renderer.unmount());
  }
});

test('new extraction buttons return home without opening a dialog or stopping the session; source selection stays explicit', async () => {
  const stored = new Map([['galtransl-extraction-session', 'live']]), calls = [], streams = [];
  const session = fixture('live', 'C:\\Games\\Example', 'running');
  const newSession = fixture('new', session.game_dir);
  let created = false;
  const movedProfiles = [], movedImages = [];
  let state;
  class Source {
    constructor() { streams.push(this); }
    close() { this.closed = true; }
  }
  const load = createTypeScriptLoader({ localStorage: storage(stored), EventSource: Source,
    window: { innerWidth: 1500, matchMedia: () => ({ matches: true }) },
    document: { hidden: false, documentElement: { dataset: {} } } }, {
    '../styles/pages/agent.css': {}, '../styles/pages/extraction.css': {},
    'react-router-dom': { useNavigate: () => () => {} },
    '../lib/api': { getBackendProfile() {} },
    './HomePage': { addProjectToHistory() {} },
    '../../components/PageActivity': { usePageActive: () => true, useRetainPage() {} },
    './api': { extractionUrl: () => 'fixture-stream', extractionApi: async (action, params, body) => {
      calls.push({ action, params, body });
      if (action === 'environment') return { ready: true };
      if (action === 'sessions') return { sessions: created ? [newSession, session] : [session], total: created ? 2 : 1, next_offset: null };
      if (action === 'create') { created = true; return { session: newSession }; }
      if (action === 'session') return { session: params.session_id === newSession.id ? newSession : session,
        events: params.session_id === newSession.id ? [] : [{ type: 'user_message', seq: 1, text: 'Inspect fixture' }],
        live_events: [], cursor: 1, pending: null, has_more: false, before_seq: null };
      throw Error(`Unexpected mutation: ${action}`);
    } },
    './extraction/useExtraction': { useExtraction: () => state },
    './extraction/useModelProfile': { useModelProfile: () => ({ models: ['fixture'], profile: 'fixture',
      boundProfile: 'fixture', defaultProfile: 'fixture', setProfile() {}, moveProfile: id => movedProfiles.push(id) }) },
    './agent/images': { useImageInput: () => ({ images: [], move: target => movedImages.push(target) }) },
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
  const workspace = renderer.root.findByProps({ className: 'test-workspace' });
  const toggle = () => renderer.root.findByProps({ 'aria-controls': 'extraction-workspace' });
  const panel = () => renderer.root.findByProps({ id: 'extraction-workspace' });
  // Keep browsing state mounted through both directions, while folded controls cannot receive focus.
  act(() => toggle().props.onClick());
  assert.equal(toggle().props['aria-expanded'], false);
  assert.equal(panel().props.inert, '');
  assert.equal(panel().props['aria-hidden'], true);
  assert.equal(renderer.root.findByProps({ className: 'test-workspace' }), workspace);
  act(() => toggle().props.onClick());
  assert.equal(toggle().props['aria-expanded'], true);
  assert.equal(panel().props.inert, undefined);
  assert.equal(renderer.root.findByProps({ className: 'test-workspace' }), workspace);
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
  // Folder creation skips source selection and leaves both existing drafts/attachments in place.
  await act(async () => renderer.root.findByProps({ title: session.title }).props.onClick());
  act(() => renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props.onClick());
  await act(async () => renderer.root.findByProps({ className: 'agent-sessions__group-new' }).props.onClick());
  assert.equal(state.selected, 'new');
  const creation = calls.filter(call => call.action === 'create');
  assert.equal(creation.length, 1);
  assert.equal(creation[0].body.game_dir, session.game_dir);
  assert.equal(creation[0].body.output_dir, '');
  assert.equal(calls.some(call => call.action === 'stop'), false);
  assert.equal(renderer.root.findAllByProps({ role: 'dialog' }).length, 0);
  assert.equal(renderer.root.findByProps({ className: 'agent-sessions__group-toggle' }).props['aria-expanded'], true);
  assert.equal(renderer.root.findByProps({ className: 'agent-sessions__group-count' }).children.join(''), '2');
  assert.equal(renderer.root.findByType('textarea').props.value, '');
  assert.deepEqual(movedProfiles, ['new']);
  assert.deepEqual(movedImages, []);
  await act(async () => renderer.root.findByProps({ title: session.title }).props.onClick());
  assert.equal(renderer.root.findByType('textarea').props.value, 'existing conversation draft');
  await act(async () => renderer.root.findByProps({ className: 'agent-sessions__new' }).props.onClick());
  assert.equal(renderer.root.findByType('textarea').props.value, 'new conversation draft');
  await act(async () => renderer.unmount());
});
