import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('file preview keeps its closing content and ignores responses after close or a newer file click', async () => {
  const pending = [];
  const load = createTypeScriptLoader({}, {
    './api': { extractionApi: (action, params) => action === 'files'
      ? Promise.resolve({ entries: ['first.txt', 'second.txt'].map(name => ({ name, is_dir: false, size: 12 })), total: 2 })
      : new Promise(resolve => pending.push({ params, resolve })) },
  });
  const { Workspace } = load(new URL('../src/pages/extraction/Workspace.tsx', import.meta.url));
  let renderer;
  await act(async () => { renderer = create(h(Workspace, {
    session: { id: 'fixture', game_dir: '/game', workspace: '/output' }, refresh: 0, running: false, busy: false, importProject() {},
  })); });
  const dialog = () => renderer.root.findByProps({ role: 'dialog' });
  const close = () => dialog().findByType('button');
  const file = name => renderer.root.findAllByType('button').find(node => node.props.title === name);
  const clickFile = name => file(name).props.onClick();
  const reply = (index, content) => pending[index].resolve({ path: pending[index].params.path, content, next_offset: null });

  assert.equal(dialog().props['aria-hidden'], true);
  assert.equal(dialog().props.inert, '');
  act(() => clickFile('first.txt'));
  assert.equal(dialog().props['aria-hidden'], false);
  assert.equal(dialog().findByType('pre').props['aria-busy'], true);
  act(() => close().props.onClick());
  await act(async () => reply(0, 'late first response'));
  assert.equal(dialog().props['aria-hidden'], true);
  assert.equal(dialog().props.inert, '');

  act(() => clickFile('first.txt'));
  act(() => clickFile('second.txt'));
  await act(async () => reply(2, 'second file contents'));
  await act(async () => reply(1, 'stale first response'));
  assert.equal(dialog().findByType('strong').children.join(''), 'second.txt');
  assert.equal(dialog().findByType('pre').children.join(''), 'second file contents');
  assert.equal(file('second.txt').props.className, 'extract-file is-active');
  act(() => close().props.onClick());
  assert.equal(dialog().props['aria-hidden'], true);
  // Content stays available to render throughout the CSS exit transition.
  assert.equal(dialog().findByType('pre').children.join(''), 'second file contents');
  assert.equal(file('second.txt').props.className, 'extract-file');
  act(() => renderer.unmount());
});
