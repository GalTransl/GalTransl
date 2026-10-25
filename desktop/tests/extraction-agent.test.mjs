import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const source = file => new URL(`../src/pages/extraction/${file}`, import.meta.url);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

test('native bash snapshots replace live output and image results retain attachments', () => {
  const { reduceEvents } = createTypeScriptLoader()(source('transcript.ts'));
  const image = { id: `${'a'.repeat(64)}.gif`, name: 'fixture.gif' };
  const rows = reduceEvents([], [
    { type: 'tool_start', toolCallId: 'bash', toolName: 'bash' },
    { type: 'tool_output', toolCallId: 'bash', text: 'old snapshot', replace: true },
    { type: 'tool_output', toolCallId: 'bash', text: '中'.repeat(17000), replace: true },
    { type: 'tool_start', toolCallId: 'image', toolName: 'read' },
    { type: 'tool_end', toolCallId: 'image', text: 'Read image file [image/gif]', images: [image] },
  ]);
  assert.equal(rows[0].text, '中'.repeat(17000));
  assert.deepEqual(rows[1].images, [image]);
});
function snapshot(id, extra = {}) {
  return { session: { id, title: id, status: 'idle', seq: 0 }, events: [], live_events: [], cursor: 0, pending: null, has_more: false, before_seq: null, ...extra };
}
test('streaming text reconciles with authoritative final messages; reasoning and tools retain their own rows', () => {
  const { reduceEvents } = createTypeScriptLoader()(source('transcript.ts'));
  const initial = reduceEvents([], [
    { type: 'user_message', seq: 1, text: 'Extract a synthetic fixture' },
    { type: 'assistant_delta', messageId: 'a', text: 'partial' },
    { type: 'thinking_delta', messageId: 'a', text: 'analysis' },
    { type: 'tool_start', toolCallId: 't', toolName: 'run_python', arguments: { code: 'synthetic' } },
    { type: 'tool_output', toolCallId: 't', text: 'live output' },
  ]);
  const final = reduceEvents(initial, [
    { type: 'assistant_message', messageId: 'a', text: 'final' },
    { type: 'tool_end', toolCallId: 't', text: 'authoritative output', isError: true },
  ]);
  assert.equal(final.length, 3);
  assert.equal(final[1].text, 'final');
  assert.equal(final[1].thinking, 'analysis');
  assert.equal(final[2].text, 'authoritative output');
  assert.equal(final[2].error, true);
  assert.equal(initial[1].text, 'partial');
  assert.equal(final[0], initial[0]);
  assert.equal(reduceEvents([], Array.from({ length: 1500 }, (_, seq) => ({ type: 'user_message', seq, text: 'x' }))).length, 1000);
});

test('late snapshots cannot cross sessions; hiding the page closes only the UI stream and restores live text', async () => {
  let active = true, state;
  const deferred = new Map(), streams = [], calls = [];
  const stored = new Map([['galtransl-extraction-session', 'a']]);
  class Source {
    constructor(url) { this.url = url; streams.push(this); }
    close() { this.closed = true; }
  }
  const api = async (action, params, body) => {
    calls.push({ action, params, body });
    if (action === 'environment') return { ready: true };
    if (action === 'sessions') return { sessions: [], total: 0, next_offset: null };
    if (action === 'session') return new Promise(resolve => deferred.set(params.session_id, resolve));
    throw Error(`Unexpected mutation ${action}`);
  };
  const load = createTypeScriptLoader({ EventSource: Source, localStorage: { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v) } }, {
    '../../components/PageActivity': { usePageActive: () => active, useRetainPage() {} },
    './api': { extractionApi: api, extractionUrl: (action, params) => `${action}/${params.session_id}` },
  });
  const { useExtraction } = load(source('useExtraction.ts'));
  function Probe() { state = useExtraction(); return h('div'); }
  let renderer;
  await act(async () => { renderer = create(h(Probe)); });
  await act(async () => state.select('b'));
  await act(async () => deferred.get('b')(snapshot('b', { session: { id: 'b', status: 'running' }, events: [
    { type: 'context_usage', used_tokens: 32000, window_tokens: 128000 },
  ] })));
  await act(async () => deferred.get('a')(snapshot('a')));
  assert.equal(state.view.session.id, 'b');
  assert.equal(streams.length, 1);
  assert.equal(state.view.contextUsage.used_tokens, 32000);
  await act(async () => {
    streams[0].onmessage({ data: JSON.stringify({ type: 'assistant_delta', messageId: 'live', seq: 1, text: 'in progress' }) });
    await wait(100);
  });
  assert.equal(state.view.rows[0].text, 'in progress');
  await act(async () => {
    streams[0].onmessage({ data: JSON.stringify({ type: 'compaction', active: true }) });
    streams[0].onmessage({ data: JSON.stringify({ type: 'context_usage', used_tokens: null, window_tokens: 128000 }) });
    await wait(100);
  });
  assert.equal(state.view.compacting, true);
  assert.equal(state.view.contextUsage.used_tokens, null);
  await act(async () => { active = false; renderer.update(h(Probe)); });
  assert.equal(streams[0].closed, true);
  assert.equal(calls.some(c => c.action === 'stop'), false);
  await act(async () => { active = true; renderer.update(h(Probe)); });
  await act(async () => deferred.get('b')(snapshot('b', { events: [{ type: 'context_usage', used_tokens: 24000, window_tokens: 128000 }], live_events: [{ type: 'assistant_delta', messageId: 'live', text: 'continued in background' }], cursor: 5 })));
  assert.equal(state.view.rows[0].text, 'continued in background');
  assert.equal(state.view.contextUsage.used_tokens, 24000);
  assert.equal(state.view.compacting, false);
  await act(async () => renderer.unmount());
});

