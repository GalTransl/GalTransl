import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/lib/paths.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const paths = {};
vm.runInNewContext(outputText, { exports: paths, URL });

test('project and cache paths preserve Windows, UNC, and Linux roots', () => {
  for (const [base, expected] of [
    ['C:\\games', 'C:\\games\\gt_input'],
    ['C:/games/', 'C:/games/gt_input'],
    ['\\\\server\\share', '\\\\server\\share\\gt_input'],
    ['/home/user/games', '/home/user/games/gt_input'],
    ['/', '/gt_input'],
  ]) assert.equal(paths.joinPath(base, 'gt_input'), expected);
  assert.equal(paths.dirnamePath('C:\\config.yaml'), 'C:\\');
  assert.equal(paths.dirnamePath('/config.yaml'), '/');
  assert.equal(paths.dirnamePath('/home/user/config.yaml'), '/home/user');
  assert.equal(paths.basenamePath('/home/user/config.yaml'), 'config.yaml');
});

test('dragged file URIs keep absolute platform paths and decode Unicode', () => {
  for (const [uri, expected] of [
    ['file:///home/user/a%20b.txt', '/home/user/a b.txt'],
    ['file:///C:/games/%E6%B5%8B%E8%AF%95.txt', 'C:/games/测试.txt'],
    ['file://server/share/a.txt', '//server/share/a.txt'],
    ['file://localhost/home/user/a.txt', '/home/user/a.txt'],
  ]) {
    const path = paths.normalizeFileUriPath(uri);
    assert.equal(path, expected);
    assert.equal(paths.isAbsolutePath(path), true);
  }
  assert.equal(paths.isAbsolutePath('relative/file.txt'), false);
});
