import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

// Test the public API helpers with the Tauri bridge and HTTP transport replaced.
// Transform only import.meta so each test can select Vite's runtime environment.
const source = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  transformers: {
    before: [(context) => {
      const visit = (node) => ts.isMetaProperty(node)
        ? ts.factory.createIdentifier('__importMeta')
        : ts.visitEachChild(node, visit, context);
      return (node) => ts.visitNode(node, visit);
    }],
  },
});

function loadApi({ native = true, dev = false, configured, invoke } = {}) {
  const requests = [];
  const exports = {};
  vm.runInNewContext(outputText, {
    exports,
    __importMeta: { env: { DEV: dev, VITE_BACKEND_URL: configured } },
    window: native ? { __TAURI_INTERNALS__: {} } : {},
    localStorage: { getItem: () => null },
    require: (name) => {
      assert.equal(name, '@tauri-apps/api/core');
      return { invoke: invoke ?? (() => Promise.resolve({ url: 'http://127.0.0.1:45678' })) };
    },
    URL, AbortController, TextDecoder, setTimeout, clearTimeout,
    fetch: async (url) => {
      requests.push(url);
      return url.includes('/api/agent/stream')
        ? new Response('event: agent\ndata: {"type":"close"}\n\n')
        : new Response(JSON.stringify({ jobs: [], version: 'test' }));
    },
  });
  return { api: exports, requests };
}

test('packaged startup cannot fall back to an unrelated backend on 12333', async () => {
  const { api, requests } = loadApi();
  assert.equal(api.getBackendBaseUrl(), '');
  await assert.rejects(api.fetchJobs(), /尚未就绪/);
  assert.equal(requests.length, 0);
});

test('HTTP, AI translation, and Agent streams use the assigned port', async () => {
  const { api, requests } = loadApi();
  await api.ensureDesktopBackendReady();
  await api.fetchJobs();
  assert.equal(api.getBackendBaseUrl(), 'http://127.0.0.1:45678');
  assert.match(api.getAiTranslateUrl('project'), /^http:\/\/127\.0\.0\.1:45678\//);
  await new Promise((resolve, reject) => {
    api.subscribeAgentStream('project', (event) => { if (event.type === 'close') resolve(); }, reject);
  });
  assert.equal(requests.length, 2);
  assert.ok(requests.every((url) => url.startsWith('http://127.0.0.1:45678/')));
});

test('reconnection updates the port and failure disables old addresses', async () => {
  let attempts = 0;
  const { api, requests } = loadApi({ invoke: async () => {
    attempts += 1;
    if (attempts === 2) throw new Error('startup failed');
    return { url: `http://127.0.0.1:${45000 + attempts}` };
  } });
  await api.ensureDesktopBackendReady();
  await api.fetchJobs();
  await assert.rejects(api.ensureDesktopBackendReady(), /startup failed/);
  assert.equal(api.getBackendBaseUrl(), '');
  await assert.rejects(api.fetchJobs(), /尚未就绪/);
  await api.ensureDesktopBackendReady();
  await api.fetchJobs();
  assert.equal(attempts, 3);
  assert.deepEqual(requests, ['http://127.0.0.1:45001/api/jobs', 'http://127.0.0.1:45003/api/jobs']);
});

test('concurrent startup callers share one native request', async () => {
  let calls = 0;
  let finish;
  const { api } = loadApi({ invoke: () => {
    calls += 1;
    return new Promise((resolve) => { finish = resolve; });
  } });
  const first = api.ensureDesktopBackendReady();
  const second = api.ensureDesktopBackendReady();
  assert.equal(calls, 1);
  finish({ url: 'http://127.0.0.1:45678' });
  await Promise.all([first, second]);
  assert.equal(api.getBackendBaseUrl(), 'http://127.0.0.1:45678');
});

test('browser development and explicit external URLs keep their configured backend', async () => {
  for (const options of [
    { native: false, expected: 'http://127.0.0.1:12333' },
    { native: true, configured: 'http://localhost:45679/', expected: 'http://localhost:45679' },
  ]) {
    const { api, requests } = loadApi({ ...options, invoke: () => { throw new Error('must not spawn'); } });
    assert.equal(await api.ensureDesktopBackendReady(), null);
    await api.fetchJobs();
    assert.deepEqual(requests, [`${options.expected}/api/jobs`]);
  }
});

test('Tauri development keeps the external backend on 12333', async () => {
  const { api, requests } = loadApi({ dev: true, invoke: async () => ({ url: 'http://127.0.0.1:12333' }) });
  assert.equal(api.getBackendBaseUrl(), 'http://127.0.0.1:12333');
  await api.ensureDesktopBackendReady();
  await api.fetchJobs();
  assert.deepEqual(requests, ['http://127.0.0.1:12333/api/jobs']);
});
