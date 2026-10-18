import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function storage() {
  const values = new Map();
  const localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const api = createTypeScriptLoader({ localStorage })(new URL('../src/pages/agent/storage.ts', import.meta.url));
  return { api, localStorage };
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

test('the browser transcript cache keeps only a small replayable tail', () => {
  const { api, localStorage } = storage();
  const events = Array.from({ length: 200 }, (_, index) => ({ type: 'assistant_message', step: index, parts: [] }));
  api.saveSession({
    projectDir: 'project', sessionId: 'session', events, status: 'idle',
    first_prompt: '', startedAt: 1, finishedAt: 2,
  });
  const saved = JSON.parse(localStorage.getItem(api.sessionsKey('project', 'session')));
  assert.equal(saved.events.length, 80);
  assert.equal(saved.events[0].step, 120);
});

test('a quota error keeps the transcript available for the current app session', () => {
  const values = new Map();
  const localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: () => { throw new DOMException('Storage full', 'QuotaExceededError'); },
  };
  const api = createTypeScriptLoader({ localStorage })(new URL('../src/pages/agent/storage.ts', import.meta.url));
  api.saveSession({
    projectDir: 'project', sessionId: 'session',
    events: [{ type: 'user_message', step: 1, message: 'hi' }],
    status: 'idle', first_prompt: 'hi', startedAt: 1, finishedAt: 2,
  });
  assert.equal(api.loadSession('project', 'session').events[0].message, 'hi');
});
