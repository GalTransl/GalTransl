import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture() {
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  const api = {
    fetchCacheDictionaryMatches(project, config, entries, options) {
      return new Promise((resolve, reject) => calls.push({ project, config, entries, options, resolve, reject }));
    },
  };
  const load = createTypeScriptLoader({
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, {
    './api': api, '../lib/api': api,
    '../i18n': { t: key => key, useUiLanguage: () => 'zh-CN' },
  });
  let state;
  const { useCacheDictionaryMatches } = load(new URL('../src/lib/useCacheDictionaryMatches.ts', import.meta.url));
  function Harness({ project = 'a', config = 'config.yaml', file = 'scene.json', entries, active = true, revision = 0 }) {
    state = useCacheDictionaryMatches(project, config, file, entries, active, revision);
    return null;
  }
  return {
    calls, load, Harness, get state() { return state; },
    async flush() {
      await act(async () => {
        const callbacks = [...timers.values()]; timers.clear();
        // Async timers can wait on deferred requests. Start, do not await them.
        for (const callback of callbacks) void callback();
      });
    },
  };
}
const entry = (index, overrides = {}) => ({ index, name: '猫', pre_src: `原文${index}`, post_src: `原文${index}`, pre_dst: '译文', trans_by: 'model', ...overrides });
const counts = { pre: 1, post: 2, gpt: 3 };
function finish(call, value = counts) {
  call.resolve({ results: call.entries.map(() => ({ counts: value })) });
}

test('counts are batched with dialogue context; results stay outside cache entries', async () => {
  const f = fixture();
  const entries = Array.from({ length: 105 }, (_, i) => entry(i));
  const before = JSON.stringify(entries);
  let renderer;
  act(() => { renderer = create(h(f.Harness, { entries })); });
  await f.flush();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].entries.length, 100);
  assert.equal(f.calls[0].entries[0].context_after.length, 2);
  assert.equal(f.calls[0].entries[99].context_after[0].index, 100);
  assert.equal(f.calls[0].entries[0].trans_by, undefined);
  await act(async () => finish(f.calls[0]));
  assert.equal(f.calls[1].entries.length, 5);
  assert.equal(f.calls[1].entries[0].context_before[0].index, 98);
  await act(async () => finish(f.calls[1]));
  assert.equal(f.state.counts.size, 105);
  assert.equal(JSON.stringify(entries), before);
  act(() => renderer.unmount());
});

test('unsaved edits recompute affected sentences, cancel stale requests and refresh on activation/config changes', async () => {
  const f = fixture();
  const entries = Array.from({ length: 8 }, (_, i) => entry(i));
  let renderer;
  const view = (data, extra = {}) => h(f.Harness, { entries: data, ...extra });
  act(() => { renderer = create(view(entries)); });
  await f.flush();
  await act(async () => finish(f.calls[0]));
  const edited = entries.map((value, i) => i === 4 ? { ...value, pre_dst: '改后的译文' } : value);
  act(() => renderer.update(view(edited)));
  // The edited row and its four neighbours cannot display old counts.
  assert.equal(f.state.counts.size, 3);
  await f.flush();
  assert.equal(f.calls[1].entries.length, 5);
  assert.equal(f.calls[1].entries[2].pre_dst, '改后的译文');
  act(() => renderer.update(view(edited, { project: 'b', config: 'alternate.yaml' })));
  assert.equal(f.calls[1].options.signal.aborted, true);
  await act(async () => finish(f.calls[1], { pre: 99, post: 99, gpt: 99 }));
  assert.equal(f.state.counts.size, 0);
  await f.flush();
  assert.equal(f.calls[2].project, 'b');
  assert.equal(f.calls[2].config, 'alternate.yaml');
  await act(async () => finish(f.calls[2]));
  act(() => renderer.update(view(edited, { project: 'b', config: 'alternate.yaml', active: false })));
  await f.flush();
  assert.equal(f.calls.length, 3);
  act(() => renderer.update(view(edited, { project: 'b', config: 'alternate.yaml', active: true })));
  await f.flush();
  assert.equal(f.calls.length, 4);
  await act(async () => finish(f.calls[3]));
  act(() => renderer.update(view(edited, { project: 'b', config: 'alternate.yaml', revision: 1 })));
  await f.flush();
  assert.equal(f.calls.length, 5);
  act(() => renderer.unmount());
});

