import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function setup(status = 'running', currentJobId = 'job-1') {
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  let snapshot = {
    job: { job_id: currentJobId, status, started_at: '2026-11-01T00:00:00Z', finished_at: '' },
    summary: { total: 100, translated: 8, percent: 8, translation_speed_lpm: 12, eta_seconds: 460 },
  };
  const boundary = {};
  const load = createTypeScriptLoader({ TextEncoder, btoa, window: {
    addEventListener() {},
    removeEventListener() {},
    setInterval: callback => { timers.set(++timerId, callback); return timerId; },
    clearInterval: id => timers.delete(id),
  } }, { '../../../lib/api': boundary });
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  Object.assign(boundary, api, {
    fetchProjectRuntime: async () => snapshot,
    fetchJobs: async () => [{ job_id: 'job-1', status }],
    stopProjectTranslation: projectId => {
      requests.push(projectId);
      return boundary.stopResponse;
    },
  });
  const { TranslationJobCard } = load(new URL('../src/pages/agent/rows/TranslationJobCard.tsx', import.meta.url));
  const item = { kind: 'tool', step: 1, ok: true, arguments: { translator: 'auto-translate', files: ['scene.json'] }, result: { job_id: 'job-1', status } };
  return { load, boundary, requests, timers, api,
    view: () => createElement(TranslationJobCard, { item, projectDir: 'C:\\Games\\Example' }),
    cancel: () => { snapshot = { ...snapshot, job: { ...snapshot.job, status: 'cancelled', finished_at: '2026-11-01T00:01:00Z' } }; },
  };
}

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
