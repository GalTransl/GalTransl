import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { toolMeta } = load(new URL('../src/pages/agent/toolMeta.ts', import.meta.url));

test('extraction intent takes precedence while historical calls keep their argument summary', () => {
  for (const [name, args, fallback] of [
    ['read', { path: 'script.txt' }, 'script.txt'],
    ['write', { path: 'script.txt', content: 'output' }, 'script.txt'],
    ['edit', { path: 'script.txt', edits: [] }, 'script.txt'],
    ['bash', { command: 'echo checked\necho more' }, 'echo checked'],
    ['run_python', { code: 'print("checked")\nprint("more")' }, 'print("checked")'],
  ]) {
    const meta = toolMeta(name);
    assert.equal(meta.summary({ ...args, reason: '  检查剧本\n文件格式  ' }), '检查剧本 文件格式');
    for (const reason of [undefined, '', '  ', 1]) {
      assert.equal(meta.summary({ ...args, reason }), fallback);
    }
  }
});

test('billion-context tool labels follow the selected UI language', async () => {
  const { setUiLanguage } = load(new URL('../src/i18n/core.ts', import.meta.url));
  const labels = [
    ['compress', '压缩上下文', 'Compress context'],
    ['decompress', '还原上下文', 'Restore context'],
    ['search_context', '搜索上下文', 'Search context'],
    ['acp_status', '查看上下文状态', 'View context status'],
  ];
  await setUiLanguage('en');
  for (const [name, , english] of labels) assert.equal(toolMeta(name).action, english);
  await setUiLanguage('zh-CN');
  for (const [name, chinese] of labels) {
    assert.equal(toolMeta(name).action, chinese);
    assert.match(toolMeta(name).running, /^正在/);
  }
  assert.equal(toolMeta('decompress').summary({ blockId: 'block-1' }), 'block-1');
  assert.equal(toolMeta('search_context').summary({ query: '剧本' }), '剧本');
});

test('merged tools select labels, icons and summaries by action', () => {
  for (const [name, args, operation] of [
    ['input', { action: 'read', filename: 'a.json', index: '1-10' }, 'read_input_file'],
    ['dictionary', { action: 'set', file_key: 'gpt.txt' }, 'save_dict'],
    ['translation', { action: 'start', translator: 'GenDic' }, 'start_translation'],
    ['translation', { action: 'wait', seconds: 30 }, 'wait'],
    ['problems', { action: 'set', target: 'white_list', entry: 'a.json:1' }, 'manage_problem_white_list'],
    ['subagents', { action: 'changes', task_id: 'task-1' }, 'read_proofread_changes'],
    ['edit_transl_cache', { action: 'delete', filename: 'a.json', indexes: '1-3' }, 'delete_transl_cache'],
  ]) {
    const meta = toolMeta(name, args);
    const previous = toolMeta(operation);
    assert.equal(meta.action, previous.action);
    assert.equal(meta.icon, previous.icon);
    assert.equal(meta.summary(args), previous.summary(args));
  }
});
const summary = (selection, result) => toolMeta('patch_transl_cache').summary({
  action: 'replace', old_text: '扭动挣扎', new_text: '扭动呻吟', ...selection,
}, result);

test('new write actions and historical arguments both display correctly', () => {
  const guideline = toolMeta('write_project_guideline');
  for (const action of ['append', 'overwrite', 'replace']) {
    assert.equal(guideline.summary({ action }), guideline.summary({ mode: action }));
  }
  assert.equal(toolMeta('patch_transl_cache').summary({
    action: 'replace', filename: 'a.json', query: 'old', replacement: 'new',
  }), summary({ files: 'a.json', old_text: 'old', new_text: 'new' }));
  assert.equal(summary({ files: 'a.json', old_text: 'old', new_text: '', query: 'legacy', replacement: 'legacy' }), '1 个文件 · old -> ');
});

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
