import type { AgentEvent, AgentStreamingMessage } from '../../lib/api';

const HISTORY_KEY = 'galtransl-project-history';

type HistoryEntry = {
  projectDir: string;
  configFileName: string;
  lastOpened: string;
};

/* ── Session persistence ──
   The backend keeps agent state per project process-wide, but the visible
   "conversation" (events) lives in the page. Persist it so reopening the page
   or restarting the app restores the transcript instead of a blank slate. */

type TranscriptSession = {
  projectDir: string;
  sessionId: string;
  events: AgentEvent[];
  status: string;
  goal: string;
  startedAt: number;
  finishedAt: number;
};

export function sessionsKey(projectDir: string, sessionId: string): string {
  return `galtransl-agent-session:${projectDir}:${sessionId}`;
}

/** 记住每个项目当前选中的会话 id，刷新页面后回到同一个会话。 */
function activeSessionKey(projectDir: string): string {
  return `galtransl-agent-session-id:${projectDir}`;
}

export function loadActiveSessionId(projectDir: string): string {
  try {
    return localStorage.getItem(activeSessionKey(projectDir)) || '';
  } catch {
    return '';
  }
}

export function saveActiveSessionId(projectDir: string, sessionId: string): void {
  try {
    if (sessionId) localStorage.setItem(activeSessionKey(projectDir), sessionId);
    else localStorage.removeItem(activeSessionKey(projectDir));
  } catch {
    // 存储不可用不影响主流程
  }
}

/** 每个会话自己绑定的后端配置：projectDir -> sessionId -> 配置名。
 *  后端配置的选择此前是页面级状态——切到别的会话改一下再回来，原会话就跟着变了。
 *  绑到会话上之后，改配置只影响当前会话。sessionId 为 '' 的是「新会话草稿」：
 *  还没发出第一条消息时选的，建会话后迁移到新 sid。 */
const SESSION_BACKENDS_KEY = 'galtransl-agent-session-backends';

export type SessionBackendMap = Record<string, Record<string, string>>;

export function loadSessionBackends(): SessionBackendMap {
  try {
    const raw = localStorage.getItem(SESSION_BACKENDS_KEY);
    return raw ? (JSON.parse(raw) as SessionBackendMap) : {};
  } catch {
    return {};
  }
}

export function saveSessionBackends(map: SessionBackendMap): void {
  try {
    localStorage.setItem(SESSION_BACKENDS_KEY, JSON.stringify(map));
  } catch {
    // 存储不可用不影响主流程
  }
}

