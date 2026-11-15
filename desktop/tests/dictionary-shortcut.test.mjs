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
    './Panel': { Panel: ({ children, actions }) => h('section', null, actions, children) },
    './page-state': { EmptyState: () => null, ErrorState: () => null, InlineFeedback: () => null, LoadingState: () => null },
    '../lib/backendUsage': { formatBackendUsage: () => '' },
    './api': { DISABLE_ANIMATIONS_CHANGE_EVENT: 'animations', getDisableAnimationsPreference: () => false },
    '@tauri-apps/api/core': { invoke: async () => {} },
  });
  return { load, key(overrides = {}) {
    let prevented = false;
    const event = { key: 's', ctrlKey: true, preventDefault() { prevented = true; }, ...overrides };
    for (const callback of listeners.get('keydown') || []) callback(event);
    return prevented;
  } };
}

test('mini window targets select the dictionary category, match the row key and scroll only the editor', () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const { PageActivityContext } = f.load(new URL('../src/components/PageActivity.tsx', import.meta.url));
  const data = { pre_dict_files: ['pre.txt'], post_dict_files: [], gpt_dict_files: ['gpt.txt'], dict_contents: {
    'gpt.txt': { lines: ['猫\tcat'] },
    'pre.txt': { lines: ['mono\t猫\tkitten', 'diag\t猫\tcat', '犬\tdog'] },
  } };
  const highlighted = [], focused = [];
  let renderer;
  // 单分组时竖向滚动条在表格（.dict-card__table）上，外层列表是 overflow: hidden、滚不动。
  const table = { scrollTop: 0, clientTop: 0, clientHeight: 100, scrollHeight: 1000, offsetHeight: 100,
    getBoundingClientRect: () => ({ top: 100, height: 100 }) };
  const pane = { scrollTop: 0, clientTop: 0, clientHeight: 100, scrollHeight: 100, offsetHeight: 100,
    getBoundingClientRect: () => ({ top: 100, height: 100 }), querySelector: selector => {
      const index = Number(selector.match(/"(\d+)"/)[1]);
      if (!renderer.root.findAllByProps({ 'data-dict-row-index': index }).length) return null;
      return { getBoundingClientRect: () => ({ top: 100 + index * 200 - table.scrollTop, bottom: 150 + index * 200 - table.scrollTop, height: 50 }),
        closest: selector => { assert.equal(selector, '.dict-card__table'); return table; },
        offsetWidth: 0,
        classList: { add: () => highlighted.push(index), remove() {} },
        querySelector: () => ({ focus: () => focused.push(index) }),
        scrollIntoView() { assert.fail('must not scroll the outer dialog'); },
      };
    } };
  const props = { title: 'Dictionary', description: '', data: null, loading: true, error: null,
    onReload: async () => {}, onCreateFile: async () => '', onDeleteFile: async () => {}, onSaveFile: async () => {} };
  const view = (focusTarget, active = true) => h(PageActivityContext.Provider, { value: active }, h(DictionaryManager, { ...props, focusTarget }));
  act(() => { renderer = create(view({ page: 'dictionary', fileKey: 'pre.txt', line: 'diag\t猫\told' }), {
    createNodeMock: node => String(node.props.className).startsWith('dict-card-list') ? pane : null,
  }); });
  try {
    props.data = data;
    props.loading = false;
    act(() => renderer.update(view({ page: 'dictionary', fileKey: 'pre.txt', line: 'diag\t猫\told' })));
    assert.deepEqual(highlighted, [1]);
    assert.deepEqual(focused, [1]);
    assert.equal(table.scrollTop, 175);
    assert.equal(pane.scrollTop, 0);
    assert.ok(renderer.root.findAllByType('input').some(node => node.props.value === 'dog'));
    act(() => renderer.update(view({ page: 'dictionary', fileKey: 'pre.txt', line: '犬\tdog' })));
    assert.deepEqual(highlighted, [1, 2]);
    assert.equal(table.scrollTop, 375);
    act(() => renderer.update(view({ page: 'dictionary', fileKey: 'pre.txt', line: '消失\tmissing' })));
    assert.ok(renderer.root.findAll(node => node.props.children === 'projects:projectPreview.entryMissing').length);
    assert.deepEqual(highlighted, [1, 2]);
  } finally { act(() => renderer.unmount()); }
});

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
  const dirtyDots = () => renderer.root.findAllByProps({ className: 'dict-file-item__dot' });
  assert.equal(dirtyDots().length, 0);
  const translationInput = () => renderer.root.findAllByType('input').find(node => ['cat', 'kitten', 'newer'].includes(node.props.value));
  act(() => translationInput().props.onChange({ target: { value: 'kitten' } }));
  assert.equal(dirtyDots().length, 1);
  act(() => renderer.update(view(false)));
  assert.equal(dirtyDots().length, 1);
  act(() => assert.equal(f.key(), false));
  assert.equal(saved.length, 0);
  act(() => renderer.update(view(true)));
  act(() => {
    assert.equal(f.key(), true);
    f.key();
    f.key({ repeat: true });
  });
  assert.deepEqual(saved, [['gpt.txt', '猫\tkitten\t']]);
  assert.equal(dirtyDots().length, 1); // A pending save has not committed the edits yet.
  act(() => translationInput().props.onChange({ target: { value: 'newer' } }));
  await act(async () => { finish(); });
  assert.equal(dirtyDots().length, 1); // Newer edits still need saving.
  act(() => assert.equal(f.key({ ctrlKey: false, metaKey: true }), true));
  assert.equal(saved.length, 2);
  assert.equal(saved[1][1], '猫\tnewer\t');
  await act(async () => { finish(); });
  assert.equal(dirtyDots().length, 0);
  act(() => f.key());
  assert.equal(saved.length, 2);
  act(() => renderer.unmount());
  assert.equal(f.key(), false);
});

