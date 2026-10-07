import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';
const i18n = createTypeScriptLoader()(new URL('../src/i18n/core.ts', import.meta.url));

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

function loadApi({ native = true, dev = false, configured, invoke, storage } = {}) {
  const requests = [];
  const requestOptions = [];
  const exports = {};
  vm.runInNewContext(outputText, {
    exports,
    __importMeta: { env: { DEV: dev, VITE_BACKEND_URL: configured } },
    window: { ...(native ? { __TAURI_INTERNALS__: {} } : {}), dispatchEvent: () => true },
    localStorage: storage ?? { getItem: () => null },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    require: (name) => {
      if (name === '../i18n/core') return i18n;
      assert.equal(name, '@tauri-apps/api/core');
      return { invoke: invoke ?? (() => Promise.resolve({ url: 'http://127.0.0.1:45678' })) };
    },
    URL, AbortController, TextDecoder, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      requests.push(url);
      requestOptions.push(options);
      return url.includes('/api/agent/stream')
        ? new Response('event: agent\ndata: {"type":"close"}\n\n')
        : new Response(JSON.stringify({ jobs: [], version: 'test' }));
    },
  });
  return { api: exports, requests, requestOptions };
}

test('localized profile copies preserve model values and rename updates saved references', async () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const { api } = loadApi({ native: false, storage });
  const config = { 'OpenAI-Compatible': { tokens: [{ modelName: 'model-a', stream: false }] } };
  await api.createBackendProfile('original', config);
  api.setSelectedBackendProfile('project', 'original');
  const chineseCopy = await api.copyBackendProfile('original');
  assert.equal(chineseCopy.name, 'original-副本');
  await i18n.setUiLanguage('en');
  try {
    const englishCopy = await api.copyBackendProfile('original');
    assert.equal(englishCopy.name, 'original-copy');
    assert.equal((await api.copyBackendProfile('original')).name, 'original-copy-2');
    await api.renameBackendProfile('original', 'renamed');
    assert.equal(api.getDefaultBackendProfile(), 'renamed');
    assert.equal(api.getAgentDefaultBackendProfile(), 'renamed');
    assert.equal(api.getSelectedBackendProfile('project'), 'renamed');
    const profiles = JSON.parse(values.get('galtransl-backend-profiles'));
    assert.deepEqual(profiles.renamed, config);
    assert.deepEqual(profiles[chineseCopy.name], config);
    assert.deepEqual(profiles[englishCopy.name], config);
    await assert.rejects(api.renameBackendProfile('renamed', ''), /Configuration name cannot be empty/);
    await assert.rejects(api.renameBackendProfile('renamed', englishCopy.name), /already exists/);
    await assert.rejects(api.copyBackendProfile('missing'), /Backend configuration not found/);
  } finally {
    await i18n.setUiLanguage('zh-CN');
  }
});

function taskBackendApi() {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  return { ...loadApi({ native: false, storage }), storage };
}

test('four backend defaults persist independently and track profile rename/delete', async () => {
  const { api, storage } = taskBackendApi();
  const defaults = ['Default', 'AgentDefault', 'GenDicDefault', 'SubagentDefault'];
  await api.createBackendProfile('first', { 'OpenAI-Compatible': { tokens: [] } });
  for (const role of defaults) assert.equal(api[`get${role}BackendProfile`](), 'first');
  for (const role of defaults) {
    await api.createBackendProfile(role, { 'OpenAI-Compatible': { tokens: [{ modelName: role }] } });
    api[`set${role}BackendProfile`](role);
  }
  const reloaded = loadApi({ native: false, storage }).api;
  for (const role of defaults) assert.equal(reloaded[`get${role}BackendProfile`](), role);
  await api.renameBackendProfile('GenDicDefault', 'dictionary');
  assert.equal(api.getGenDicDefaultBackendProfile(), 'dictionary');
  assert.equal(api.getSubagentDefaultBackendProfile(), 'SubagentDefault');
  await api.deleteBackendProfile('SubagentDefault');
  assert.equal(api.getSubagentDefaultBackendProfile(), '');
  assert.equal(api.getAgentDefaultBackendProfile(), 'AgentDefault');
  await api.deleteBackendProfile('dictionary');
  assert.equal(api.getGenDicDefaultBackendProfile(), '');
  assert.equal(api.getDefaultBackendProfile(), 'Default');
});

