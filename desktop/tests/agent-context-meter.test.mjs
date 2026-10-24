import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const load = createTypeScriptLoader();
  const { ContextMeter } = load(new URL('../src/pages/agent/composer.tsx', import.meta.url));
  let renderer;
  const update = (cache) => act(() => {
    const element = createElement(ContextMeter, { usage: { used_tokens: 32000, window_tokens: 128000, cache } });
    if (renderer) renderer.update(element);
    else renderer = create(element);
  });
  return { update, meter: () => renderer.root.findByProps({ role: 'img' }),
    root: () => renderer.root, dispose: () => act(() => renderer.unmount()) };
}

test('cache stats appear only in the existing context meter tooltip and update for each session', () => {
  const f = fixture();
  f.update({ request_count: 2, input_tokens: 1000, cached_tokens: 800, hit_rate: 0.8, latest_hit_rate: 0.5 });
  let title = f.meter().props.title;
  assert.match(title, /25\.0%/);
  assert.match(title, /平均缓存命中率 80\.0%/);
  assert.match(title, /50\.0%/);
  assert.match(title, /统计 2 次请求/);
  assert.equal(f.meter().props['aria-label'], title);
  assert.equal(f.root().findAllByType('span').length, 1);
  f.update(null);
  title = f.meter().props.title;
  assert.match(title, /缓存命中率未知/);
  assert.doesNotMatch(title, /80\.0%/);
  f.dispose();
});

test('unknown cache data is distinct from a measured zero hit rate', () => {
  const f = fixture();
  f.update({ request_count: 1, input_tokens: 1000, cached_tokens: 0, hit_rate: 0, latest_hit_rate: null });
  assert.match(f.meter().props.title, /平均缓存命中率 0\.0%/);
  assert.match(f.meter().props.title, /最近一次.*—/);
  f.dispose();
});