test('adding an entry reveals and focuses the inserted row, including searches and section filters', () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const data = {
    pre_dict_files: [], post_dict_files: [], gpt_dict_files: ['gpt.txt'],
    dict_contents: { 'gpt.txt': { lines: ['---↓Animals↓---', '猫\tcat', '---↓Places↓---', '家\thouse'], mtime: 1 } },
  };
  const scrolled = [], focused = [];
  let renderer;
  act(() => {
    renderer = create(h(DictionaryManager, {
      title: 'Dictionary', description: '', data, loading: false, error: null,
      onReload: async () => {}, onCreateFile: async () => '', onDeleteFile: async () => {}, onSaveFile: async () => {},
    }), { createNodeMock(element) {
      if (element.props.className !== 'dict-card-list') return null;
      return { querySelector(selector) {
        const index = Number(selector.match(/"(\d+)"/)[1]);
        // The target must actually be rendered after filtering, not merely exist in the draft.
        const row = renderer.root.findByProps({ 'data-dict-row-index': index });
        assert.equal(row.findAllByType('input')[0].props.value, '');
        return {
          scrollIntoView: () => scrolled.push(index),
          querySelector: () => ({ focus: () => focused.push(index) }),
        };
      } };
    } });
  });
  const add = () => renderer.root.findAllByType('button')
    .find(button => button.props.children === 'projects:dictionaryManager.dictToolbar_message_entry');
  const search = () => renderer.root.findByProps({ className: 'dict-search' });
  // A normal toolbar addition appends to the bottom, even with a search active.
  act(() => search().props.onChange({ target: { value: 'cat' } }));
  act(() => add().props.onClick());
  assert.equal(search().props.value, '');
  assert.deepEqual(scrolled, [4]);
  assert.deepEqual(focused, [4]);
  // A section addition lands before the next section, rather than at the file's end.
  act(() => renderer.root.findAllByType('button').find(button => button.props.children?.[0] === 'Animals').props.onClick());
  act(() => add().props.onClick());
  assert.deepEqual(scrolled, [4, 2]);
  assert.deepEqual(focused, [4, 2]);
  // The small + at the end of a group follows the same behavior.
  act(() => renderer.root.findByProps({ className: 'dict-card__add-row-btn' }).props.onClick());
  assert.deepEqual(scrolled, [4, 2, 3]);
  assert.deepEqual(focused, [4, 2, 3]);
  act(() => renderer.unmount());
});

test('edits after switching from text to cards during save remain dirty and are saved next time', async () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const data = { pre_dict_files: [], post_dict_files: [], gpt_dict_files: ['gpt.txt'], dict_contents: { 'gpt.txt': { lines: ['猫\tcat'], mtime: 1 } } };
  const saved = [];
  let finish, renderer;
  act(() => {
    renderer = create(h(DictionaryManager, {
      title: 'Dictionary', description: '', data, loading: false, error: null,
      onReload: async () => {}, onCreateFile: async () => '', onDeleteFile: async () => {},
      onSaveFile: (file, text) => { saved.push(text); return new Promise(resolve => { finish = resolve; }); },
    }));
  });
  const toggle = () => renderer.root.findByProps({ className: 'dict-panel-actions' }).findAllByType('button')[0].props.onClick();
  act(toggle);
  act(() => renderer.root.findByType('textarea').props.onChange({ target: { value: '猫\tkitten' } }));
  act(() => f.key());
  assert.equal(saved[0], '猫\tkitten');
  act(toggle);
  act(() => renderer.root.findAllByType('input').find(node => node.props.value === 'kitten').props.onChange({ target: { value: 'newer' } }));
  await act(async () => finish());
  assert.equal(renderer.root.findAllByProps({ className: 'dict-file-item__dot' }).length, 1);
  act(() => f.key());
  assert.equal(saved[1], '猫\tnewer\t');
  await act(async () => finish());
  act(() => renderer.unmount());
});

