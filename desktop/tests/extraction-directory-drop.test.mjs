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
      items: [], files, getData: type => type === 'text/uri-list' ? text : '',
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
