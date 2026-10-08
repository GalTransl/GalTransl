import type { AgentEvent } from '../../lib/api';
import { buildTimeline, type TimelineGroup } from './timeline';

// Payloads come straight from immutable events. Never walk large tool results.
const PAYLOAD_KEYS = new Set(['arguments', 'args', 'result', 'preview']);

function shareModel(previous: unknown, next: unknown): unknown {
  if (Object.is(previous, next)) return previous;
  if (!previous || !next || typeof previous !== 'object' || typeof next !== 'object') return next;
  if (Array.isArray(previous) !== Array.isArray(next)) return next;
  const before = previous as Record<string, unknown>;
  const after = next as Record<string, unknown>;
  const keys = Object.keys(after);
  let same = Object.keys(before).length === keys.length;
  for (const key of keys) {
    const value = PAYLOAD_KEYS.has(key) ? after[key] : shareModel(before[key], after[key]);
    after[key] = value;
    if (!Object.prototype.hasOwnProperty.call(before, key) || !Object.is(before[key], value)) same = false;
  }
  return same ? previous : next;
}

/** Share unchanged render models, including tools inside a still-growing turn. */
export function createTimelineBuilder() {
  let previous: TimelineGroup[] = [];
  return (events: AgentEvent[]): TimelineGroup[] => {
    const byId = new Map(previous.map((group) => [group.id, group]));
    const next = buildTimeline(events).map((group) => shareModel(byId.get(group.id), group) as TimelineGroup);
    previous = next.length === previous.length && next.every((group, index) => group === previous[index])
      ? previous
      : next;
    return previous;
  };
}