test('one sidebar lists pre, GPT and post dictionaries as trees and new files go to the chosen tree', async () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const data = { pre_dict_files: ['pre.txt'], post_dict_files: ['post.txt'], gpt_dict_files: ['gpt.txt'], dict_contents: {
    'gpt.txt': { lines: ['猫	cat'], count: 1 },
    'pre.txt': { lines: ['a	b'], count: 1 },
    'post.txt': { lines: ['c	d'], count: 1 },
  } };
  const created = [];
  let renderer;
  act(() => {
    renderer = create(h(DictionaryManager, {
      title: 'Dictionary', description: '', data, loading: false, error: null,
      onReload: async () => {}, onDeleteFile: async () => {}, onSaveFile: async () => {},
      onCreateFile: async (category, name) => { created.push([category, name]); return name; },
    }));
  });
  const trees = () => renderer.root.findAllByProps({ className: 'dict-file-tree' });
  const fileNames = () => trees().map(tree => tree.findAllByProps({ className: 'dict-file-item__name' }).map(node => node.props.children));
  assert.deepEqual(trees().map(tree => tree.props['aria-label']), [
    'projects:dictionaryManager.dictTabs_message_dictionary',
    'projects:dictionaryManager.dictTabs_message_gPTDictionary',
    'projects:dictionaryManager.dictTabs_message_dictionaryVariant2',
  ]);
  assert.deepEqual(fileNames(), [['pre.txt'], ['gpt.txt'], ['post.txt']]);
  // The GPT dictionary is still opened by default.
  assert.ok(renderer.root.findAllByType('input').some(node => node.props.value === 'cat'));
  const radios = () => renderer.root.findAllByProps({ role: 'radio' });
  assert.deepEqual(radios().map(radio => radio.props['aria-checked']), [false, true, false]);
  // Selecting a post file opens it as a post dictionary and makes it the target for new files.
  act(() => trees()[2].findByProps({ className: 'dict-file-item ' }).props.onClick());
  assert.ok(renderer.root.findAllByType('input').some(node => node.props.value === 'd'));
  assert.ok(renderer.root.findAllByType('button').some(node => node.props.children === 'projects:dictionaryManager.dictToolbar_message_entryEntry'));
  const newName = renderer.root.findByProps({ placeholder: 'projects:dictionaryManager.dictCreateFile_placeholder_fileCustomPreTxt' });
  const createButton = () => renderer.root.findByProps({ className: 'dict-create-file' }).findByType('button');
  act(() => newName.props.onChange({ target: { value: 'extra' } }));
  await act(async () => createButton().props.onClick());
  act(() => radios()[0].props.onClick());
  act(() => newName.props.onChange({ target: { value: 'extra2' } }));
  await act(async () => createButton().props.onClick());
  assert.deepEqual(created, [['post', 'extra.txt'], ['pre', 'extra2.txt']]);
  // Collapsing a tree hides only its files.
  act(() => trees()[0].findByProps({ className: 'dict-file-tree__header' }).props.onClick());
  assert.deepEqual(fileNames(), [[], ['gpt.txt'], ['post.txt']]);
  act(() => renderer.unmount());
});

test('card edits keep dictionary row classification consistent with serialized text', () => {
  const f = fixture();
  const { DictionaryManager } = f.load(new URL('../src/components/DictionaryManager.tsx', import.meta.url));
  const data = { pre_dict_files: ['pre.txt'], post_dict_files: [], gpt_dict_files: [], dict_contents: { 'pre.txt': { lines: ['search\treplace\tnote'], mtime: 1 } } };
  let renderer;
  act(() => {
    renderer = create(h(DictionaryManager, {
      title: 'Dictionary', description: '', data, loading: false, error: null,
      onReload: async () => {}, onCreateFile: async () => '', onDeleteFile: async () => {}, onSaveFile: async () => {},
    }));
  });
  act(() => renderer.root.findAllByType('input').find(node => node.props.value === 'search').props.onChange({ target: { value: 'diag' } }));
  const heading = renderer.root.findByProps({ className: 'dict-card__pill dict-card__pill--situation' });
  assert.equal(heading.props.children, 'projects:dictionaryManager.getTypeLabel_message_textVariant4');
  act(() => renderer.unmount());
});