test('dictionary errors are visible and never become zero counts', async () => {
  const f = fixture();
  let renderer;
  act(() => { renderer = create(h(f.Harness, { entries: [entry(1)] })); });
  await f.flush();
  await act(async () => f.calls[0].reject(new Error('invalid dictionary')));
  assert.equal(f.state.error, 'invalid dictionary');
  assert.equal(f.state.counts.size, 0);
  act(() => renderer.unmount());
});

test('details show file/line and full rule, page through matches, and retry errors', async () => {
  const f = fixture();
  const { CacheDictionaryDetails } = f.load(new URL('../src/components/CacheDictionaryDetails.tsx', import.meta.url));
  let renderer;
  const sentence = { ...entry(1), context_before: [entry(0)] };
  act(() => { renderer = create(h(CacheDictionaryDetails, { projectId: 'a', configFileName: 'custom.yaml', entry: sentence, id: 'matches' })); });
  assert.equal(f.calls[0].options.details, true);
  assert.equal(f.calls[0].entries[0].context_before[0].index, 0);
  const match = { kind: 'post', file: 'E:\\project\\post.txt', line: 13, rule: 'post_src\t犬\tぉ\t', search: 'ぉ', replacement: '', note: '' };
  await act(async () => f.calls[0].resolve({ results: [{ counts, entries: [match], total: 101, offset: 0 }] }));
  const row = renderer.root.findByType('tbody').findByType('tr');
  assert.equal(row.props.title, match.rule);
  assert.deepEqual(row.findAllByType('td').map(cell => cell.children.filter(child => typeof child === 'string').join('')), ['13', 'post_src', '犬', 'ぉ', '', '']);
  assert.equal(renderer.root.findByProps({ title: match.file }).children.join(''), 'post.txt');
  const button = label => renderer.root.findAllByType('button').find(node => node.children.includes(`projects:cacheDictionary.${label}`));
  assert.equal(button('previous').props.disabled, true);
  act(() => button('next').props.onClick());
  assert.equal(f.calls[1].options.offset, 100);
  await act(async () => f.calls[1].reject(new Error('temporary error')));
  assert.ok(renderer.root.findByProps({ role: 'alert' }));
  act(() => button('retry').props.onClick());
  await act(async () => f.calls[2].resolve({ results: [{ counts, entries: [match], total: 101, offset: 100 }] }));
  assert.equal(button('next').props.disabled, true);
  assert.equal(button('previous').props.disabled, false);
  act(() => renderer.unmount());
});

test('the pill omits zero categories, hides when all counts are zero, and retains its disclosure button', () => {
  const load = createTypeScriptLoader({}, {
    '../i18n': { useUiLanguage: () => 'zh-CN', t: (key, values) => {
      const kind = key.split('.').pop();
      return ({ preCount: '译前', postCount: '译后', gptCount: 'GPT' })[kind]
        ? `${({ preCount: '译前', postCount: '译后', gptCount: 'GPT' })[kind]} ${values.count}` : key;
    } },
  });
  const { CacheDictionaryPill } = load(new URL('../src/components/CacheDictionaryPill.tsx', import.meta.url));
  let renderer;
  let toggles = 0;
  const view = counts => h(CacheDictionaryPill, { counts, error: '', expanded: false, controls: 'details', onToggle: () => { toggles += 1; } });
  act(() => { renderer = create(view({ pre: 0, post: 2, gpt: 3 })); });
  assert.equal(renderer.root.findByType('button').children.join(''), '译后 2 · GPT 3');
  act(() => renderer.root.findByType('button').props.onClick());
  assert.equal(toggles, 1);
  assert.equal(renderer.root.findByType('button').props['aria-controls'], 'details');
  act(() => renderer.update(view({ pre: 1, post: 0, gpt: 0 })));
  assert.equal(renderer.root.findByType('button').children.join(''), '译前 1');
  act(() => renderer.update(view({ pre: 0, post: 0, gpt: 0 })));
  assert.equal(renderer.toJSON(), null);
  act(() => renderer.unmount());
});

