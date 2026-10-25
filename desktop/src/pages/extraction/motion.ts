import type { AgentEvent as TranslationEvent } from '../../lib/api';
import { extractionStep } from './presentation';
import type { AgentEvent } from './types';

/** Adapt live/newly submitted turn events to the shared animation cues. */
export function extractionMotionEvent(event: AgentEvent): TranslationEvent | null {
  if (event.type === 'user_message') return { type: 'user_message', step: extractionStep(`user-${event.seq}`) };
  if (event.type === 'tool_start' || event.type === 'tool_end') return {
    type: event.type === 'tool_start' ? 'tool_call' : 'tool_result', step: 0,
    id: event.toolCallId || `tool-${event.seq}`,
  };
  if (event.type === 'assistant_delta' || event.type === 'thinking_delta') return {
    type: event.type === 'assistant_delta' ? 'content_delta' : 'reasoning_delta', step: 0, delta: event.text,
  };
  if (event.type === 'assistant_message' && event.stopReason === 'stop') return {
    type: 'finish', step: extractionStep(event.messageId || `assistant-${event.seq}`), summary: event.text,
  };
  if (event.type === 'error' || (event.type === 'state' && ['stopped', 'interrupted', 'failed'].includes(event.status || ''))) {
    return { type: 'stopped', step: 0 };
  }
  return null;
}
