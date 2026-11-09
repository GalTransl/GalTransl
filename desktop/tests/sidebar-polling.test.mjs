import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(hidden = false) {
  const listeners = new Map(), timers = new Map();
  let timerId = 0;
  const document = {
    hidden,
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const window = {
    ...document,
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
  };
  const requests = { agent: [], runtime: [], extraction: [] };
  const pending = kind => new Promise(resolve => { requests[kind].push(resolve); });
  const load = createTypeScriptLoader({ document, window, localStorage: { getItem: () => null } }, {
    '../i18n': { message: key => key, resolveMessage: key => key, t: key => key, useUiLanguage: () => 'en' },
    '../lib/api': {
      getBackendProfileNames: () => [], isProjectConfigDirty: () => false,
      encodeProjectDir: dir => dir, decodeProjectDir: dir => dir, setProjectConfigDirty: () => {},
      listAgentSessions: () => pending('agent'), fetchProjectRuntime: () => pending('runtime'),
    },
    './Icon': { Icon: () => null },
    './page-state/InlineFeedback': { InlineFeedback: () => null },
    '../assets/logo.png': { default: '' }, '../pages/agent/storage': { readHistory: () => [] },
    '../pages/extraction/api': { extractionApi: () => pending('extraction') },
    '@tauri-apps/api/core': {},
  });
  const { Sidebar } = load(new URL('../src/components/Sidebar.tsx', import.meta.url));
  const view = openProjects => h(MemoryRouter, null, h(Sidebar, {
    openProjects, wizardOpen: false, wizardProjectName: '',
    onCloseWizard: () => {}, onCloseProject: () => {}, onCloseOtherProjects: () => {}, onCloseAllProjects: () => {},
  }));
  let renderer;
  act(() => { renderer = create(view(['project'])); });
  return {
    timers, requests, renderer,
    visibility(value) { document.hidden = value; act(() => { for (const cb of listeners.get('visibilitychange') || []) cb(); }); },
    tick() { const callbacks = [...timers.values()]; timers.clear(); act(() => callbacks.forEach(cb => cb())); },
    complete: async () => act(async () => {
      for (const resolve of requests.agent.splice(0)) resolve([]);
      for (const resolve of requests.runtime.splice(0)) resolve({ job: null });
      for (const resolve of requests.extraction.splice(0)) resolve({ sessions: [], total: 0, next_offset: null });
    }),
    replace: projects => act(() => renderer.update(view(projects))),
    dispose: () => act(() => renderer.unmount()),
  };
}

test('visibility changes never overlap sidebar polls or multiply their timers', async () => {
  const f = fixture();
  const inFlight = count => ['agent', 'runtime', 'extraction'].forEach(kind => assert.equal(f.requests[kind].length, count, kind));
  inFlight(1);
  f.visibility(true);
  f.visibility(false);
  inFlight(1);
  await f.complete();
  assert.equal(f.timers.size, 3);
  f.visibility(true);
  f.tick();
  inFlight(0);
  assert.equal(f.timers.size, 3);
  f.visibility(false);
  inFlight(1);
  assert.equal(f.timers.size, 0);
  await f.complete();
  assert.equal(f.timers.size, 3);
  f.dispose();
  assert.equal(f.timers.size, 0);
});

test('sidebar skips initial requests while hidden and late responses cannot restart cleaned-up polls', async () => {
  const f = fixture(true);
  const inFlight = count => ['agent', 'runtime', 'extraction'].forEach(kind => assert.equal(f.requests[kind].length, count, kind));
  inFlight(0);
  f.visibility(false);
  inFlight(1);
  f.replace([]);
  await f.complete();
  assert.equal(f.timers.size, 3);
  f.dispose();
  assert.equal(f.timers.size, 0);
});