test('expanded details hide every zero-count category', async () => {
  const f = fixture();
  const { CacheDictionaryDetails } = f.load(new URL('../src/components/CacheDictionaryDetails.tsx', import.meta.url));
  let renderer;
  act(() => { renderer = create(h(CacheDictionaryDetails, { projectId: 'a', configFileName: 'config.yaml', entry: entry(1), id: 'matches' })); });
  await act(async () => f.calls[0].resolve({ results: [{
    counts: { pre: 0, post: 1, gpt: 0 }, total: 1, offset: 0,
    entries: [{ kind: 'post', file: 'post.txt', line: 1, rule: 'っ\t', search: 'っ', replacement: '', note: '' }],
  }] }));
  const headings = renderer.root.findAllByType('strong').map(node => node.children.join(''));
  assert.deepEqual(headings, ['projects:cacheDictionary.post · 1']);
  assert.equal(renderer.root.findAllByType('table').length, 1);
  act(() => renderer.unmount());
});

test('mini dictionary tables align each rule type, preserve empty fields and group file provenance', () => {
  const f = fixture();
  const { CacheDictionaryTable } = f.load(new URL('../src/components/CacheDictionaryTable.tsx', import.meta.url));
  const match = (file, line, rule, extra = {}) => ({ kind: 'post', file, line, rule, search: '', replacement: '', note: '', ...extra });
  const matches = [
    match('post.txt', 1, 'ぉ\t\t'),
    match('post.txt', 2, '^^っ\t！\t保留前缀'),
    match('post.txt', 3, 'post_src\t犬[and]!猫\t1^哦\t喔\t条件备注'),
    match('post.txt', 4, 'diag\t呀\t啊'),
    match('other.txt', 5, '猫    犬'),
    match('gpt.txt', 6, '猫\t猫咪\t动物\t女性', { kind: 'gpt' }),
    match('gpt.txt', 7, '犬->狗#动物', { kind: 'gpt' }),
  ];
  let renderer;
  act(() => { renderer = create(h(CacheDictionaryTable, { matches })); });
  const tables = renderer.root.findAllByType('table');
  assert.equal(tables.length, 5);
  const cells = row => row.findAllByType('td').map(cell => cell.children.filter(child => typeof child === 'string').join(''));
  const rows = table => table.findByType('tbody').findAllByType('tr');
  assert.deepEqual(cells(rows(tables[0])[0]), ['1', 'ぉ', '', '']);
  assert.deepEqual(cells(rows(tables[0])[1]), ['2', '^^っ', '！', '保留前缀']);
  assert.equal(rows(tables[0])[0].findAllByProps({ title: 'projects:cacheDictionary.emptyCell' }).length, 2);
  assert.deepEqual(cells(rows(tables[1])[0]), ['3', 'post_src', '犬[and]!猫', '1^哦', '喔', '条件备注']);
  assert.equal(tables[1].findAllByType('th').length, 6);
  assert.deepEqual(cells(rows(tables[2])[0]), ['4', 'diag', '呀', '啊']);
  assert.deepEqual(cells(rows(tables[3])[0]), ['5', '猫', '犬', '']);
  assert.deepEqual(cells(rows(tables[4])[0]), ['6', '猫', '猫咪', '动物\t女性']);
  assert.deepEqual(cells(rows(tables[4])[1]), ['7', '犬', '狗', '动物']);
  act(() => renderer.unmount());
});
