import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const { outputText } = ts.transpileModule(
  readFileSync(new URL('../src/pages/agent/storage.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } },
);

function storage() {
  const values = new Map();
  const exports = {};
  const localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  vm.runInNewContext(outputText, { exports, localStorage });
  return { api: exports, localStorage };
}

test('legacy browser sessions read goal and save only first_prompt', () => {
  const { api, localStorage } = storage();
  const key = api.sessionsKey('project', 'session');
  localStorage.setItem(key, JSON.stringify({
    projectDir: 'project', sessionId: 'session', goal: '旧会话首条输入',
    events: [{ type: 'user_message', step: 1, message: '旧会话首条输入' }],
  }));
  const session = api.loadSession('project', 'session');
  assert.equal(session.first_prompt, '旧会话首条输入');
  assert.equal('goal' in session, false);
  api.saveSession(session);
  const saved = JSON.parse(localStorage.getItem(key));
  assert.equal(saved.first_prompt, '旧会话首条输入');
  assert.equal('goal' in saved, false);
  assert.equal(saved.events[0].message, '旧会话首条输入');
});

test('explicit first_prompt takes precedence over the legacy field', () => {
  const { api, localStorage } = storage();
  for (const first_prompt of ['新首条输入', '']) {
    localStorage.setItem(api.sessionsKey('project', 'session'), JSON.stringify({
      events: [], first_prompt, goal: '旧输入',
    }));
    assert.equal(api.loadSession('project', 'session').first_prompt, first_prompt);
  }
});
