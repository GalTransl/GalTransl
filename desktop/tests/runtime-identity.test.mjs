import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader({}, {
  '../i18n': { t: key => key, useUiLanguage: () => 'en' },
  '../i18n/core': { getUiLanguage: () => 'en' },
  '../components/Icon': {},
  '../lib/speaker': {},
  '../lib/useNameDict': {},
});
const { mergeRuntimeIdentity } = load(new URL('../src/pages/translateRuntimeShared.tsx', import.meta.url));
const snapshot = () => ({
  project_dir: 'project', job: null, summary: { total: 2 }, stage: '', current_file: '',
  files: [{ filename: 'a', translated: 1 }, { filename: 'b', translated: 0 }],
  recent_successes: [{ id: '1', speaker: ['a', 'b'] }, { id: '2', speaker: null }],
  recent_errors: [{ id: 'e1', message: 'one' }, { id: 'e2', message: 'two' }],
  retransl_stats: [{ key: 'a', count: 1 }, { key: 'b', count: 2 }],
});

test('unchanged runtime snapshots and rows reuse identities; changed rows stay authoritative', () => {
  const prev = snapshot();
  assert.equal(mergeRuntimeIdentity(prev, snapshot()), prev);
  const next = snapshot();
  next.files[1].translated = 1;
  const merged = mergeRuntimeIdentity(prev, next);
  assert.notEqual(merged, prev);
  assert.equal(merged.files[0], prev.files[0]);
  assert.equal(merged.files[1], next.files[1]);
  assert.equal(merged.recent_successes, prev.recent_successes);
});

for (const field of ['files', 'recent_successes', 'recent_errors', 'retransl_stats']) {
  test(`runtime preserves reordered ${field} while retaining unchanged row objects`, () => {
    const prev = snapshot();
    const next = snapshot();
    next[field].reverse();
    const merged = mergeRuntimeIdentity(prev, next);
    assert.notEqual(merged[field], prev[field]);
    assert.equal(merged[field][0], prev[field][1]);
    assert.equal(merged[field][1], prev[field][0]);
  });
}

test('runtime honors insertions, removals and changes outside row lists', () => {
  const prev = snapshot();
  const next = snapshot();
  next.recent_errors = [];
  next.files.push({ filename: 'c', translated: 0 });
  next.stage = 'complete';
  next.job = { job_id: 'new', status: 'completed' };
  const merged = mergeRuntimeIdentity(prev, next);
  assert.equal(merged.recent_errors.length, 0);
  assert.equal(merged.files.length, 3);
  assert.equal(merged.files[2], next.files[2]);
  assert.equal(merged.stage, 'complete');
  assert.equal(merged.job, next.job);
});
