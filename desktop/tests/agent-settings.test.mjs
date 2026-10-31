import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function setup(fetchAppSettings, updateAppSettings = async patch => patch, agent = 'translation') {
  const connection = { backendUrl: 'http://localhost:8000' };
  const listeners = new Map();
  const load = createTypeScriptLoader({ document: {
    body: {},
    documentElement: {},
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: type => listeners.delete(type),
  } }, {
    'react-dom': { createPortal: children => children },
    '../lib/api': { fetchAppSettings, updateAppSettings },
    '../../features/connection/ConnectionContext': { useConnection: () => connection },
  });
  const { AgentContextSettings } = load(new URL('../src/components/AgentSettings.tsx', import.meta.url));
  return { Component: props => createElement(AgentContextSettings, { agent, backendUrl: connection.backendUrl, ...props }), connection, load, listeners };
}
const input = view => view.root.findByProps({ role: 'switch' });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test('Python core setting loads, saves a partial patch and waits for the server', async () => {
  const loading = deferred(), saving = deferred();
  const patches = [];
  const { Component } = setup(() => loading.promise, patch => { patches.push(patch); return saving.promise; });
  let view;
  await act(async () => { view = create(createElement(Component)); });
  try {
    assert.equal(input(view).props.disabled, true);
    await act(async () => { loading.resolve({ billionContextEnabled: false }); });
    assert.equal(input(view).props.checked, false);
    assert.equal(input(view).props.disabled, false);
    await act(async () => { input(view).props.onChange({ target: { checked: true } }); });
    assert.equal(JSON.stringify(patches), '[{"billionContextEnabled":true}]');
    assert.equal(input(view).props.disabled, true);
    assert.equal(input(view).props.checked, false);
    await act(async () => { saving.resolve({ billionContextEnabled: true }); });
    assert.equal(input(view).props.checked, true);
    assert.equal(input(view).props.disabled, false);
  } finally { act(() => view.unmount()); }
});

test('load failure disables unknown state and retry reloads it', async () => {
  let attempts = 0;
  const { Component } = setup(async () => {
    if (++attempts === 1) throw new Error('backend offline');
    return { billionContextEnabled: true };
  });
  let view;
  await act(async () => { view = create(createElement(Component)); });
  try {
    assert.equal(input(view).props.disabled, true);
    assert.match(view.root.findByProps({ role: 'alert' }).children.join(''), /backend offline/);
    await act(async () => { view.root.findByType('button').props.onClick(); });
    assert.equal(input(view).props.checked, true);
    assert.equal(input(view).props.disabled, false);
    assert.equal(view.root.findAllByProps({ role: 'alert' }).length, 0);
  } finally { act(() => view.unmount()); }
});

test('failed save preserves confirmed state and permits retry', async () => {
  let attempts = 0;
  const { Component } = setup(async () => ({ billionContextEnabled: false }), async patch => {
    if (++attempts === 1) throw new Error('save failed');
    return patch;
  });
  let view;
  await act(async () => { view = create(createElement(Component)); });
  try {
    await act(async () => { input(view).props.onChange({ target: { checked: true } }); });
    assert.equal(input(view).props.checked, false);
    assert.equal(input(view).props.disabled, false);
    assert.equal(view.root.findAllByProps({ role: 'alert' }).length, 1);
    await act(async () => { input(view).props.onChange({ target: { checked: true } }); });
    assert.equal(input(view).props.checked, true);
  } finally { act(() => view.unmount()); }
});

test('changing backend discards stale loads and saves', async () => {
  const old = deferred(), save = deferred();
  let calls = 0;
  const { Component, connection } = setup(() => ++calls === 1 ? old.promise : Promise.resolve({ billionContextEnabled: false }), () => save.promise);
  let view;
  await act(async () => { view = create(createElement(Component)); });
  try {
    connection.backendUrl = 'http://localhost:9000';
    await act(async () => { view.update(createElement(Component)); });
    await act(async () => { old.resolve({ billionContextEnabled: true }); });
    assert.equal(input(view).props.checked, false);
    await act(async () => { input(view).props.onChange({ target: { checked: true } }); });
    connection.backendUrl = 'http://localhost:10000';
    await act(async () => { view.update(createElement(Component)); });
    await act(async () => { save.resolve({ billionContextEnabled: true }); });
    assert.equal(input(view).props.checked, false);
    assert.equal(input(view).props.disabled, false);
  } finally { act(() => view.unmount()); }
});

test('translation and extraction settings save independently and persist after reopening', async () => {
  let settings = { billionContextEnabled: false, extractionBillionContextEnabled: true };
  const patches = [];
  const { load } = setup(async () => settings, async patch => {
    patches.push(patch);
    settings = { ...settings, ...patch };
    return settings;
  });
  const { AgentContextSettings } = load(new URL('../src/components/AgentSettings.tsx', import.meta.url));
  const controls = ['translation', 'extraction'].map(agent => createElement(AgentContextSettings, { key: agent, agent, backendUrl: 'http://localhost:8000' }));
  let view;
  await act(async () => { view = create(createElement('div', {}, controls)); });
  try {
    const switches = () => view.root.findAllByProps({ role: 'switch' });
    assert.deepEqual(switches().map(node => node.props.checked), [false, true]);
    await act(async () => switches()[0].props.onChange({ target: { checked: true } }));
    assert.deepEqual(switches().map(node => node.props.checked), [true, true]);
    await act(async () => switches()[1].props.onChange({ target: { checked: false } }));
    assert.deepEqual(switches().map(node => node.props.checked), [true, false]);
    assert.equal(JSON.stringify(patches), '[{"billionContextEnabled":true},{"extractionBillionContextEnabled":false}]');
    act(() => view.unmount());
    await act(async () => { view = create(createElement('div', {}, controls)); });
    assert.deepEqual(switches().map(node => node.props.checked), [true, false]);
  } finally { act(() => view.unmount()); }
});

test('translation sidebar opens its configuration from the footer and closes on Escape', async () => {
  const { load, listeners } = setup(async () => ({ billionContextEnabled: false }));
  const { AgentSessionSidebar } = load(new URL('../src/pages/agent/AgentSessionSidebar.tsx', import.meta.url));
  let view;
  await act(async () => { view = create(createElement(AgentSessionSidebar, {
    sessionsByProject: {}, projects: [], activeProject: '', activeSessionId: '', collapsed: {},
    disabled: false, activeRunning: false, unseenLights: {},
  })); });
  try {
    assert.equal(view.root.findAllByProps({ role: 'dialog' }).length, 0);
    const button = view.root.findByType('aside').findByProps({ className: 'agent-settings-button' });
    await act(async () => button.props.onClick());
    assert.equal(view.root.findAllByProps({ role: 'dialog' }).length, 1);
    assert.equal(input(view).props.checked, false);
    act(() => listeners.get('keydown')({ key: 'Escape' }));
    assert.equal(view.root.findAllByProps({ role: 'dialog' }).length, 0);
    assert.equal(listeners.size, 0);
  } finally { act(() => view.unmount()); }
});
