import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function setup(fetchAppSettings, updateAppSettings = async patch => patch) {
  const connection = { backendUrl: 'http://localhost:8000' };
  const load = createTypeScriptLoader({}, {
    '../lib/api': { fetchAppSettings, updateAppSettings },
    '../features/connection/ConnectionContext': { useConnection: () => connection },
  });
  return { Component: load(new URL('../src/pages/SettingsPage.tsx', import.meta.url)).AgentContextPluginSection, connection };
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
