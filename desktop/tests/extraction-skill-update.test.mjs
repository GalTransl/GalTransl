import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const installed = {
  ready: true, problems: [], python_version: 'Python 3.11', system_pythons: [],
  settings: { skill_dir: '/app/skills/current', python_path: '' },
  skill: { installed: true, current_commit: 'a'.repeat(40), latest_commit: '', update_available: false },
};

async function fixture(t) {
  const calls = [], requests = [], timers = new Map();
  const upstream = { latest: 'b'.repeat(40), failed: false, environment: installed };
  const server = createServer(async (request, response) => {
    const action = new URL(request.url, 'http://localhost').pathname.split('/').at(-1);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
    calls.push({ action, method: request.method, body });
    response.setHeader('Content-Type', 'application/json');
    if (action === 'environment') return response.end(JSON.stringify(upstream.environment));
    if (action === 'sessions') return response.end(JSON.stringify({ sessions: [], total: 0, next_offset: null }));
    assert.equal(action, 'skill-check');
    if (upstream.failed) {
      response.statusCode = 502;
      return response.end(JSON.stringify({ error: 'GitHub unavailable' }));
    }
    response.end(JSON.stringify({ ...upstream.environment.skill, latest_commit: upstream.latest,
      update_available: upstream.environment.skill.current_commit !== upstream.latest }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  let active = true, state, renderer, nextTimer = 0;
  const load = createTypeScriptLoader({
    URLSearchParams,
    fetch: (...args) => {
      const pending = fetch(...args).then(async response => new Response(await response.text(), { status: response.status }));
      requests.push(pending);
      return pending;
    },
    localStorage: { getItem: () => null, setItem() {} },
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    window: { innerWidth: 1366, matchMedia: () => ({ matches: true }) },
    document: { body: {}, documentElement: {}, addEventListener() {}, removeEventListener() {} },
  }, {
    '../../lib/api': { getBackendBaseUrl: () => `http://127.0.0.1:${server.address().port}` },
    '../../components/PageActivity': { usePageActive: () => active, useRetainPage() {} },
    '../styles/pages/agent.css': {}, '../styles/pages/extraction.css': {},
    'react-router-dom': { useNavigate: () => () => {} },
    '../lib/api': {}, './HomePage': {},
    './agent/drafts': { useMessageDraft: () => ['', () => {}] },
    './agent/images': { useImageInput: () => ({ images: [] }) },
    './extraction/useExtraction': { useExtraction: () => state },
    './extraction/useModelProfile': { useModelProfile: () => ({ models: [] }) },
    './extraction/Composer': { ExtractionComposer: () => null },
    './extraction/Dialogs': { EnvironmentDialog: props => h('div', { className: 'runtime-dialog', ...props }) },
  });
  const { useExtraction } = load(new URL('../src/pages/extraction/useExtraction.ts', import.meta.url));
  const { ExtractionPage } = load(new URL('../src/pages/ExtractionPage.tsx', import.meta.url));
  function Probe() { state = useExtraction(); return h(ExtractionPage); }
  const flush = async () => {
    while (requests.length) await act(async () => {
      await Promise.all(requests.splice(0));
      await new Promise(resolve => setImmediate(resolve));
    });
  };
  act(() => { renderer = create(h(Probe)); });
  t.after(() => act(() => renderer.unmount()));
  await flush();
  assert.equal(state.error, '');
  return {
    calls, upstream, renderer, state: () => state, flush,
    get timers() { return new Map([...timers].filter(([, timer]) => timer.delay === 60 * 60 * 1000)); },
    button: () => renderer.root.findByProps({ className: 'agent-settings-button' }),
    async tick() {
      const pending = [...timers.entries()].filter(([, timer]) => timer.delay === 60 * 60 * 1000);
      act(() => pending.forEach(([id, timer]) => { timers.delete(id); void timer.callback(); }));
      await flush();
    },
    async active(value) { act(() => { active = value; renderer.update(h(Probe)); }); await flush(); },
    async save(environment) { act(() => state.setEnvironment(environment)); await flush(); },
  };
}

test('automatic checks use the real HTTP client, show the sidebar notice and retain it through refresh and failures', async t => {
  const f = await fixture(t);
  assert.deepEqual(f.calls.map(call => call.action), ['sessions', 'environment', 'skill-check']);
  assert.equal(f.calls[2].method, 'POST');
  assert.equal(f.calls[2].body.skill_dir, installed.settings.skill_dir);
  assert.equal(f.button().findByType('span').children.join(''), 'Skill 有更新');
  assert.equal(f.button().findByType('i').props.className, '');
  assert.equal(f.state().environment.ready, true);
  assert.equal(f.state().error, '');
  assert.equal([...f.timers.values()].filter(timer => timer.delay === 60 * 60 * 1000).length, 1);
  await f.save({ ...f.state().environment, ready: false, problems: ['python_runner_missing'] });
  assert.equal(f.button().findByType('span').children.join(''), 'Skill 有更新');
  await f.save({ ...f.state().environment, ready: true, problems: [] });
  let refreshed;
  await act(async () => { refreshed = await f.state().refreshEnvironment(); });
  assert.equal(refreshed.skill.update_available, true);
  act(() => f.button().props.onClick());
  const dialog = f.renderer.root.findByProps({ className: 'runtime-dialog' });
  assert.equal(dialog.props.initial.skill.latest_commit, f.upstream.latest);
  assert.equal(f.calls.filter(call => call.action === 'skill-check').length, 1);
  f.upstream.failed = true;
  await f.tick();
  assert.equal(f.state().environment.skill.update_available, true);
  assert.equal(f.state().error, '');
  assert.equal(f.timers.size, 1);
  f.upstream.failed = false;
  f.upstream.latest = installed.skill.current_commit;
  await f.tick();
  assert.equal(f.button().findByType('span').children.join(''), '环境就绪');
  assert.equal(f.button().findByType('i').props.className, 'is-ready');
});

test('checks pause on hidden pages, restart for the selected Skill, and clear after installing the latest version', async t => {
  const f = await fixture(t);
  await f.active(false);
  assert.equal(f.timers.size, 0);
  const checks = f.calls.filter(call => call.action === 'skill-check').length;
  await f.tick();
  assert.equal(f.calls.filter(call => call.action === 'skill-check').length, checks);
  await f.active(true);
  assert.equal(f.calls.filter(call => call.action === 'skill-check').length, checks + 1);
  const updated = { ...installed, settings: { ...installed.settings, skill_dir: '/data/skills/latest' },
    skill: { ...installed.skill, current_commit: f.upstream.latest, latest_commit: f.upstream.latest } };
  f.upstream.environment = updated;
  await f.save(updated);
  assert.equal(f.calls.at(-1).body.skill_dir, updated.settings.skill_dir);
  assert.equal(f.button().findByType('span').children.join(''), '环境就绪');
  assert.equal(f.timers.size, 1);
  const missing = { ...installed, ready: false, skill: { ...installed.skill, installed: false, current_commit: '' } };
  f.upstream.environment = missing;
  await f.save(missing);
  assert.equal(f.timers.size, 0);
  assert.equal(f.button().findByType('span').children.join(''), '配置环境');
  const count = f.calls.length;
  await f.tick();
  assert.equal(f.calls.length, count);
});
