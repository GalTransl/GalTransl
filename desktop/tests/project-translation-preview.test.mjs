import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useContext } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(continuous = false) {
  const directory = 'C:\\Games\\Preview';
  const timers = new Map(), intervals = new Map(), requests = [], stops = [], configs = [];
  const storage = new Map([['galtransl-disable-animations', 'true'],
    ['galtransl-continuous-retransl-by-project', JSON.stringify({ [directory]: continuous })]]);
  let nextTimer = 0;
  let runtime = {
    project_dir: directory,
    job: { job_id: 'job-1', status: 'running', translator: 'ForGal-json', started_at: '2026-11-01T00:00:00Z' },
    summary: { total: 100, translated: 8, percent: 8, translation_speed_lpm: 12, eta_seconds: 460 },
    files: [], recent_successes: [], recent_errors: [], retransl_stats: [{ key: 'term', count: 3 }],
  };
  const events = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
  const connection = { connectionPhase: 'online', translators: [{ name: 'ForGal-json', label: 'ForGal-json' }], loadJobs: async () => [] };
  const load = createTypeScriptLoader({ TextEncoder, btoa, document: { ...events, body: {}, documentElement: {} },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    window: { ...events,
      setInterval: (callback, delay) => { intervals.set(++nextTimer, { callback, delay }); return nextTimer; },
      clearInterval: id => intervals.delete(id),
      setTimeout: (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
      clearTimeout: id => timers.delete(id),
    },
  }, {
    'react-dom': { createPortal: children => children },
    '@tauri-apps/api/core': { invoke: async () => {} },
    '../features/connection/ConnectionContext': { useConnection: () => connection },
  });
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  Object.assign(api, {
    fetchProjectRuntime: async () => runtime,
    fetchNameDict: async () => ({ name_dict: {} }),
    fetchProjectConfig: async (id, file) => { configs.push({ id, file }); return { config: { common: { retranslKey: ['term'] } } }; },
    stopProjectTranslation: async id => { stops.push(id); runtime = { ...runtime, job: { ...runtime.job, status: 'cancelled' } }; return runtime.job; },
    submitJob: async payload => { requests.push(payload); return { ...runtime.job, project_dir: directory, config_file_name: 'custom.yml' }; },
  });
  const page = load(new URL('../src/pages/ProjectTranslatePage.tsx', import.meta.url));
  const activity = load(new URL('../src/components/PageActivity.tsx', import.meta.url));
  const preview = load(new URL('../src/pages/agent/ProjectTranslationPreview.tsx', import.meta.url));
  return { directory, api, page, preview, activity, timers, intervals, requests, stops, configs,
    complete() { runtime = { ...runtime, job: { ...runtime.job, status: 'completed' } }; },
  };
}

test('preview renders the real workbench, stops the matching project and pauses view polling when closed', async () => {
  const f = fixture();
  function Open() {
    const show = useContext(f.preview.ProjectTranslationPreviewContext);
    return h('open-preview', { show: () => show(f.directory, 'custom.yml') });
  }
  let root;
  await act(async () => { root = create(h(MemoryRouter, null, h(f.preview.ProjectTranslationPreviewProvider, null, h(Open)))); });
  try {
    assert.equal(f.configs.length, 0);
    await act(async () => root.root.findByType('open-preview').props.show());
    assert.equal(root.root.findAllByProps({ className: 'ptv2-page project-translate-page' }).length, 1);
    assert.equal(f.configs[0].id, f.api.encodeProjectDir(f.directory));
    assert.equal(f.configs[0].file, 'custom.yml');
    assert.equal(f.requests.length, 0);
    const stop = root.root.findAllByType('button').find(node => node.props.className?.includes('project-translate-page__stop-button'));
    assert.ok(stop);
    assert.equal(stop.props.disabled, false);
    await act(async () => stop.props.onClick());
    assert.deepEqual(f.stops, [f.api.encodeProjectDir(f.directory)]);
    const outside = {};
    act(() => root.root.findByProps({ className: 'agent-translation-preview-backdrop' }).props.onClick({ target: outside, currentTarget: outside }));
    assert.equal(f.intervals.size, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(root.root.findAllByProps({ className: 'ptv2-page project-translate-page' }).length, 1);
  } finally { act(() => root.unmount()); }
});

test('a workbench and its preview schedule only one automatic retranslating job for the same completion', async () => {
  const f = fixture(true);
  const ctx = { projectDir: f.directory, projectId: f.api.encodeProjectDir(f.directory), configFileName: 'custom.yml' };
  let root;
  const view = preview => h(MemoryRouter, null,
    h(f.page.ProjectTranslatePage, { key: 'workbench', ctx }), preview && h(f.page.ProjectTranslatePage, { key: 'preview', ctx }));
  await act(async () => { root = create(view(false)); });
  await act(async () => root.update(view(true)));
  try {
    assert.equal(f.requests.length, 0);
    f.complete();
    await act(async () => { for (const { callback, delay } of [...f.intervals.values()]) if (delay === 1000) callback(); });
    const followups = [...f.timers.values()].filter(timer => timer.delay === 450);
    assert.equal(followups.length, 2);
    await act(async () => { for (const timer of followups) timer.callback(); });
    assert.equal(f.requests.length, 1);
    assert.equal(f.requests[0].project_dir, f.directory);
    assert.equal(f.requests[0].config_file_name, 'custom.yml');
    assert.equal(f.requests[0].translator, 'ForGal-json');
  } finally { act(() => root.unmount()); }
  assert.equal(f.intervals.size, 0);
});
