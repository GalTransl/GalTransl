import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('one jobs request updates connectivity and returns the UI snapshot, including reconnection', async () => {
  const jobs = [{ job_id: 'job', config_file_name: 'config.yaml' }];
  let calls = 0, fail = false, context, renders = 0;
  const load = createTypeScriptLoader({ document: {}, window: {} }, {
    '../../i18n': { message: key => ({ key }), t: key => key, useUiLanguage: () => 'en', useMessageState: useState },
    '../../lib/api': {
      getBackendBaseUrl: () => 'http://localhost', ensureDesktopBackendReady: async () => {},
      fetchJobs: async () => { calls++; if (fail) throw new Error('offline'); return jobs; },
      fetchTranslators: async () => [], fetchVersion: async () => '1', fetchVersionCheck: async () => null,
    },
    '../../lib/errors': { normalizeError: error => error.message },
    '@tauri-apps/api/window': {},
  });
  const { ConnectionProvider, useConnection } = load(new URL('../src/features/connection/ConnectionContext.tsx', import.meta.url));
  function Consumer() { context = useConnection(); renders++; return null; }
  let renderer;
  await act(async () => { renderer = create(h(ConnectionProvider, null, h(Consumer))); });
  let result;
  await act(async () => { result = await context.loadJobs(true); });
  assert.equal(result, jobs);
  assert.equal(calls, 1);
  assert.equal(context.connectionPhase, 'online');
  const stableRenders = renders;
  await act(async () => { result = await context.loadJobs(true); });
  assert.equal(result, jobs);
  assert.equal(calls, 2);
  assert.equal(renders, stableRenders);
  fail = true;
  await act(async () => { result = await context.loadJobs(true); });
  assert.equal(result, null);
  assert.equal(calls, 3);
  assert.equal(context.connectionPhase, 'offline');
  fail = false;
  await act(async () => { result = await context.loadJobs(true); });
  assert.equal(result, jobs);
  assert.equal(calls, 4);
  assert.equal(context.connectionPhase, 'online');
  act(() => renderer.unmount());
});
