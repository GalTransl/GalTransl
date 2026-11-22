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

test('a finished file stops showing its held live phase once the hold expires, even without new props', async () => {
  // Regression: polls reuse unchanged file objects, so a memo'd row only re-rendered once after the
  // last request ended. That render fell inside the 2s hold and kept "translating"; a finished file's
  // props never change again, so the row stayed "translating" until the whole job ended.
  const { mock } = await import('node:test');
  const { createElement: h } = await import('react');
  const { act, create } = await import('react-test-renderer');
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
  try {
    const rowLoad = createTypeScriptLoader({}, {
      '../i18n': { t: key => key, useUiLanguage: () => 'en' },
      '../i18n/core': { getUiLanguage: () => 'en' },
      '../components/Icon': { Icon: () => null },
      '../lib/speaker': { speakerStyle: () => ({}) },
      '../lib/useNameDict': { resolveSpeakerName: name => name },
    });
    const { FileProgressRow } = rowLoad(new URL('../src/pages/translateRuntimeShared.tsx', import.meta.url));
    const writing = 'projects:translateRuntimeShared.writing_writing_translation';
    const props = { isRunning: true, isSuccessFileFilterActive: false, onToggleSuccessFileFilter() {} };
    const file = { filename: 'a.json', translated: 310, total: 310, failed: 0 };
    let renderer;
    const label = () => renderer.root.findByProps({ className: 'file-progress-row__state' }).children.filter(c => typeof c === 'string').join('');
    act(() => { renderer = create(h(FileProgressRow, { ...props, file: { ...file, activity: { phase: 'writing', cps: 30, requests: 1, stalled: false } } })); });
    assert.equal(label(), writing);
    // Next poll: the request has ended. Within the hold the row may still say "translating"...
    mock.timers.tick(1000);
    const finished = { ...file };
    act(() => { renderer.update(h(FileProgressRow, { ...props, file: finished })); });
    assert.equal(label(), writing);
    // ...but once the hold expires it must settle on "done" with no further prop changes.
    act(() => { mock.timers.tick(1100); });
    assert.equal(label(), 'projects:translateRuntimeShared.stateLabel_message_doneComplete');
    act(() => renderer.unmount());
  } finally {
    mock.timers.reset();
  }
});
