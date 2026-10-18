import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const PREFIX = 'galtransl-agent-session:';
function fixture({ blocked = false, removalBlocked = false, initial = {} } = {}) {
  const values = new Map(Object.entries(initial));
  const localStorage = {
    get length() { return values.size; },
    key: i => [...values.keys()][i] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem(key, value) {
      if (blocked) throw new DOMException('Storage full', 'QuotaExceededError');
      values.set(key, String(value));
    },
    removeItem(key) {
      if (removalBlocked) throw new DOMException('Storage blocked', 'SecurityError');
      values.delete(key);
    },
  };
  const load = createTypeScriptLoader({ localStorage });
  const api = load(new URL('../src/lib/projectStorage.ts', import.meta.url));
  return { api, values, localStorage, load };
}

test('a single oversized transcript cannot exceed the budget or evict unrelated caches', () => {
  const f = fixture({ initial: { [`${PREFIX}other:s`]: '{"events":[]}', 'settings': 'keep' } });
  assert.equal(f.api.writeProjectStorage(`${PREFIX}huge:s`, 'x'.repeat(f.api.AGENT_SESSION_CACHE_BUDGET + 1)), false);
  assert.equal(f.api.readProjectStorage(`${PREFIX}huge:s`), null);
  assert.equal(f.values.has(`${PREFIX}other:s`), true);
  assert.equal(f.values.get('settings'), 'keep');
});

test('in-memory transcript fallbacks obey the same aggregate budget', () => {
  const f = fixture({ blocked: true });
  for (let i = 0; i < 4; i++) {
    f.api.writeProjectStorage(`${PREFIX}p:${i}`, JSON.stringify({ startedAt: i + 1, pad: 'x'.repeat(800_000) }));
  }
  const remaining = Array.from({ length: 4 }, (_, i) => f.api.readProjectStorage(`${PREFIX}p:${i}`));
  assert.ok(remaining.reduce((sum, raw) => sum + (raw?.length ?? 0), 0) <= f.api.AGENT_SESSION_CACHE_BUDGET);
  assert.ok(remaining[3]);
});

test('removing a cached session hides stale disk data even if removal is blocked', () => {
  const key = `${PREFIX}p:s`;
  const f = fixture({ blocked: true, removalBlocked: true, initial: { [key]: 'old' } });
  f.api.writeProjectStorage(key, 'new');
  f.api.removeProjectStorage(key);
  assert.equal(f.api.readProjectStorage(key), null);
});

test('startup cleanup bounds old transcript caches before any new session is saved', () => {
  const f = fixture({ initial: {
    [`${PREFIX}p:old`]: JSON.stringify({ startedAt: 1, pad: 'x'.repeat(800_000) }),
    [`${PREFIX}p:new`]: JSON.stringify({ startedAt: 2, pad: 'x'.repeat(800_000) }),
    'galtransl-backend-profiles': '{"saved":{}}',
  } });
  f.api.initializeProjectStorage();
  assert.equal(f.api.readProjectStorage(`${PREFIX}p:old`), null);
  assert.ok(f.api.readProjectStorage(`${PREFIX}p:new`));
  assert.equal(f.values.get('galtransl-backend-profiles'), '{"saved":{}}');
});

test('saveSession skips an oversized tool result while leaving the live events intact', () => {
  const f = fixture();
  const agent = f.load(new URL('../src/pages/agent/storage.ts', import.meta.url));
  const event = { type: 'tool_result', step: 1, result: 'x'.repeat(f.api.AGENT_SESSION_CACHE_BUDGET + 1) };
  agent.saveSession({ projectDir: 'p', sessionId: 's', events: [event], status: 'idle', startedAt: 1, finishedAt: 2 });
  assert.equal(agent.loadSession('p', 's'), null);
  assert.ok(event.result.length > f.api.AGENT_SESSION_CACHE_BUDGET);
});
