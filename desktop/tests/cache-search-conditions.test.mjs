import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('cache conditions edit fields and regex independently and can be removed', () => {
  const load = createTypeScriptLoader({}, {
    './CustomSelect': { CustomSelect: ({ children, ...props }) => h('select', props, children) },
  });
  const { CacheSearchConditionRow } = load(new URL('../src/components/CacheSearchConditionRow.tsx', import.meta.url));
  let conditions;
  function Harness() {
    const [rows, setRows] = useState([
      { field: 'post_src', query: '悶え', options: { re: false } },
      { field: 'pre_dst', query: '挣扎', options: { re: false } },
    ]);
    conditions = rows;
    return rows.map((condition, index) => h(CacheSearchConditionRow, {
      key: index, condition,
      onChange: (next) => setRows((current) => current.map((item, i) => i === index ? next : item)),
      onRemove: index ? () => setRows((current) => current.filter((_, i) => i !== index)) : undefined,
    }));
  }
  let view;
  act(() => { view = create(h(Harness)); });
  try {
    assert.deepEqual(view.root.findAllByType('select').map((node) => node.props.value), ['post_src', 'pre_dst']);
    act(() => view.root.findAllByType('input').filter((node) => node.props.type === 'text')[1].props.onChange({ target: { value: '^挣扎' } }));
    act(() => view.root.findAllByType('input').filter((node) => node.props.type === 'checkbox')[1].props.onChange({ target: { checked: true } }));
    assert.equal(conditions[0].query, '悶え');
    assert.equal(conditions[0].options.re, false);
    assert.equal(conditions[1].query, '^挣扎');
    assert.equal(conditions[1].options.re, true);
    act(() => view.root.findAllByType('select')[1].props.onChange({ target: { value: 'proofread_dst' } }));
    assert.equal(conditions[1].field, 'proofread_dst');
    act(() => view.root.findByType('button').props.onClick());
    assert.equal(conditions.length, 1);
    assert.equal(conditions[0].field, 'post_src');
  } finally { act(() => view.unmount()); }
});

test('cache search sends combined conditions and continuation offset to the backend', async () => {
  const requests = [];
  const load = createTypeScriptLoader({
    fetch: async (url, init) => {
      requests.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ results: [], total: 123, has_more: true, next_offset: 42 }) };
    },
  }, { '@tauri-apps/api/core': { invoke: async () => ({}) } });
  const { searchCache } = load(new URL('../src/lib/api.ts', import.meta.url));
  const conditions = [{ field: 'pre_dst', query: '挣扎', options: { re: false } }];
  const response = await searchCache('project', '悶え', 'post_src', { re: false }, 100, 'custom.yaml', true, conditions, 21);
  assert.ok(requests[0].url.endsWith('/api/projects/project/cache/search'));
  assert.deepEqual(requests[0].body.conditions, conditions);
  assert.equal(requests[0].body.query, '悶え');
  assert.equal(requests[0].body.field, 'post_src');
  assert.equal(requests[0].body.offset, 21);
  assert.equal(requests[0].body.max_results, 100);
  assert.equal(response.next_offset, 42);
});