export function loadSession(projectDir: string, sessionId: string): TranscriptSession | null {
  if (!sessionId) return null;
  try {
    const raw = localStorage.getItem(sessionsKey(projectDir, sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as TranscriptSession;
    if (!parsed || !Array.isArray(parsed.events)) return null;
    // Delta/tick events are a real-time-only SSE side channel.  They are not
    // persisted by the backend and therefore must never contribute to the
    // resume cursor after a restart.  Filter them here as well as in
    // mergeTranscriptEvents so caches written by older versions are safe.
    return { ...parsed, events: persistedTranscriptEvents(parsed.events) };
  } catch {
    return null;
  }
}

export function saveSession(session: TranscriptSession): void {
  if (!session.sessionId) return;
  try {
    // Bound the payload: keep the tail of very long transcripts.
    // 首条用户消息是会话的身份锚点，即使历史很长也要保留，不能只取尾部。
    // Keep only replayable events in the durable browser cache.  The backend
    // intentionally keeps content/reasoning deltas and wait ticks in a
    // transient SSE queue; persisting them would advance after_step beyond
    // the backend's post-restart cursor and permanently skip new events.
    const events = boundTranscriptEvents(persistedTranscriptEvents(session.events));
    localStorage.setItem(sessionsKey(session.projectDir, session.sessionId), JSON.stringify({ ...session, events }));
  } catch {
    // Quota or serialization failure is non-fatal.
  }
}

const TRANSIENT_EVENT_TYPES = new Set<AgentEvent['type']>([
  'content_delta',
  'reasoning_delta',
  'wait_tick',
  // 上下文用量只驱动指示器，不进转录、也不该进浏览器缓存（刷新后由状态快照给）
  'context_usage',
  // 压缩的开始/结束只是过程指示：终态有持久事件 compacted，刷新后由它重建
  'compacting',
  // 队列快照（排队消息的实时变更）同理：不进转录、不进缓存
  'queue',
  // 子代理的逐步活动**不在这里**：它们同样是瞬态的（后端不进内存 events 窗口），但后端
  // 会把它落盘、转录回放时会带上（见后端 _SUBAGENT_STEP_EVENTS）。放进这个集合就等于
  // 重建时把它们全过滤掉——切页/刷新后展开子代理就只剩 start/done 两条了。
]);

export function persistedTranscriptEvents(events: AgentEvent[]): AgentEvent[] {
  // streaming=true 的是"进行中的消息"快照合成的临时事件：每次都从后端快照重建，
  // 绝不能进缓存或参与合并，否则会和收尾后的正式 assistant_message 重复一份。
  return events.filter((event) => !TRANSIENT_EVENT_TYPES.has(event.type) && !event.streaming);
}

function boundTranscriptEvents(events: AgentEvent[], limit = 600): AgentEvent[] {
  if (events.length <= limit) return events;
  const firstUser = events.find((event) => event.type === 'user_message');
  if (!firstUser) return events.slice(-limit);
  return [firstUser, ...events.slice(-(limit - 1))];
}

export function readHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? (JSON.parse(raw) as HistoryEntry[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function shortName(projectDir: string): string {
  return projectDir.replace(/[\\/]/g, '/').split('/').filter(Boolean).pop() || projectDir;
}

/** 事件数组里的最大 step（忽略本地乐观的 -1）。 */
export function maxStep(events: AgentEvent[]): number {
  let max = 0;
  for (const ev of events) {
    if (typeof ev.step === 'number' && ev.step > max) max = ev.step;
  }
  return max;
}

/** 把「正在生成的助手消息」接在转录尾部（进行中消息快照语义）。
 *  刷新/切会话时照样看得到正在写的思考与正文，随后的 delta 会继续往这批卡片上
 *  追加；等响应落定，正式的 assistant_message 事件会认领并校正它们。
 *  step 取快照自报的序号，调用方据此推进续订游标，避免重复补增量。 */
export function seedStreaming(
  events: AgentEvent[],
  streaming: AgentStreamingMessage | null | undefined,
): AgentEvent[] {
  if (!streaming || !streaming.parts?.length) return events;
  return [...events, {
    type: 'assistant_message',
    step: streaming.step,
    parts: streaming.parts,
    streaming: true,
  }];
}

export function mergeTranscriptEvents(cached: AgentEvent[], backend: AgentEvent[]): AgentEvent[] {
  const byStep = new Map<number, AgentEvent>();
  const extras: AgentEvent[] = [];
  // Older caches may contain transient events.  They are not present in a
  // post-restart backend snapshot, so retaining them would make maxStep()
  // return a cursor the backend can never reach.
  for (const event of persistedTranscriptEvents(cached)) {
    if (typeof event.step === 'number' && event.step >= 0) byStep.set(event.step, event);
    else extras.push(event);
  }
  // 后端同 step 的事件覆盖本地缓存；它是恢复后的权威副本。
  for (const event of persistedTranscriptEvents(backend)) {
    if (typeof event.step === 'number' && event.step >= 0) byStep.set(event.step, event);
    else extras.push(event);
  }
  const ordered = [...byStep.entries()]
    .sort(([a], [b]) => a - b)
    .map(([, event]) => event);
  return [...extras.filter((event) => event.step < 0), ...ordered];
}
