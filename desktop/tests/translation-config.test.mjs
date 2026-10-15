import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const requests = [];
  let value, renderer;
  const load = createTypeScriptLoader({}, { './api': {
    fetchProjectConfig(projectId, file) {
      return new Promise((resolve, reject) => requests.push({ projectId, file, resolve, reject }));
    },
  } });
  const { useProjectTranslationConfig } = load(new URL('../src/lib/useProjectTranslationConfig.ts', import.meta.url));
  function View(props) { value = useProjectTranslationConfig(props.id, props.file, props.active); return null; }
  let props = { id: 'a', file: 'config.yaml', active: true };
  act(() => { renderer = create(h(View, props)); });
  return {
    requests, value: () => value,
    update(changes) { props = { ...props, ...changes }; act(() => renderer.update(h(View, props))); },
    async reply(index, config) { await act(async () => requests[index].resolve({ config })); },
    async fail(index) { await act(async () => requests[index].reject(new Error('offline'))); },
    dispose() { act(() => renderer.unmount()); },
  };
}

test('runtime renders share one config request; returning to the page revalidates it', async () => {
  const f = fixture();
  const config = { common: { retranslKey: ['term'] }, backendSpecific: { backend: {} } };
  await f.reply(0, config);
  for (let i = 0; i < 20; i++) f.update({ runtimeRevision: i });
  assert.equal(f.requests.length, 1);
  assert.equal(f.value(), config);
  f.update({ active: false });
  f.update({ runtimeRevision: 30 });
  assert.equal(f.requests.length, 1);
  f.update({ active: true });
  assert.equal(f.requests.length, 2);
  const edited = { common: { retranslKey: ['new term'] } };
  await f.reply(1, edited);
  assert.equal(f.value(), edited);
  f.dispose();
});

test('late responses cannot cross project/config scopes or update a hidden page', async () => {
  const f = fixture();
  f.update({ id: 'b' });
  await f.reply(0, { from: 'a' });
  assert.equal(f.value(), null);
  await f.reply(1, { from: 'b' });
  assert.equal(f.value().from, 'b');
  f.update({ file: 'other.yaml' });
  assert.equal(f.value(), null);
  f.update({ active: false });
  await f.reply(2, { from: 'hidden' });
  assert.equal(f.value(), null);
  f.update({ active: true });
  await f.reply(3, { from: 'other.yaml' });
  f.update({ active: false });
  f.update({ active: true });
  await f.fail(4);
  assert.equal(f.value().from, 'other.yaml');
  f.update({ id: '' });
  assert.equal(f.value(), null);
  assert.equal(f.requests.length, 5);
  f.dispose();
});
