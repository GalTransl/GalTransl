import type { AgentEvent } from './types';

export type Row = { id: string; role: 'user' | 'assistant' | 'tool' | 'notice' | 'retry' | 'compact'; text: string; images?: AgentEvent['images']; thinking?: string; tool?: string; args?: Record<string, unknown>; done?: boolean; error?: boolean; stopReason?: string; partial?: boolean;
  durationMs?: number; thinkingDurationMs?: number; contentDone?: boolean; thinkingDone?: boolean;
  durations?: Record<string, { kind: 'content' | 'reasoning'; ms: number }>;
  compact?: Pick<AgentEvent, 'mode' | 'removed' | 'cleared' | 'summary_chars' | 'tokens_before' | 'tokens_after'>;
  retry?: Pick<AgentEvent, 'attempt' | 'max_attempts' | 'delay_ms' | 'ts' | 'code' | 'reason'> };
export function reduceEvents(rows: Row[], events: AgentEvent[]): Row[] {
  const next = rows.slice();
  const changed = new Set<string>();
  const index = new Map(next.map((row, i) => [row.id, i]));
  function get(id: string, role: Row['role']) {
    let at = index.get(id);
    if (at === undefined) { at = next.length; index.set(id, at); next.push({ id, role, text: '' }); changed.add(id); }
    else if (!changed.has(id)) { next[at] = { ...next[at] }; changed.add(id); }
    return next[at];
  }
  for (const event of events) {
    const type = event.type;
    if (type === 'user_message') Object.assign(get(`user-${event.seq}`, 'user'), { text: event.text || '', ...(event.images?.length ? { images: event.images } : {}) });
    else if (['assistant_start', 'assistant_delta', 'assistant_message', 'thinking_delta', 'thinking_message', 'content_end', 'reasoning_end'].includes(type)) {
      const row = get(event.messageId || `assistant-${event.seq}`, 'assistant');
      if (type === 'assistant_message') { row.text = event.text || ''; row.done = true; row.stopReason = event.stopReason; row.partial = event.partial; }
      if (type === 'assistant_delta') { row.text = (row.text + (event.text || '')).slice(-64000); row.contentDone = false; }
      if (type === 'thinking_delta') { row.thinking = ((row.thinking || '') + (event.text || '')).slice(-64000); row.thinkingDone = false; }
      if (type === 'thinking_message') row.thinking = event.text || '';
      if (type === 'content_end' || type === 'reasoning_end') {
        const kind = type === 'content_end' ? 'content' : 'reasoning';
        // Sequence keys prevent split history and replay from counting a segment twice.
        Object.assign(row, streamTiming({ ...row.durations, [event.seq ?? `${type}-${Object.keys(row.durations || {}).length}`]: { kind, ms: event.duration_ms ?? 0 } as const }));
        if (kind === 'content') row.contentDone = true;
        if (kind === 'reasoning') row.thinkingDone = true;
      }
    } else if (type.startsWith('tool_')) {
      const row = get(event.toolCallId || `tool-${event.seq}`, 'tool');
      if (type === 'tool_start') { row.tool = event.toolName; row.args = event.arguments; }
      if (type === 'tool_output') row.text = event.replace ? (event.text || '') : (row.text + (event.text || '')).slice(-16000);
      if (type === 'tool_end') { row.text = event.text || ''; row.done = true; row.error = event.isError; row.durationMs = event.duration_ms; if (event.images?.length) row.images = event.images; }
    } else if (type === 'compacted') {
      get(`compact-${event.seq}`, 'compact').compact = { mode: event.mode, removed: event.removed, cleared: event.cleared,
        summary_chars: event.summary_chars, tokens_before: event.tokens_before, tokens_after: event.tokens_after };
    } else if (type === 'llm_retry_start' || type === 'llm_retry_end') {
      const row = get(`retry-${event.messageId || event.seq}`, 'retry');
      row.done = type === 'llm_retry_end';
      if (type === 'llm_retry_start') row.retry = { attempt: event.attempt, max_attempts: event.max_attempts,
        delay_ms: event.delay_ms, ts: event.ts, code: event.code, reason: event.reason };
    } else if (type === 'error' || type === 'notice') {
      Object.assign(get(`notice-${event.seq}`, 'notice'), { text: event.message || '', error: type === 'error' });
    }
  }
  return next;
}

function streamTiming(durations: NonNullable<Row['durations']>) {
  const segments = Object.values(durations);
  return { durations, durationMs: segments.filter(segment => segment.kind === 'content').reduce((sum, segment) => sum + segment.ms, 0),
    thinkingDurationMs: segments.filter(segment => segment.kind === 'reasoning').reduce((sum, segment) => sum + segment.ms, 0) };
}

export function prependHistory(rows: Row[], history: Row[]) {
  const byId = new Map(history.map(row => [row.id, row]));
  const merged = rows.map(row => {
    const older = byId.get(row.id);
    return older ? { ...older, ...row,
      ...(row.role === 'tool' ? { tool: row.tool ?? older.tool, args: row.args ?? older.args } : {}),
      ...(row.role === 'assistant' ? { thinking: row.thinking ?? older.thinking,
        ...(row.durations || older.durations ? streamTiming({ ...older.durations, ...row.durations }) : {}),
        text: row.done || row.text ? row.text : older.text } : {}),
    } : row;
  });
  const ids = new Set(rows.map(row => row.id));
  return [...history.filter(row => !ids.has(row.id)), ...merged];
}
