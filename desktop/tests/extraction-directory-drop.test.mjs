import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('new extraction accepts one dropped directory path and rejects multiple or unreadable drops', () => {
  let state, selected = '';
  const load = createTypeScriptLoader({}, {
    '@tauri-apps/api/core': { isTauri: () => false },
    '@tauri-apps/api/webviewWindow': {},
    '../../components/PageActivity': { usePageActive: () => true },
    './labels': { x: key => key },
  });
  const { useDirectoryDrop } = load(new URL('../src/pages/extraction/useDirectoryDrop.ts', import.meta.url));
  function Probe() {
    state = useDirectoryDrop(path => { selected = path; }, false);
    return null;
  }
  let renderer;
  act(() => { renderer = create(h(Probe)); });
  const drop = (files, text = '') => act(() => state.handlers.onDrop({
    preventDefault() {}, stopPropagation() {}, dataTransfer: {
      items: [], files, types: files.length ? ['Files'] : ['text/uri-list'], getData: type => type === 'text/uri-list' ? text : '',
    },
  }));
  drop([{ path: 'C:\\Games\\Example' }]);
  assert.equal(selected, 'C:\\Games\\Example');
  assert.equal(state.error, '');
  drop([{ path: 'C:\\Games\\One' }, { path: 'C:\\Games\\Two' }]);
  assert.equal(selected, 'C:\\Games\\Example');
  assert.equal(state.error, 'directoryDropError');
  drop([], 'file:///D:/Games/Dragged%20Game');
  assert.equal(selected, 'D:/Games/Dragged Game');
  assert.equal(state.error, '');
  drop([], 'relative/game');
  assert.equal(selected, 'D:/Games/Dragged Game');
  assert.equal(state.error, 'directoryDropError');
  act(() => renderer.unmount());
});

test('a folder dropped on the page is offered to the caller, leaving the composer and text drags alone', () => {
  let state;
  const selected = [], errors = [];
  const load = createTypeScriptLoader({}, {
    '@tauri-apps/api/core': { isTauri: () => false },
    '@tauri-apps/api/webviewWindow': {},
    '../../components/PageActivity': { usePageActive: () => true },
    './labels': { x: key => key },
  });
  const { useDirectoryDrop } = load(new URL('../src/pages/extraction/useDirectoryDrop.ts', import.meta.url));
  function Probe() {
    state = useDirectoryDrop(path => selected.push(path), false, { exclude: '.agent-composer', onError: message => errors.push(message) });
    return null;
  }
  let renderer;
  act(() => { renderer = create(h(Probe)); });
  const event = (files, { composer = false, types = ['Files'] } = {}) => {
    const calls = [];
    return { calls, preventDefault() { calls.push('prevent'); }, stopPropagation() { calls.push('stop'); },
      target: { closest: selector => composer && selector === '.agent-composer' ? {} : null },
      dataTransfer: { items: [], files, types, getData: () => '' } };
  };
  const over = event([]);
  act(() => state.handlers.onDragOver(over));
  assert.equal(state.dragging, true);
  assert.equal(over.dataTransfer.dropEffect, 'copy');
  act(() => state.handlers.onDrop(event([{ path: 'E:/Games/Tomefure' }])));
  assert.deepEqual(selected, ['E:/Games/Tomefure']);
  assert.equal(state.dragging, false);

  // The composer handles its own image drops: no highlight, no default prevented, nothing selected.
  const image = event([{ path: 'E:/shot.png' }], { composer: true });
  act(() => state.handlers.onDragOver(image));
  act(() => state.handlers.onDrop(image));
  assert.deepEqual(image.calls, []);
  assert.equal(state.dragging, false);

  const text = event([], { types: ['text/plain'] });
  act(() => state.handlers.onDragOver(text));
  assert.deepEqual(text.calls, []);
  assert.equal(state.dragging, false);

  act(() => state.handlers.onDrop(event([{ path: 'E:/One' }, { path: 'E:/Two' }])));
  assert.deepEqual(selected, ['E:/Games/Tomefure']);
  assert.deepEqual(errors, ['directoryDropError']);
  act(() => renderer.unmount());
});

test('the new extraction dialog starts from the dropped folder or the drop error', () => {
  const load = createTypeScriptLoader({}, {
    '@tauri-apps/api/core': { isTauri: () => false },
    '@tauri-apps/api/webviewWindow': {},
    '../../components/PageActivity': { usePageActive: () => true },
    './labels': { x: key => key },
  });
  const { useDirectoryDrop } = load(new URL('../src/pages/extraction/useDirectoryDrop.ts', import.meta.url));
  let state;
  function Probe() { state = useDirectoryDrop(() => {}, false, { initialError: 'directoryDropError' }); return null; }
  let renderer;
  act(() => { renderer = create(h(Probe)); });
  assert.equal(state.error, 'directoryDropError');
  act(() => state.handlers.onDrop({ preventDefault() {}, stopPropagation() {}, target: {},
    dataTransfer: { items: [], files: [{ path: 'C:/Games/Example' }], types: ['Files'], getData: () => '' } }));
  assert.equal(state.error, '');
  act(() => renderer.unmount());
});