test('GenDic submission and agent request contexts use the independent defaults', async () => {
  const { api, requestOptions } = taskBackendApi();
  for (const name of ['translator', 'main', 'dictionary', 'child']) {
    await api.createBackendProfile(name, { 'OpenAI-Compatible': { tokens: [{ modelName: name, token: `test-${name}` }] } });
  }
  api.setDefaultBackendProfile('translator');
  api.setAgentDefaultBackendProfile('main');
  api.setGenDicDefaultBackendProfile('dictionary');
  api.setSubagentDefaultBackendProfile('child');
  const model = (profile) => profile['OpenAI-Compatible'].tokens[0].modelName;
  const payload = { project_dir: 'project', config_file_name: 'config.yaml', ...api.getSelectedBackendProfileJobPayload('project') };
  await api.submitJob({ ...payload, translator: 'GenDic' });
  assert.equal(model(JSON.parse(requestOptions.at(-1).body).backend_profile_data), 'dictionary');
  await api.submitJob({ ...payload, translator: 'auto-translate' });
  assert.equal(model(JSON.parse(requestOptions.at(-1).body).backend_profile_data), 'translator');
  const context = api.getAgentTranslatorBackendContext('project');
  assert.equal(model(context.translator_profile_data), 'translator');
  assert.equal(model(context.gendic_profile_data), 'dictionary');
  assert.equal(model(context.subagent_profile_data), 'child');
  assert.equal(context.backend_profile_data, undefined); // Main agent still uses its session selector.
  const backendUsage = createTypeScriptLoader({}, { './api': api })(new URL('../src/lib/backendUsage.ts', import.meta.url));
  assert.equal(backendUsage.summarizeGenDicBackendUsage('project', null).model, 'dictionary');
  api.setGenDicDefaultBackendProfile('');
  api.setSubagentDefaultBackendProfile('');
  await api.submitJob({ ...payload, translator: 'GenDic' });
  assert.equal(model(JSON.parse(requestOptions.at(-1).body).backend_profile_data), 'translator');
  assert.equal(backendUsage.summarizeGenDicBackendUsage('project', null).model, 'translator');
  const cleared = api.getAgentTranslatorBackendContext('project');
  assert.equal(cleared.gendic_profile_name, '');
  assert.equal(cleared.subagent_profile_name, '');
  assert.equal(JSON.stringify(cleared.gendic_profile_data), '{}');
  assert.equal(JSON.stringify(cleared.subagent_profile_data), '{}');
  api.setGenDicDefaultBackendProfile('missing');
  await assert.rejects(api.submitJob({ ...payload, translator: 'GenDic' }));
  assert.throws(() => api.getAgentTranslatorBackendContext('project'));
});

test('plugin settings can save and re-extract within the same project', async () => {
  const { api, requests, requestOptions } = loadApi({ native: false });
  const config = { plugin: { filePlugin: 'file_msgtool_script', file_msgtool_script: { source_encoding: 'utf8' } } };
  await api.updateProjectConfig('project', { config, config_file_name: 'extract.yaml' });
  await api.reextractMsgtoolInput('project', 'extract.yaml');
  assert.equal(requestOptions[0].method, 'PUT');
  assert.deepEqual(JSON.parse(requestOptions[0].body), { config, config_file_name: 'extract.yaml' });
  assert.equal(new URL(requests[1]).pathname, '/api/projects/project/plugins/file_msgtool_script/reextract');
  assert.equal(requestOptions[1].method, 'POST');
  assert.deepEqual(JSON.parse(requestOptions[1].body), { config_file_name: 'extract.yaml' });
});

test('packaged startup cannot fall back to an unrelated backend on 12333', async () => {
  const { api, requests } = loadApi();
  assert.equal(api.getBackendBaseUrl(), '');
  await assert.rejects(api.fetchJobs(), /尚未就绪/);
  assert.equal(requests.length, 0);
});

test('Agent start sends the first user prompt without a goal field', async () => {
  const { api, requestOptions } = loadApi({ native: false });
  await api.startAgent({ project_dir: 'project', first_prompt: '仅检查原文' });
  const payload = JSON.parse(requestOptions[0].body);
  assert.equal(payload.first_prompt, '仅检查原文');
  assert.equal('goal' in payload, false);
});

test('input browsing and re-extraction use the selected configuration', async () => {
  const { api, requests } = loadApi({ native: false });
  await api.fetchProjectCache('project', '提取 & cp932.yaml');
  await api.fetchCacheFile('project', 'chapter-}scene.ks.json', '提取 & cp932.yaml', true);
  const listing = new URL(requests[0]);
  const reload = new URL(requests[1]);
  assert.equal(listing.searchParams.get('config'), '提取 & cp932.yaml');
  assert.equal(reload.searchParams.get('config'), '提取 & cp932.yaml');
  assert.equal(reload.searchParams.get('refresh_input'), '1');
  assert.equal(decodeURIComponent(reload.pathname), '/api/projects/project/cache/chapter-}scene.ks.json');
  await api.fetchCacheFile('project', 'scene.ks.json');
  assert.equal(new URL(requests[2]).searchParams.get('config'), 'config.yaml');
  assert.equal(new URL(requests[2]).searchParams.has('refresh_input'), false);
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
