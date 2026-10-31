import type { AgentContextUsage } from '../../lib/api';
import type { ActivityItem, TimelineGroup } from '../agent/timeline';
import { shareTimelineGroups } from '../agent/timelineCache';
import type { Row } from './transcript';
import type { AgentEvent, Snapshot } from './types';

// Adapt the extraction protocol to the same presentation used by the translation agent.
export type ExtractionGroup = TimelineGroup | { type: 'notice'; id: string; text: string };
// Shared with live motion so selectors match replay-stable timeline anchors.
export const extractionStep = (id: string) => [...id].reduce((hash, char) => (Math.imul(hash, 31) + char.charCodeAt(0)) | 0, 0) >>> 0;
export function createExtractionTimelineBuilder() {
  let previous: ExtractionGroup[] = [];
  return (rows: Row[], running: boolean, pending: AgentEvent | null, prefix?: Snapshot['activity_prefix']) => {
    previous = shareTimelineGroups(previous, extractionTimeline(rows, running, pending, prefix));
    return previous;
  };
}

export function extractionTimeline(rows: Row[], running: boolean, pending: AgentEvent | null, prefix?: Snapshot['activity_prefix']): ExtractionGroup[] {
  const groups: ExtractionGroup[] = [];
  let anchor = prefix?.anchor ?? 'history';
  let current: Extract<TimelineGroup, { type: 'activity' }> | undefined;
  let lastAssistant: Row | undefined;
  // Stable across prepending older history, session switches and stream replay.
  const step = extractionStep;
  const flush = (live: boolean) => {
    if (!current) return;
    const last = current.items[current.items.length - 1];
    if (last?.kind === 'content' && lastAssistant?.done
      && (!live || lastAssistant.stopReason === 'stop')) {
      last.final = true;
      current.finalContent = last;
    }
    if (!live) current.items.forEach(item => { item.streaming = false; });
    if (prefix && anchor === prefix.anchor) {
      const loaded = new Set(current.items.map(item => `${item.kind}:${item.id}`));
      current.hiddenStepCount = prefix.item_keys.filter(key => !loaded.has(key)).length;
    }
    groups.push(current);
    current = undefined;
    lastAssistant = undefined;
  };
  for (const row of rows) {
    if (row.role === 'user' || row.role === 'notice') {
      flush(false);
      groups.push(row.role === 'user'
        ? { type: 'user', id: row.id, step: step(row.id), message: row.text, ...(row.images?.length ? { images: row.images } : {}) }
        : row.error ? { type: 'error', id: row.id, step: step(row.id), message: row.text }
          : { type: 'notice', id: row.id, text: row.text });
      anchor = row.id;
      continue;
    }
    current ??= { type: 'activity', id: `extract-${anchor}`, items: [] };
    if (row.role === 'tool') {
      const item: ActivityItem = { kind: 'tool', id: row.id, step: step(row.id), name: row.tool,
        arguments: row.args, resultFormat: 'text', ...(row.images?.length ? { images: row.images } : {}) };
      if (row.done) {
        item.ok = !row.error;
        if (row.error) item.error = row.text;
        else item.result = row.text;
      } else item.progressText = row.text;
      if (!row.done && pending?.toolCallId === row.id) {
        item.permission = { id: pending.requestId || row.id, name: row.tool || '', label: '', risk: '', mode: '', arguments: row.args };
      }
      current.items.push(item);
      lastAssistant = undefined;
    } else {
      if (row.thinking) current.items.push({ kind: 'reasoning', id: row.id, step: step(row.id), content: row.thinking, streaming: running && !row.done });
      if (row.text) current.items.push({ kind: 'content', id: row.id, step: step(row.id), content: row.text, streaming: running && !row.done });
      lastAssistant = row;
    }
  }
  flush(running);
  return groups;
}

export function extractionContextUsage(event?: AgentEvent): AgentContextUsage | null {
  const used = event?.used_tokens, window = event?.window_tokens;
  return typeof used === 'number' && Number.isFinite(used) && used >= 0
    && typeof window === 'number' && Number.isFinite(window) && window > 0
    ? { used_tokens: used, window_tokens: window, ...(event?.cache ? { cache: event.cache } : {}) } : null;
}
