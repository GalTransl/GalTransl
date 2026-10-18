import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { validateProjectName, PROJECT_NAME_MAX_LENGTH } = load(new URL('../src/lib/projectName.ts', import.meta.url));

test('plain project names pass', () => {
  for (const name of ['MyProject', '项目 1', 'a.b', '  padded  ', 'プロジェクト']) {
    assert.equal(validateProjectName(name), null, name);
  }
});

test('names that can escape the parent directory are rejected', () => {
  for (const name of ['.', '..', 'a/b', 'a\\b', '../x', '..\\x', 'C:\\x', 'a:b']) {
    assert.equal(validateProjectName(name), 'invalid', name);
  }
});

test('windows reserved device names and invalid characters are rejected', () => {
  for (const name of ['con', 'CON', 'nul', 'com1', 'LPT9', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b']) {
    assert.equal(validateProjectName(name), 'invalid', name);
  }
});

test('trailing dots (normalized away by windows) are rejected', () => {
  for (const name of ['foo.', 'foo..']) {
    assert.equal(validateProjectName(name), 'invalid', name);
  }
});

test('empty and over-long names are rejected', () => {
  assert.equal(validateProjectName(''), 'empty');
  assert.equal(validateProjectName('   '), 'empty');
  assert.equal(validateProjectName('a'.repeat(PROJECT_NAME_MAX_LENGTH)), null);
  assert.equal(validateProjectName('a'.repeat(PROJECT_NAME_MAX_LENGTH + 1)), 'invalid');
});
