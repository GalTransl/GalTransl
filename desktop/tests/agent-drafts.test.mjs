import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const source = new URL('../src/pages/agent/drafts.ts', import.meta.url);
function storage() {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}

test('drafts are isolated by project/session, persist across reloads, and clear after sending', () => {
  const localStorage = storage();
  const api = createTypeScriptLoader({ localStorage })(source);
  api.saveMessageDraft('a:b', 'c', '  多行\n草稿  ');
  api.saveMessageDraft('a', 'b:c', 'different');
  api.saveMessageDraft('a:b', '', 'new session');
  const fresh = createTypeScriptLoader({ localStorage })(source);
  assert.equal(fresh.loadMessageDraft('a:b', 'c'), '  多行\n草稿  ');
  assert.equal(fresh.loadMessageDraft('a', 'b:c'), 'different');
  assert.equal(fresh.loadMessageDraft('a:b', ''), 'new session');
  fresh.saveMessageDraft('a:b', 'c', '');
  assert.equal(createTypeScriptLoader({ localStorage })(source).loadMessageDraft('a:b', 'c'), '');
});

test('switching sessions restores drafts immediately and stale async writes cannot change the visible draft', () => {
  const api = createTypeScriptLoader({ localStorage: storage() })(source);
  function Editor({ project, session }) {
    const [value, onChange] = api.useMessageDraft(project, session);
    return h('input', { value, onChange });
  }
  let renderer;
  act(() => { renderer = create(h(Editor, { project: 'p', session: '1' })); });
  const setFirst = renderer.root.findByType('input').props.onChange;
  act(() => setFirst('first'));
  act(() => renderer.update(h(Editor, { project: 'p', session: '2' })));
  assert.equal(renderer.root.findByType('input').props.value, '');
  act(() => renderer.root.findByType('input').props.onChange('second'));
  act(() => setFirst('restored after failure'));
  assert.equal(renderer.root.findByType('input').props.value, 'second');
  act(() => renderer.update(h(Editor, { project: 'p', session: '1' })));
  assert.equal(renderer.root.findByType('input').props.value, 'restored after failure');
  act(() => renderer.unmount());
});

test('typing during session creation transfers to the created session and storage failures retain drafts', () => {
  const broken = { getItem() { throw Error(); }, setItem() { throw Error(); }, removeItem() { throw Error(); } };
  const api = createTypeScriptLoader({ localStorage: broken })(source);
  api.saveMessageDraft('project', '', 'next message');
  api.moveMessageDraft('project', '', 'created');
  assert.equal(api.loadMessageDraft('project', ''), '');
  assert.equal(api.loadMessageDraft('project', 'created'), 'next message');
});