test('live permission changes reach the backend, persist through snapshots and retain mode on failure', async () => {
  let state, source, mode = 'auto', fail = false;
  const calls = [];
  class Source {
    constructor() { source = this; }
    close() {}
  }
  const api = async (action, params, body) => {
    calls.push({ action, params, body });
    if (action === 'environment') return { ready: true };
    if (action === 'sessions') return { sessions: [], total: 0, next_offset: null };
    if (action === 'session') return snapshot('live', { session: { id: 'live', status: 'running', permission_mode: mode } });
    if (action === 'permission-mode') {
      if (fail) throw Error('Synthetic change failed');
      mode = body.permission_mode;
      return { session_id: 'live', permission_mode: mode };
    }
    throw Error(`Unexpected ${action}`);
  };
  const componentLoad = createTypeScriptLoader({ EventSource: Source,
    localStorage: { getItem: () => 'live', setItem() {} } }, {
    '../../components/PageActivity': { usePageActive: () => true, useRetainPage() {} },
    './api': { extractionApi: api, extractionUrl: () => 'synthetic-stream' },
  });
  const { useExtraction } = componentLoad(new URL('../src/pages/extraction/useExtraction.ts', import.meta.url));
  function Probe() { state = useExtraction(); return h('div'); }
  let renderer;
  await act(async () => { renderer = create(h(Probe)); });
  await act(async () => { await state.mutate('permission-mode', { permission_mode: 'auto-quiet' }); });
  assert.equal(state.view.session.permission_mode, 'auto-quiet');
  assert.equal(calls.find(call => call.action === 'permission-mode').body.session_id, 'live');
  fail = true;
  await act(async () => { await state.mutate('permission-mode', { permission_mode: 'ask' }); });
  assert.equal(state.view.session.permission_mode, 'auto-quiet');
  assert.match(state.error, /Synthetic change failed/);
  await act(async () => {
    source.onmessage({ data: JSON.stringify({ type: 'permission_mode', permission_mode: 'ask', seq: 10 }) });
    await wait(100);
  });
  assert.equal(state.view.session.permission_mode, 'ask');
  await act(async () => renderer.unmount());
});

test('model bindings follow the main default, survive session switches and remember explicit follow across reloads', () => {
  const listeners = new Map(), stored = new Map();
  let defaultName = 'primary', state;
  let names = ['primary', 'alternate', 'incompatible'];
  const profiles = { primary: { 'OpenAI-Compatible': {} }, alternate: { 'OpenAI-Compatible': {} }, incompatible: { Other: {} } };
  const componentLoad = createTypeScriptLoader({ window: {
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name),
  } }, {
    '../../lib/api': { AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT: 'default', BACKEND_PROFILES_CHANGE_EVENT: 'profiles',
      getAgentDefaultBackendProfile: () => defaultName, getBackendProfileNames: () => names,
      getBackendProfile: name => profiles[name] },
    '../../lib/projectStorage': { readProjectStorage: key => stored.get(key), writeProjectStorage: (key, value) => stored.set(key, value) },
  });
  const { useModelProfile } = componentLoad(new URL('../src/pages/extraction/useModelProfile.ts', import.meta.url));
  function Probe({ session }) { state = useModelProfile(session, session?.id || ''); return h('div'); }
  const first = { id: 'first', profile_name: '' }, legacy = { id: 'legacy', profile_name: 'alternate' };
  let renderer;
  act(() => { renderer = create(h(Probe, { session: first })); });
  assert.equal(state.models.join(','), 'primary,alternate');
  assert.equal(state.profile, 'primary');
  assert.equal(state.boundProfile, '');
  act(() => state.setProfile('primary'));
  defaultName = 'alternate';
  act(() => listeners.get('default')());
  assert.equal(state.defaultProfile, 'alternate');
  assert.equal(state.profile, 'primary');
  act(() => state.setProfile(''));
  assert.equal(state.profile, 'alternate');
  assert.equal(state.boundProfile, '');
  act(() => renderer.update(h(Probe, { session: legacy })));
  assert.equal(state.boundProfile, 'alternate');
  act(() => state.setProfile(''));
  defaultName = 'primary';
  act(() => listeners.get('default')());
  assert.equal(state.profile, 'primary');
  act(() => renderer.update(h(Probe, { session: first })));
  assert.equal(state.profile, 'primary');
  act(() => renderer.unmount());
  assert.equal(listeners.size, 0);
  act(() => { renderer = create(h(Probe, { session: legacy })); });
  assert.equal(state.boundProfile, '');
  assert.equal(state.profile, 'primary');
  defaultName = 'incompatible';
  names = ['alternate', 'incompatible'];
  act(() => listeners.get('profiles')());
  assert.equal(state.defaultProfile, 'alternate');
  assert.equal(state.profile, 'alternate');
  act(() => renderer.update(h(Probe, {})));
  act(() => state.setProfile('alternate'));
  act(() => state.moveProfile('new-session'));
  act(() => renderer.update(h(Probe, { session: { id: 'new-session', profile_name: '' } })));
  assert.equal(state.boundProfile, 'alternate');
  act(() => renderer.unmount());
});
