import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(writeText) {
  const timers = new Map();
  let id = 0;
  const load = createTypeScriptLoader({
    navigator: { clipboard: { writeText } },
    window: {
      setTimeout: fn => { timers.set(++id, fn); return id; },
      clearTimeout: id => timers.delete(id),
    },
  });
  const { MessageCopyButton } = load(new URL('../src/pages/agent/rows/MessageCopyButton.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(createElement(MessageCopyButton, { text: '**原始回复**' })); });
  return { renderer, timers, MessageCopyButton,
    button: () => renderer.root.findByType('button'),
    status: () => renderer.root.findByProps({ role: 'status' }).children.join(''),
  };
}

test('copy preserves source formatting, announces success and releases its timer on unmount', async () => {
  const written = [];
  const f = fixture(async text => written.push(text));
  await act(async () => f.button().props.onClick());
  assert.deepEqual(written, ['**原始回复**']);
  assert.equal(f.status(), '已复制');
  act(() => { for (const fn of [...f.timers.values()]) fn(); });
  assert.equal(f.status(), '');
  await act(async () => f.button().props.onClick());
  act(() => f.renderer.unmount());
  assert.equal(f.timers.size, 0);
});

test('clipboard failures stay visible and the user can retry successfully', async () => {
  let attempt = 0;
  const f = fixture(async () => { if (++attempt === 1) throw new Error('denied'); });
  try {
    await act(async () => f.button().props.onClick());
    assert.match(f.status(), /复制失败/);
    assert.equal(f.button().props.disabled, false);
    await act(async () => f.button().props.onClick());
    assert.equal(f.status(), '已复制');
  } finally { act(() => f.renderer.unmount()); }
});

test('a pending clipboard result cannot mark a different response as copied', async () => {
  let resolve;
  const f = fixture(() => new Promise(done => { resolve = done; }));
  act(() => f.button().props.onClick());
  assert.equal(f.button().props.disabled, true);
  act(() => f.renderer.update(createElement(f.MessageCopyButton, { text: 'new response' })));
  await act(async () => resolve());
  assert.equal(f.status(), '');
  act(() => f.renderer.unmount());
});
