import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { toolMeta } = load(new URL('../src/pages/agent/toolMeta.ts', import.meta.url));
const summary = (selection, result) => toolMeta('patch_transl_cache').summary({
  action: 'replace', query: '扭动挣扎', replacement: '扭动呻吟', ...selection,
}, result);

test('replace summaries accept string, array and filename scopes without inventing zero files', () => {
  for (const selection of [{ files: 'a.json' }, { filename: 'a.json' }, { files: ['a.json', 'a.json'] }]) {
    assert.equal(summary(selection), '1 个文件 · 扭动挣扎 -> 扭动呻吟');
  }
  assert.match(summary({ files: ['a.json', 'b.json'] }), /^2 个文件/);
  assert.equal(summary({}), '扭动挣扎 -> 扭动呻吟');
});

test('wildcard scopes remain selectors until execution resolves actual changed files', () => {
  for (const files of ['*', '*.json', ['a.json', '[bc].json'], 'chapter-?.json']) {
    const pending = summary({ files });
    assert.doesNotMatch(pending, /\d+ 个文件/);
    assert.ok(pending.includes(Array.isArray(files) ? files.join('、') : files));
  }
  const result = { updated: 5, files: [
    { filename: 'a.json', updated: 3 }, { filename: 'b.json', updated: 2 },
    { filename: 'no-match.json', updated: 0 }, { filename: 'failed.json', updated: 0, error: 'failed' },
  ] };
  assert.equal(summary({ files: '*' }, result), '2 个文件 · 扭动挣扎 -> 扭动呻吟');
  assert.match(summary({ files: '*' }, { files: [{ filename: 'a.json', updated: 0 }] }), /^0 个文件/);
});
