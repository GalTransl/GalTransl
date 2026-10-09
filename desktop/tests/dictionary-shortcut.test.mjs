import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const listeners = new Map();
  const document = {
    addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(callback); },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
  };
  const load = createTypeScriptLoader({ document, window: document }, {
    '../i18n': { t: key => key, message: key => key, useUiLanguage: () => 'en', useMessageState: useState, UiTrans: () => null },
    './Button': { Button: props => h('button', props) },
    './Icon': { Icon: () => null },
    './Panel': { Panel: ({ children }) => h('section', null, children) },
    './page-state': { EmptyState: () => null, ErrorState: () => null, InlineFeedback: () => null, LoadingState: () => null },
    '../lib/backendUsage': { formatBackendUsage: () => '' },
    '@tauri-apps/api/core': { invoke: async () => {} },
  });
  return { load, key(overrides = {}) {
    let prevented = false;
    const event = { key: 's', ctrlKey: true, preventDefault() { prevented = true; }, ...overrides };
    for (const callback of listeners.get('keydown') || []) callback(event);
    return prevented;
  } };
}

test('Ctrl+S/Cmd+S saves only the visible dictionary, prevents duplicate requests and preserves edits made during save', async () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const { PageActivityContext } = f.load(new URL('../src/components/PageActivity.tsx', import.meta.url));
  const saved = [];
  let finish;
  const data = { pre_dict_files: [], post_dict_files: [], gpt_dict_files: ['gpt.txt'], dict_contents: { 'gpt.txt': { lines: ['猫\tcat'], mtime: 1 } } };
  const props = {
    title: 'Dictionary', description: '', data, loading: false, error: null,
    onReload: async () => {}, onCreateFile: async () => '', onDeleteFile: async () => {},
    onSaveFile: (file, text) => { saved.push([file, text]); return new Promise(resolve => { finish = resolve; }); },
  };
  let renderer;
  const view = active => h(PageActivityContext.Provider, { value: active }, h(DictionaryManager, props));
  act(() => { renderer = create(view(true)); });
  const translationInput = () => renderer.root.findAllByType('input').find(node => ['cat', 'kitten', 'newer'].includes(node.props.value));
  act(() => translationInput().props.onChange({ target: { value: 'kitten' } }));
  act(() => renderer.update(view(false)));
  act(() => assert.equal(f.key(), false));
  assert.equal(saved.length, 0);
  act(() => renderer.update(view(true)));
  act(() => {
    assert.equal(f.key(), true);
    f.key();
    f.key({ repeat: true });
  });
  assert.deepEqual(saved, [['gpt.txt', '猫\tkitten\t']]);
  act(() => translationInput().props.onChange({ target: { value: 'newer' } }));
  await act(async () => { finish(); });
  act(() => assert.equal(f.key({ ctrlKey: false, metaKey: true }), true));
  assert.equal(saved.length, 2);
  assert.equal(saved[1][1], '猫\tnewer\t');
  await act(async () => { finish(); });
  act(() => f.key());
  assert.equal(saved.length, 2);
  act(() => renderer.unmount());
  assert.equal(f.key(), false);
});
