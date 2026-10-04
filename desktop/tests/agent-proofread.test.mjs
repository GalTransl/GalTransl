import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { buildTimeline } = load(new URL('../src/pages/agent/timeline.ts', import.meta.url));
const { SubagentList } = load(new URL('../src/pages/agent/rows/SubagentList.tsx', import.meta.url));

function events(terminal = true) {
  const rows = [
    { type: 'tool_call', step: 1, id: 'parent', name: 'run_subagents', arguments: { tasks: [{ agent: 'proofread', file: 'a.json' }] } },
    { type: 'subagent_start', step: 2, id: 'child', parent_id: 'parent', agent: 'proofread', label: '校对', file: 'a.json', started_at: 100 },
    { type: 'subagent_tool_call', step: 3, id: 'child', tool_call_id: 'edit', name: 'patch_transl_cache', arguments: { patches: [{ index: 1, dst: 'new', proofread_comment: 'uncertain' }, { index: 2, dst: 'rejected' }] } },
    { type: 'subagent_tool_result', step: 4, id: 'child', tool_call_id: 'edit', name: 'patch_transl_cache', ok: true, result: {
      updated: 1, changes: [{ path: 'a.json#1.pre_dst', before: 'old', after: 'new' }], files: [{ filename: 'a.json', updated: 1 }],
    } },
  ];
  if (terminal) rows.push({ type: 'subagent_done', step: 5, id: 'child', status: 'done', modified_count: 1, needs_review_count: 1, unverified_count: 0, failed_file_count: 0, proofread_comment: 1 });
  return rows;
}

function run(rows) {
  return buildTimeline(rows).find(group => group.type === 'activity').items.find(item => item.id === 'parent').subagents[0];
}

test('replayed proofreading events retain full actual changes and review counters', () => {
  const child = run(events());
  assert.equal(child.modifiedCount, 1);
  assert.equal(child.needsReviewCount, 1);
  assert.equal(child.steps[0].result.changes[0].before, 'old');
  const html = renderToStaticMarkup(createElement(SubagentList, { runs: [child] }));
  assert.match(html, /需二次审查 1 条/);
  assert.match(html, /实际修改 1 条/);
  assert.doesNotMatch(html, /实际修改 2 条/);
});

test('parent result reconciles stopped child with its committed edits', () => {
  const rows = events(false);
  rows.push({ type: 'tool_result', step: 5, id: 'parent', name: 'run_subagents', ok: true, result: {
    tasks: [{ id: 'child', status: 'stopped', modified_count: 1, needs_review_count: 2, unverified_count: 1, failed_file_count: 1 }],
  } });
  const child = run(rows);
  assert.equal(child.status, 'stopped');
  assert.equal(child.modifiedCount, 1);
  assert.equal(child.needsReviewCount, 2);
  assert.equal(child.unverifiedCount, 1);
});
