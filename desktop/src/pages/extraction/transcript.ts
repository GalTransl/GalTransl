import type { AgentEvent } from './types';

export type Row = { id: string; role: 'user' | 'assistant' | 'tool' | 'notice'; text: string; images?: AgentEvent['images']; thinking?: string; tool?: string; args?: Record<string, unknown>; done?: boolean; error?: boolean; stopReason?: string; partial?: boolean };
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
    else if (['assistant_start', 'assistant_delta', 'assistant_message', 'thinking_delta', 'thinking_message'].includes(type)) {
      const row = get(event.messageId || `assistant-${event.seq}`, 'assistant');
      if (type === 'assistant_message') { row.text = event.text || ''; row.done = true; row.stopReason = event.stopReason; row.partial = event.partial; }
      if (type === 'assistant_delta') row.text = (row.text + (event.text || '')).slice(-64000);
      if (type === 'thinking_delta') row.thinking = ((row.thinking || '') + (event.text || '')).slice(-64000);
      if (type === 'thinking_message') row.thinking = event.text || '';
    } else if (type.startsWith('tool_')) {
      const row = get(event.toolCallId || `tool-${event.seq}`, 'tool');
      if (type === 'tool_start') { row.tool = event.toolName; row.args = event.arguments; }
      if (type === 'tool_output') row.text = event.replace ? (event.text || '') : (row.text + (event.text || '')).slice(-16000);
      if (type === 'tool_end') { row.text = event.text || ''; row.done = true; row.error = event.isError; if (event.images?.length) row.images = event.images; }
    } else if (type === 'error' || type === 'notice') {
      Object.assign(get(`notice-${event.seq}`, 'notice'), { text: event.message || '', error: type === 'error' });
    }
  }
  return next.slice(-1000);
}
