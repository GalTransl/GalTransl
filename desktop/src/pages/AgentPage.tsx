import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import {
  addOpenProject,
  AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  getAgentDefaultBackendProfile,
  getBackendProfile,
  getBackendProfileNames,
  loadOpenProjects,
  OPEN_PROJECTS_CHANGE_EVENT,
  readConfigFileName,
  stopAgent,
  startAgent,
  sendAgentMessage,
  resetAgent,
  subscribeAgentStream,
  fetchAgentStatus,
  fetchAgentTranscript,
  listAgentSessions,
  createAgentSession,
  deleteAgentSession,
  deleteAgentQueued,
  getAgentTranslatorBackendContext,
  updateAgentQueued,
  sendAgentQueuedNow,
  answerAgentAsk,
  answerAgentPermission,
  setAgentPermissionMode,
  encodeProjectDir,
  fetchProjectRuntime,
  fetchJobs,
  type AgentContextUsage,
  type AgentEvent,
  type AgentSession as AgentSessionMeta,
  type AgentStreamingMessage,
  type QueuedMessage,
  type ProjectRuntimeResponse,
  type RuntimeJob,
} from '../lib/api';
import { normalizeError } from '../lib/errors';
import {
  PERMISSION_MODE_HINTS,
  PERMISSION_MODE_LABELS,
  PERMISSION_MODES,
  loadPermissionMode,
  normalizePermissionMode,
  savePermissionMode,
  type PermissionDecision,
  type PermissionMode,
} from '../lib/permissionMode';
import { AgentMarkdown, invalidateCacheFilesForToolResult } from '../components/AgentCacheRef';
import { Icon, type IconName } from '../components/Icon';
import { formatProfileLabel } from '../lib/backendProfile';
import {
  clampPercent,
  formatElapsedTime,
  formatEta,
  formatPercentDisplay,
  formatSpeed,
} from './translateRuntimeShared';

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

function sessionsKey(projectDir: string, sessionId: string): string {
  return `galtransl-agent-session:${projectDir}:${sessionId}`;
}

/** 记住每个项目当前选中的会话 id，刷新页面后回到同一个会话。 */
function activeSessionKey(projectDir: string): string {
  return `galtransl-agent-session-id:${projectDir}`;
}

function loadActiveSessionId(projectDir: string): string {
  try {
    return localStorage.getItem(activeSessionKey(projectDir)) || '';
  } catch {
    return '';
  }
}

function saveActiveSessionId(projectDir: string, sessionId: string): void {
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

type SessionBackendMap = Record<string, Record<string, string>>;

function loadSessionBackends(): SessionBackendMap {
  try {
    const raw = localStorage.getItem(SESSION_BACKENDS_KEY);
    return raw ? (JSON.parse(raw) as SessionBackendMap) : {};
  } catch {
    return {};
  }
}

function saveSessionBackends(map: SessionBackendMap): void {
  try {
    localStorage.setItem(SESSION_BACKENDS_KEY, JSON.stringify(map));
  } catch {
    // 存储不可用不影响主流程
  }
}

function loadSession(projectDir: string, sessionId: string): TranscriptSession | null {
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

function saveSession(session: TranscriptSession): void {
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

function persistedTranscriptEvents(events: AgentEvent[]): AgentEvent[] {
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

function readHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? (JSON.parse(raw) as HistoryEntry[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function shortName(projectDir: string): string {
  return projectDir.replace(/[\\/]/g, '/').split('/').filter(Boolean).pop() || projectDir;
}

/* ── Timeline model ──
   Raw SSE events are folded into render groups: runs of thinking/tool activity
   collapse into one summary row ("工作 6 秒 · 4 步"), while terminal moments
   (finish / error / stopped) and the initial user goal stay as their own rows.
   This mirrors how modern agent clients avoid a wall of one-line cards. */

type ActivityItem = {
  kind: 'content' | 'reasoning' | 'tool' | 'compact' | 'retry';
  step: number;
  content?: string;
  id?: string;
  name?: string;
  arguments?: unknown;
  ok?: boolean;
  result?: unknown;
  error?: string;
  durationMs?: number;
  // 权限审批：后端在这个工具调用执行前挂起等用户点（见 PermissionCard）。
  // 没有超时字段——后端不设超时，卡片会一直等着（老会话事件里可能还带着 timeout_s /
  // started_at，这里不读、也不用）。
  permission?: {
    id: string;
    name: string;
    label: string;
    risk: string;
    mode: string;
    arguments?: Record<string, unknown>;
    // 「将要变更」预览：后端在挂起前算好的 before→after（只读算出来的，不是执行结果）。
    // 结构与工具结果里那份一致，交给 extractChangeList 认（见 PermissionCard）。
    preview?: unknown;
  };
  // 流式 content/reasoning：正在收 delta、还没收到对应的 *_end
  streaming?: boolean;
  // 回合的收尾回复：渲染时提升为顶层普通消息（不折进活动组）
  final?: boolean;
  // wait tool: 倒计时快照
  waitTotalMs?: number;
  waitRemainingMs?: number;
  waitInterrupted?: boolean;
  // compact: 上下文压缩提示（前后都是估算 token；tokensAfter 含保留下来的尾部）
  removed?: number;
  summaryChars?: number;
  tokensBefore?: number;
  tokensAfter?: number;
  // retry: LLM 请求失败自动重试（倒计时 + 第 N/M 次）
  attempt?: number;
  maxAttempts?: number;
  retryDelayMs?: number;
  retryStartedAtMs?: number;
  retryCode?: string;
  retryReason?: string;
  retryDone?: boolean;
  // 子代理：发起它的那次 run_subagents 调用下面挂的子代理行（见 SubagentList）
  subagents?: SubagentRun[];
};

/** 子代理自己的一步（工具调用或一句说明），只在对它展开时显示。 */
type SubagentStep =
  | { kind: 'text'; round: number; text: string }
  | {
      kind: 'tool';
      id: string;
      name: string;
      args?: unknown;
      ok?: boolean;
      result?: unknown;
      error?: string;
      durationMs?: number;
      at: number;
    };

/** 一个子代理的运行状态（由 subagent_* 事件累积出来）。 */
type SubagentRun = {
  id: string;
  agent: string;
  /** 角色中文名（校对） */
  label: string;
  file: string;
  indexes: string;
  brief: string;
  parentId?: string;
  status: 'running' | 'done' | 'failed' | 'stopped' | 'max_rounds';
  steps: SubagentStep[];
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  /** 正在退避重试（subagent_retry，瞬态）：拿到下一次成功的响应就清掉 */
  retry?: { attempt: number; maxAttempts: number; code?: string; reason?: string };
  turns?: number;
  toolCalls?: number;
  /** 写了几条校对意见 */
  doubts?: number;
  report?: string;
  error?: string;
};

type TimelineGroup =
  | { type: 'activity'; id: string; items: ActivityItem[]; finalContent?: ActivityItem }
  | { type: 'user'; id: string; step: number; message: string }
  | { type: 'error'; id: string; step: number; message: string; traceback?: string }
  | { type: 'stopped'; id: string; step: number; reason: string };

/** run_subagents 的结果一到，就按结果里的 tasks 把子代理状态对齐一遍。

    结果是最终名单：父回合被停止时，没跑完的子代理等不到自己的 subagent_done 事件
    （后端把它们的终态直接写进了结果的 tasks，status=stopped）。不对齐的话这些行会
    永远停在"进行中"，头部就一直挂着一个假的「N/16 个在跑」。 */
function reconcileSubagentStatuses(target: ActivityItem) {
  if (target.kind !== 'tool' || target.name !== 'run_subagents') return;
  const runs = target.subagents;
  if (!runs?.length || typeof target.result !== 'object' || target.result === null) return;
  const tasks = (target.result as Record<string, unknown>).tasks;
  if (!Array.isArray(tasks)) return;
  for (const run of runs) {
    if (run.status !== 'running') continue; // 已有终态的（done 事件先到）听事件的
    const task = tasks.find(
      (task): task is Record<string, unknown> =>
        typeof task === 'object' && task !== null && (task as Record<string, unknown>).id === run.id,
    );
    if (!task) continue;
    const status = String(task.status || '');
    if (!status || status === 'running') continue;
    run.status = status as SubagentRun['status'];
    if (typeof task.error === 'string' && task.error) run.error = task.error;
    if (typeof task.report === 'string' && task.report) run.report = task.report;
    if (typeof task.duration_ms === 'number') run.durationMs = task.duration_ms;
  }
}

function buildTimeline(events: AgentEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = [];
  let current: Extract<TimelineGroup, { type: 'activity' }> | null = null;
  // 助手段落（parts）的认领游标：parts 与 delta 拼出来的卡片按顺序一一对应
  // （两侧口径一致，见后端 _assistant_parts）。实时流里卡片已由 delta 建好，
  // 这里按顺序认领并写入权威文本；重建时没有 delta，就按 parts 顺序新建。
  // 游标只在同一个活动组内前进，组结束即归零。
  let partsCursor = 0;

  // final=true 表示组被真正终结（用户消息/finish/error/stopped）：此时兜底
  // 撤掉残留光标。buildTimeline 每次全量重算，结尾的尾组冲刷不能算终结——
  // 尾组正是正在流式的活动组，在那里清标志会让活卡片永远显示「已思考」。
  const closeActivity = (final = false) => {
    if (current && final) {
      // 兜底：end 事件丢失时（异常中断/旧会话回放），组终结强制撤掉残留光标
      for (const it of current.items) {
        if ((it.kind === 'content' || it.kind === 'reasoning') && it.streaming) it.streaming = false;
      }
    }
    // finalContent 也算有效内容：纯文字回复的回合里 items 会被清空
    // （finish 把同文的流式 content 移出折叠区），只剩 finalContent 也要入组。
    if (current && (current.items.length || current.finalContent)) groups.push(current);
    current = null;
    partsCursor = 0;
  };

  // 收掉指定流（content/reasoning）里所有还在流式的段：撤光标、记耗时。
  // 交替思考模型一路流里 想/说 来回切换，end 到达时它对应的段未必还是
  // items 的最后一条（中间可能隔着别的段），所以按 kind 扫，不能只看末尾。
  const endStreamKind = (kind: 'content' | 'reasoning', durationMs?: number) => {
    if (!current) return;
    let last: ActivityItem | null = null;
    for (const it of current.items) {
      if (it.kind === kind && it.streaming) {
        it.streaming = false;
        last = it;
      }
    }
    if (last && typeof durationMs === 'number') {
      // 同一张卡片在一次请求里可能收尾多次（想→说→想），耗时累加成总时长
      last.durationMs = (last.durationMs || 0) + durationMs;
    }
  };

  // 流式增量落点：从末尾往前找同 kind 的块拼回去。交替思考的 后段 要拼回
  // 前面的块（同一段思考/同一个回复），而不是新起一块掉到回复下面；扫描
  // 跨过说/想块，遇到工具/压缩行即止——那意味着上一轮请求已结束，不能跨轮拼。
  const findAppendTarget = (kind: 'content' | 'reasoning'): ActivityItem | null => {
    if (!current) return null;
    for (let i = current.items.length - 1; i >= 0; i -= 1) {
      const it = current.items[i];
      if (it.kind === 'tool' || it.kind === 'compact' || it.kind === 'retry') return null;
      if (it.kind === kind && it.streaming !== undefined) return it;
    }
    return null;
  };

  // 工具行 upsert：tool_call 事件与助手消息里的 tool_call 段落都走这里，按 id
  // 复用同一行——两处都会发，界面不能出现两行。
  const upsertToolCall = (
    items: ActivityItem[],
    step: number,
    part: { id?: string; name?: string; arguments?: unknown },
  ) => {
    const existing = part.id ? items.find((it) => it.kind === 'tool' && it.id === part.id) : undefined;
    if (existing) {
      existing.name = part.name ?? existing.name;
      existing.arguments = part.arguments;
      return;
    }
    items.push({ kind: 'tool', step, id: part.id, name: part.name, arguments: part.arguments });
  };

  for (const ev of events) {
    // status/close/context_usage/queue 是控制与指标事件，不进对话转录
    if (
      ev.type === 'status' ||
      ev.type === 'close' ||
      ev.type === 'context_usage' ||
      ev.type === 'queue'
    )
      continue;

    // 用户消息独立成行（右对齐气泡），并打断当前活动组。
    if (ev.type === 'user_message') {
      closeActivity(true);
      groups.push({ type: 'user', id: `u-${ev.step}`, step: ev.step, message: ev.message || '' });
      continue;
    }

    if (ev.type === 'content') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({ kind: 'content', step: ev.step, content: ev.content });
      continue;
    }

    // 流式「说」增量：拼回本轮请求里已有的 content 块（打字机效果）；
    // 没有可拼接的（比如恢复会话时第一事件就是 delta）才新起一条。
    if (ev.type === 'content_delta') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = findAppendTarget('content');
      if (target) {
        target.content = (target.content || '') + (ev.delta || '');
        target.streaming = true;
      } else {
        current.items.push({
          kind: 'content',
          step: ev.step,
          content: ev.delta || '',
          streaming: true,
        });
      }
      continue;
    }

    // 一段「说」的流结束：撤掉打字机光标，并记下这段回复的耗时
    if (ev.type === 'content_end') {
      endStreamKind('content', ev.duration_ms);
      continue;
    }

    // 思考流（reasoning）：与「说」（content）分开成独立的可折叠卡片。
    // 增量拼回本轮请求里已有的思考卡片（交替思考的尾部也归位到上面那张），
    // 没有可拼接的才新起一条。
    if (ev.type === 'reasoning_delta') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = findAppendTarget('reasoning');
      if (target) {
        target.content = (target.content || '') + (ev.delta || '');
        target.streaming = true;
      } else {
        current.items.push({
          kind: 'reasoning',
          step: ev.step,
          content: ev.delta || '',
          streaming: true,
        });
      }
      continue;
    }

    // 思考流结束：卡片收尾（撤光标、记耗时）。只挂在已有卡片上、不新建——
    // 重建时的卡片由 assistant_message 的 parts 建（delta 是瞬态的，不回放）。
    if (ev.type === 'reasoning_end') {
      endStreamKind('reasoning', ev.duration_ms);
      continue;
    }

    // 助手消息：思考/正文/工具调用的有序段落，转录里文本的唯一来源。
    // delta 是瞬态的（刷新/切会话后根本不存在），重建完全靠这里；实时流里则
    // 按顺序"认领"delta 已经建好的卡片、写入权威文本（幂等，不会多出一份）。
    // streaming=true 的是"进行中"快照（status().streaming 合成），后续 delta
    // 会继续往这批卡片上追加。
    if (ev.type === 'assistant_message') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const partial = Boolean(ev.streaming);
      for (const part of ev.parts || []) {
        if (part.type === 'tool_call') {
          upsertToolCall(current.items, ev.step, part);
          continue;
        }
        const kind = part.type === 'reasoning' ? 'reasoning' : 'content';
        let claimed: ActivityItem | undefined;
        for (let i = partsCursor; i < current.items.length; i += 1) {
          if (current.items[i].kind === kind) {
            claimed = current.items[i];
            partsCursor = i + 1;
            break;
          }
        }
        if (claimed) {
          claimed.content = part.text;
          claimed.streaming = partial;
        } else {
          current.items.push({ kind, step: ev.step, content: part.text, streaming: partial });
          partsCursor = current.items.length;
        }
      }
      continue;
    }

    if (ev.type === 'tool_call') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      upsertToolCall(current.items, ev.step, { id: ev.id, name: ev.name, arguments: ev.arguments });
      continue;
    }

    if (ev.type === 'tool_result') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = ev.id ? current.items.find((it) => it.kind === 'tool' && it.id === ev.id) : undefined;
      if (target) {
        target.ok = ev.ok;
        target.result = ev.result;
        target.error = ev.error;
        target.durationMs = ev.duration_ms;
        reconcileSubagentStatuses(target);
      } else {
        current.items.push({
          kind: 'tool',
          step: ev.step,
          id: ev.id,
          name: ev.name,
          ok: ev.ok,
          result: ev.result,
          error: ev.error,
          durationMs: ev.duration_ms,
        });
      }
      continue;
    }

    // 权限审批：挂到**本次工具调用**那一行上（不单独成行），卡片就摆在那行下面。
    // 后端在真正执行写操作前发这条事件并挂起，等用户点允许/拒绝。
    if (ev.type === 'permission_request') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = ev.tool_call_id
        ? current.items.find((it) => it.kind === 'tool' && it.id === ev.tool_call_id)
        : undefined;
      const item: ActivityItem =
        target ?? { kind: 'tool', step: ev.step, id: ev.tool_call_id, name: ev.name };
      if (!target) current.items.push(item);
      item.permission = {
        id: ev.id || '',
        name: ev.name || item.name || '',
        label: ev.label || '',
        risk: ev.risk || '',
        mode: ev.mode || '',
        arguments:
          ev.arguments && typeof ev.arguments === 'object' && !Array.isArray(ev.arguments)
            ? (ev.arguments as Record<string, unknown>)
            : undefined,
        // 后端算好的「将要变更」（编辑类工具才有；算不出就没有这个键）
        preview: ev.preview,
      };
      continue;
    }

    // wait 工具的倒计时事件：挂到**本次调用**那一行上，不单独成行。
    // 按 id 匹配；老日志/异常情形没有 id 时，退回"最近一个还没收到 wait_end 的
    // wait 行"——绝不能笼统找第一个，同一活动组里等待过多次时，tick 会一直打到
    // 最早那行，后面几次等待就没有倒计时条了（只有个笼统的"进行中"）。
    if (ev.type === 'wait_start' || ev.type === 'wait_tick' || ev.type === 'wait_end') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      let target = ev.id
        ? current.items.find((it) => it.kind === 'tool' && it.id === ev.id)
        : undefined;
      if (!target) {
        for (let i = current.items.length - 1; i >= 0; i -= 1) {
          const it = current.items[i];
          if (it.kind === 'tool' && it.name === 'wait' && it.waitInterrupted === undefined) {
            target = it;
            break;
          }
        }
      }
      if (target) {
        if (typeof ev.total_ms === 'number') target.waitTotalMs = ev.total_ms;
        if (typeof ev.remaining_ms === 'number') target.waitRemainingMs = ev.remaining_ms;
        if (ev.type === 'wait_end') {
          target.waitInterrupted = Boolean(ev.interrupted);
          target.waitRemainingMs = 0;
        }
      }
      continue;
    }

    // 上下文压缩：折叠成活动组里的一条提示行，不打断当前活动组。
    if (ev.type === 'compacted') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({
        kind: 'compact',
        step: ev.step,
        removed: ev.removed,
        summaryChars: ev.summary_chars,
        tokensBefore: ev.tokens_before,
        tokensAfter: ev.tokens_after,
      });
      continue;
    }

    // LLM 请求失败自动重试：先在活动组里放一条带倒计时的重试提示行，
    // 并丢掉上一次尝试已经流出的半截内容（那次请求已作废，重试会整段重发）。
    if (ev.type === 'llm_retry_start') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      // 从末尾往前清掉本次失败尝试流出的 content/reasoning；遇到工具/压缩/重试行
      // 说明已到上一轮边界，停止清除，避免误删之前的内容。
      for (let i = current.items.length - 1; i >= 0; i -= 1) {
        const it = current.items[i];
        if (it.kind === 'content' || it.kind === 'reasoning') {
          current.items.splice(i, 1);
          continue;
        }
        break;
      }
      current.items.push({
        kind: 'retry',
        step: ev.step,
        attempt: ev.attempt,
        maxAttempts: ev.max_attempts,
        retryDelayMs: ev.delay_ms,
        retryStartedAtMs: typeof ev.ts === 'number' ? ev.ts * 1000 : Date.now(),
        retryCode: ev.code,
        retryReason: ev.reason,
      });
      continue;
    }

    // 退避结束、下一次尝试已发出：把重试行定格为「已重试」，停掉倒计时。
    if (ev.type === 'llm_retry_end') {
      if (current) {
        for (let i = current.items.length - 1; i >= 0; i -= 1) {
          const it = current.items[i];
          if (it.kind === 'retry') {
            it.retryDone = true;
            break;
          }
        }
      }
      continue;
    }

    // 子代理（run_subagents）：挂到发起它的那次工具调用下面，一层就够——子代理没有子代理。
    // 事件带 id（本次派发）与 parent_id（那次工具调用），据此定位到行与具体哪个子代理。
    if (ev.type.startsWith('subagent_')) {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const host =
        current.items.find((it) => it.kind === 'tool' && it.id === ev.parent_id) ??
        // 老记录/事件乱序时的兜底：本组最后一次 run_subagents 调用
        [...current.items].reverse().find((it) => it.kind === 'tool' && it.name === 'run_subagents');
      if (!host) {
        continue;
      }
      if (!host.subagents) host.subagents = [];
      let run = host.subagents.find((item) => item.id === ev.id);
      if (!run) {
        run = {
          id: ev.id || '',
          agent: ev.agent || '',
          label: ev.label || '子代理',
          file: ev.file || '',
          indexes: ev.indexes || '',
          brief: ev.brief || '',
          status: 'running',
          steps: [],
          // 用后端给的时间戳：subagent_start 是持久事件，刷新/切页会重放它，
          // 拿"事件到达时间"会让进行中的计时每次重建都归零。
          startedAt: typeof ev.started_at === 'number' ? ev.started_at * 1000 : Date.now(),
        };
        host.subagents.push(run);
      }
      // 重试期间后端会插一条 subagent_retry（瞬态）：标出来让用户知道它在退避，
      // 不是卡住了。任何后续动静都说明这次重试成功了，标记就地清掉。
      if (ev.type === 'subagent_retry') {
        run.retry = {
          attempt: typeof ev.attempt === 'number' ? ev.attempt : 1,
          maxAttempts: typeof ev.max_attempts === 'number' ? ev.max_attempts : 0,
          code: ev.code || '',
          reason: ev.reason || '',
        };
        continue;
      }
      run.retry = undefined;
      if (ev.type === 'subagent_message') {
        if (ev.text) run.steps.push({ kind: 'text', round: ev.round || 0, text: ev.text });
      } else if (ev.type === 'subagent_tool_call') {
        run.steps.push({
          kind: 'tool',
          id: ev.tool_call_id || '',
          name: ev.name || '',
          args: ev.arguments,
          at: Date.now(),
        });
      } else if (ev.type === 'subagent_tool_result') {
        const step = [...run.steps]
          .reverse()
          .find((item): item is Extract<SubagentStep, { kind: 'tool' }> =>
            item.kind === 'tool' && item.id === ev.tool_call_id);
        if (step) {
          step.ok = ev.ok !== false;
          step.result = ev.result;
          step.error = ev.error || '';
          step.durationMs = ev.duration_ms;
        }
      } else if (ev.type === 'subagent_done') {
        run.status = (ev.status as SubagentRun['status']) || 'done';
        run.report = ev.report || '';
        run.turns = ev.turns;
        run.toolCalls = ev.tool_calls;
        run.doubts = typeof ev.doubts === 'number' ? ev.doubts : 0;
        run.durationMs = ev.duration_ms;
        run.error = ev.error || '';
        run.finishedAt = typeof ev.finished_at === 'number' ? ev.finished_at * 1000 : Date.now();
      }
      continue;
    }

    // finish 是回合的收尾回复：作为活动组的 final 消息，渲染时提升为
    // 顶层普通文本（不折进折叠区），像对话里最后一条普通消息。
    if (ev.type === 'finish') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const summary = ev.summary || '';
      // 收尾回复保留折叠区里的流式 content（显示「思考 · 耗时」），
      // summary 另行提升为顶层 final 消息，两者并存。
      if (summary) {
        current.finalContent = { kind: 'content', step: ev.step, content: summary, final: true };
      }
      closeActivity(true); // 回合结束：把组压进 groups，后续事件（新的 user_message 等）起新组
      continue;
    }

    // Terminal moments close the current activity run.
    closeActivity(true);
    if (ev.type === 'error') {
      groups.push({
        type: 'error',
        id: `e-${ev.step}`,
        step: ev.step,
        message: ev.message || '未知错误',
        traceback: ev.traceback,
      });
    } else if (ev.type === 'stopped') {
      groups.push({
        type: 'stopped',
        id: `s-${ev.step}`,
        step: ev.step,
        reason: ev.reason || '用户停止',
      });
    }
  }

  closeActivity();
  return groups;
}

/** 发送/插话按钮的图标：上箭头。原来是一个箭头字符，字重/基线随字体走，
 *  改成 SVG 后与页面其它图标（开文件夹、上下文环）口径一致。 */
function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 19V5m0 0-5.5 5.5M12 5l5.5 5.5"
      />
    </svg>
  );
}

/** 停止按钮的图标：实心圆角方块（原来是 CSS 画的方块）。 */
function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor" />
    </svg>
  );
}

/* ── Tool presentation ──
   Each backend tool maps to an action verb + icon + the most salient argument,
   so a row reads like "启动翻译 ForGal-json" rather than a raw function name. */

type ToolMeta = {
  action: string;
  running: string;
  /** 图标名（统一图标集）：渲染处 `<Icon name={meta.icon} />` */
  icon: IconName;
  summary: (args: Record<string, unknown> | undefined) => string;
  verb: string;
};

const TOOL_META: Record<string, ToolMeta> = {
  get_project_overview: {
    action: '了解项目',
    running: '了解项目',
    verb: '',
    icon: 'folder-open',
    // 带了 include 就亮出来，界面上一眼看出这次只取了哪几段
    summary: (a) =>
      Array.isArray(a?.include) && a.include.length
        ? `按需：${a.include.map((s) => str(s)).join('、')}`
        : '读取项目概况',
  },
  update_project_config: { action: '修改项目配置', running: '修改项目配置', verb: '', icon: 'sliders', summary: () => '调整翻译参数/规范等设置' },
  list_input_files: { action: '查看原文文件清单', running: '查看原文文件清单', verb: '', icon: 'archive', summary: () => '列出待翻译文件与句数' },
  read_input_file: { action: '读取原文', running: '读取原文', verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  read_guideline: {
    action: '读取翻译规范',
    running: '读取翻译规范',
    verb: '',
    icon: 'bookmark',
    summary: (a) => (str(a?.scope) === 'project' ? '项目规范' : str(a?.name)),
  },
  write_project_guideline: {
    action: '修改项目规范',
    running: '修改项目规范',
    verb: '',
    icon: 'pencil',
    summary: (a) => {
      const mode = str(a?.mode);
      if (mode === 'overwrite') return '整份覆写';
      if (mode === 'append') return '增写';
      if (mode === 'replace') return '替换一段';
      return mode;
    },
  },
  list_dict_files: { action: '查看字典清单', running: '查看字典清单', verb: '', icon: 'books', summary: () => '列出项目字典文件' },
  read_dict: { action: '读取字典', running: '读取字典', verb: '', icon: 'book', summary: (a) => str(a?.file_key) },
  save_dict: { action: '保存字典', running: '保存字典', verb: '', icon: 'save', summary: (a) => str(a?.file_key) },
  create_dict_file: { action: '新建字典', running: '新建字典', verb: '', icon: 'file-plus', summary: (a) => str(a?.filename) },
  get_name_table: { action: '读取人名表', running: '读取人名表', verb: '', icon: 'user', summary: () => 'name替换表' },
  save_name_table: { action: '保存人名表', running: '保存人名表', verb: '', icon: 'users', summary: (a) => (Array.isArray(a?.names) ? `${a.names.length} 条` : '') },
  start_translation: { action: '启动翻译', running: '启动翻译', verb: '', icon: 'play', summary: (a) => [str(a?.translator), ...(Array.isArray(a?.files) ? [`仅 ${a.files.length} 个文件`] : [])].filter(Boolean).join(' · ') },
  run_subagents: {
    // 子代理：一次调用带一批任务，界面上每个子代理一行（见 SubagentList）
    action: '派子代理',
    running: '子代理并行中',
    verb: '',
    icon: 'users',
    summary: (a) => {
      const tasks = Array.isArray(a?.tasks) ? a.tasks : [];
      if (!tasks.length) return '';
      // `file:"*" + count:N` 的展开发生在**后端**：入参里只有 1 个任务，实际会派 N 个。
      // 摘要要按展开后的数量说，否则"派子代理 1 个"和下面 16 行子代理对不上。
      const total = tasks.reduce((sum, task) => {
        const raw = (task as Record<string, unknown> | undefined)?.count;
        const n = typeof raw === 'number' && Number.isFinite(raw) ? Math.floor(raw) : 1;
        return sum + Math.max(1, n);
      }, 0);
      const files = tasks
        .map((task) => {
          const file = str((task as Record<string, unknown> | undefined)?.file);
          return file === '*' ? '自动均分' : file; // "*" 是"全部均分"的写法，照抄出来没人看得懂
        })
        .filter(Boolean);
      const head = files.slice(0, 2).join('、');
      const rest = files.length > 2 ? ` 等 ${files.length} 项` : '';
      return `${total} 个 · ${head}${rest}`;
    },
  },
  ask_user: {
    action: '询问用户',
    running: '等你回答',
    verb: '',
    icon: 'help',
    summary: (a) => {
      const questions = Array.isArray(a?.questions) ? a.questions : [];
      const first = questions[0] && typeof questions[0] === 'object'
        ? str((questions[0] as Record<string, unknown>).question)
        : '';
      return [first, questions.length > 1 ? `共 ${questions.length} 题` : ''].filter(Boolean).join(' · ');
    },
  },
  stop_translation: { action: '停止翻译', running: '停止翻译', verb: '', icon: 'stop', summary: () => '' },
  wait: { action: '等待', running: '等待中', verb: '', icon: 'hourglass', summary: (a) => waitSummary(a) },
  get_progress: { action: '查询进度', running: '查询进度', verb: '', icon: 'chart', summary: () => '' },
  get_runtime: { action: '查询运行时', running: '查询运行时', verb: '', icon: 'settings', summary: () => '' },
  list_problems: { action: '检查问题清单', running: '检查问题清单', verb: '', icon: 'search', summary: (a) => str(a?.problem_type) || '问题类型统计' },
  manage_problem_filter: { action: '管理问题过滤', running: '管理问题过滤', verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.keyword) ? a.keyword.map((k) => str(k)).join('、') : str(a?.keyword)].filter(Boolean).join(' · ') },
  manage_problem_white_list: { action: '管理问题白名单', running: '管理问题白名单', verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.entry) ? a.entry.map((k) => str(k)).join('、') : str(a?.entry)].filter(Boolean).join(' · ') },
  list_transl_cache: { action: '查看缓存清单', running: '查看缓存清单', verb: '', icon: 'archive', summary: () => '列出缓存文件' },
  read_transl_cache: { action: '读取缓存', running: '读取缓存', verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  search_input: { action: '搜索原文', running: '搜索原文', verb: '', icon: 'search-plus', summary: (a) => [str(a?.query), str(a?.filename), a?.context ? `±${a.context} 句上下文` : ''].filter(Boolean).join(' · ') },
  search_transl_cache: { action: '搜索缓存', running: '搜索缓存', verb: '', icon: 'search-plus', summary: (a) => [str(a?.query), a?.context ? `±${a.context} 句上下文` : ''].filter(Boolean).join(' · ') },
  patch_transl_cache: { action: '修改译文', running: '修改译文', verb: '', icon: 'pencil', summary: (a) => (Array.isArray(a?.patches) ? `${a.patches.length} 条` : str(a?.filename)) },
  delete_transl_cache: { action: '删除缓存', running: '删除缓存', verb: '', icon: 'trash', summary: (a) => [str(a?.filename), str(a?.indexes)].filter(Boolean).join(' · ') },
};

const DEFAULT_TOOL_META: ToolMeta = { action: '调用工具', running: '调用工具', verb: '', icon: 'tool', summary: () => '' };

/** 未收录进 TOOL_META 的工具：至少把原始工具名亮出来，不再只显示「调用工具」。 */
function toolMeta(name: string | undefined): ToolMeta {
  if (!name) return DEFAULT_TOOL_META;
  const meta = TOOL_META[name];
  if (meta) return meta;
  return { ...DEFAULT_TOOL_META, action: name, running: name };
}

function str(v: unknown): string {
  if (v === undefined || v === null) return '';
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/** wait 工具的参数摘要：把 seconds/minutes 归一成"等待 2 分钟"。
 *  带了 job_id（等某个任务，任务先结束就提前返回）时把这一点说清楚。 */
function waitSummary(args: Record<string, unknown> | undefined): string {
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const totalSeconds = num(args?.seconds) + num(args?.minutes) * 60;
  const reason = typeof args?.reason === 'string' ? args.reason.trim() : '';
  const jobId = typeof args?.job_id === 'string' ? args.job_id.trim() : '';
  if (totalSeconds <= 0) return reason;
  const duration = totalSeconds % 60 === 0 && totalSeconds >= 60
    ? `${totalSeconds / 60} 分钟`
    : `${totalSeconds} 秒`;
  const head = jobId
    ? `等任务 ${jobId.length > 8 ? `${jobId.slice(0, 6)}…` : jobId} 结束或 ${duration}`
    : duration;
  return reason ? `${head} · ${reason}` : head;
}

/** 倒计时显示：mm:ss，超过一小时用 h:mm:ss。 */
function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** token 数显示：1000 起用 K（1 位小数），如 128.4K / 1000.0K。 */
function formatTokenCount(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n < 1000) return String(Math.round(n));
  return `${(n / 1000).toFixed(1)}K`;
}

/** 已用上下文/上下文窗口的环形指示器（悬停显示百分比与具体 token 数）。
 *  达到压缩触发线（80%）后转为警示色。 */
function ContextMeter({ usage }: { usage: AgentContextUsage }) {
  const window = usage.window_tokens > 0 ? usage.window_tokens : 0;
  const used = Math.max(0, usage.used_tokens);
  const ratio = window > 0 ? Math.min(1, used / window) : 0;
  const percent = ratio * 100;
  const r = 7;
  const circumference = 2 * Math.PI * r;
  const detail = `${percent.toFixed(1)}% · ${formatTokenCount(used)} / ${formatTokenCount(window)} 上下文已使用`;
  return (
    <span
      className={`agent-context-meter${percent >= 80 ? ' is-warn' : ''}`}
      title={detail}
      aria-label={detail}
      role="img"
    >
      <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
        <circle className="agent-context-meter__track" cx="10" cy="10" r={r} fill="none" strokeWidth="2.5" />
        <circle
          className="agent-context-meter__value"
          cx="10"
          cy="10"
          r={r}
          fill="none"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={`${circumference * ratio} ${circumference}`}
          transform="rotate(-90 10 10)"
        />
      </svg>
    </span>
  );
}

function formatDuration(ms: number | undefined): string {
  if (!ms || ms < 1000) return `${ms || 0}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/* ── Session sidebar ──
   一个项目下可以有多个会话；这里负责新建、切换、删除。
   标题由后端生成：新建时是占位「新会话」，首条消息发出后变成这条消息。 */

/** 一个项目下的会话再多也只先渲染这么多条：首次打开的开销与"会话总数"脱钩。
 *  后端返回的列表仍是完整的（分组计数、状态灯都用全量），点「显示其余 N 个」
 *  纯本地展开、不再请求。 */
const SESSION_RENDER_LIMIT = 60;

function AgentSessionSidebar({
  sessionsByProject,
  projects,
  activeProject,
  activeSessionId,
  collapsed,
  disabled,
  activeRunning,
  unseenLights,
  onCreateBlank,
  onCreateInProject,
  onToggleProject,
  onSelectSession,
  onDeleteSession,
}: {
  sessionsByProject: Record<string, AgentSessionMeta[]>;
  projects: string[];
  activeProject: string;
  activeSessionId: string;
  collapsed: Record<string, boolean>;
  disabled: boolean;
  /** 当前活动会话自己的回合是否在跑（本地状态，比后端列表快一拍） */
  activeRunning: boolean;
  /** 跑完但还没被点开看过的会话：session_id -> done / failed */
  unseenLights: Record<string, 'done' | 'failed'>;
  onCreateBlank: () => void;
  onCreateInProject: (dir: string) => void;
  onToggleProject: (dir: string) => void;
  onSelectSession: (dir: string, sid: string) => void;
  onDeleteSession: (dir: string, session: AgentSessionMeta) => void;
}) {
  // 哪些项目分组已经点开过「显示其余 N 个」（纯本地状态，不涉及请求）
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>({});
  // 相对时间（刚刚 / N分钟前）要定时重算，否则页面静止时数字会一直停着不动
  const [, setTimeTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTimeTick((n) => n + 1), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <aside className="agent-sessions">
      <div className="agent-sessions__head">
        <span className="agent-sessions__title">会话</span>
        <button
          type="button"
          className="agent-sessions__new"
          onClick={onCreateBlank}
          disabled={disabled}
          title={disabled ? 'Agent 运行中，请先停止或等待' : '新建会话（选择新项目）'}
        >
          ＋
        </button>
      </div>
      <div className="agent-sessions__list">
        {projects.length === 0 ? (
          <div className="agent-sessions__empty">
            还没有项目
            <span>点 ＋ 新建，或在首页打开一个项目后再回到 Agent</span>
          </div>
        ) : (
          projects.map((dir) => {
            const raw = sessionsByProject[dir];
            const list = raw || [];
            // undefined = 这个项目还没拉过列表（非活动项目是展开时才按需拉的）
            const loaded = raw !== undefined;
            const isCollapsed = collapsed[dir] ?? dir !== activeProject;
            const isGroupActive = dir === activeProject;
            const shortDir = shortName(dir);
            // 只渲染前 N 条；但当前正在看的那个会话无论多老都要在列表里，
            // 否则侧边栏上看不出"你在哪"，它的状态灯也没地方挂。
            const visible = expandedProjects[dir] ? list : list.slice(0, SESSION_RENDER_LIMIT);
            if (!expandedProjects[dir] && activeSessionId && !visible.some((s) => s.session_id === activeSessionId)) {
              const active = list.find((s) => s.session_id === activeSessionId);
              if (active) visible.push(active);
            }
            return (
              <div
                key={dir}
                className={`agent-sessions__group${isCollapsed ? ' is-collapsed' : ''}${isGroupActive ? ' is-active' : ''}`}
              >
                <div className="agent-sessions__group-head">
                  <button
                    type="button"
                    className="agent-sessions__group-toggle"
                    onClick={() => onToggleProject(dir)}
                    title={dir}
                  >
                    <span className="agent-sessions__group-icon" aria-hidden>
                      <Icon name={isCollapsed ? 'folder' : 'folder-open'} />
                    </span>
                    <span className="agent-sessions__group-name">{shortDir}</span>
                    <span className="agent-sessions__group-count">
                      {list.length > 0 ? list.length : ''}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="agent-sessions__group-new"
                    onClick={(e) => {
                      e.stopPropagation();
                      onCreateInProject(dir);
                    }}
                    // 只有"当前正在跑的那个项目"要拦：在它下面新建会切走当前会话
                    // （并停掉正在跑的回合）。别的项目互不干扰——后端按 (项目, 会话)
                    // 各自独立运行，随时可以在它们下面新建会话、甚至同时各跑一个 Agent。
                    disabled={disabled && dir === activeProject}
                    title={
                      disabled && dir === activeProject
                        ? '该项目的 Agent 正在运行，请先停止或等待'
                        : `在「${shortDir}」下新建会话`
                    }
                    aria-label={`在 ${shortDir} 新建会话`}
                  >
                    ＋
                  </button>
                </div>
                <div className="agent-sessions__group-collapse">
                  <div className="agent-sessions__group-collapse-inner">
                    <div className="agent-sessions__group-list">
                      {list.length === 0 ? (
                        <div className="agent-sessions__group-empty">{loaded ? '暂无会话' : '加载中…'}</div>
                      ) : (
                        visible.map((s) => {
                          const isRowActive = isGroupActive && s.session_id === activeSessionId;
                          const isRowRunning = s.status === 'running' || (isRowActive && activeRunning);
                          // 灯：工作中蓝灯常亮；跑完但没被你点开看过亮绿灯/橙灯（橙=失败）；
                          // 正看着的那一行不亮灯（点开即熄灭，见 AgentPage 的 unseenLights）
                          const light = isRowRunning
                            ? 'running'
                            : isRowActive
                              ? ''
                              : unseenLights[s.session_id] || '';
                          return (
                            <div
                              key={s.session_id}
                              className={`agent-session-item${isRowActive ? ' is-active' : ''}`}
                            >
                              <button
                                type="button"
                                className="agent-session-item__main"
                                onClick={() => onSelectSession(dir, s.session_id)}
                                title={s.title}
                              >
                                <span className="agent-session-item__title">{s.title}</span>
                                <span className="agent-session-item__time">
                                  {formatSessionTime(s.updated_at)}
                                </span>
                              </button>
                              {light ? (
                                <span
                                  className={`agent-session-item__light is-${light}`}
                                  title={
                                    light === 'running'
                                      ? '正在运行'
                                      : light === 'failed'
                                        ? '已结束：出错'
                                        : '已结束'
                                  }
                                  aria-hidden
                                />
                              ) : null}
                              <button
                                type="button"
                                className="agent-session-item__delete"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onDeleteSession(dir, s);
                                }}
                                // 正在跑的那个会话不能删（灯亮着）：先停止再删
                                disabled={isRowRunning}
                                title={isRowRunning ? '正在运行，停止后才能删除' : '删除该会话'}
                                aria-label={`删除会话 ${s.title}`}
                              >
                                <Icon name="close" />
                              </button>
                            </div>
                          );
                        })
                      )}
                      {list.length > visible.length ? (
                        <button
                          type="button"
                          className="agent-sessions__show-more"
                          onClick={() => setExpandedProjects((prev) => ({ ...prev, [dir]: true }))}
                        >
                          显示其余 {list.length - visible.length} 个会话
                        </button>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

/** 会话时间改成相对时间：刚刚 / N分钟前 / N小时前 / N天前，超过一周退回日期。 */
function formatSessionTime(ts: number): string {
  if (!ts) return '';
  const then = new Date(ts * 1000);
  const time = then.getTime();
  if (Number.isNaN(time)) return '';
  const diffMs = Date.now() - time;
  if (diffMs < 60_000) return '刚刚'; // 含时钟偏差导致的「未来时间」
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}天前`;
  const showYear = then.getFullYear() !== new Date().getFullYear();
  const md = `${then.getMonth() + 1}/${then.getDate()}`;
  return showYear ? `${then.getFullYear()}/${md}` : md;
}

/* ── Main page ── */

export function AgentPage() {
  const navigate = useNavigate();

  // 已打开项目与历史合并的去重列表。响应式：监听 OPEN_PROJECTS_CHANGE_EVENT
  // （翻译器/别处打开或关闭项目时广播），让侧边栏分组与全局侧边栏保持同步。
  const mergeProjects = useCallback((): string[] => {
    const seen = new Set<string>();
    const list: string[] = [];
    for (const d of loadOpenProjects()) {
      if (!seen.has(d)) {
        seen.add(d);
        list.push(d);
      }
    }
    for (const h of readHistory()) {
      if (!seen.has(h.projectDir)) {
        seen.add(h.projectDir);
        list.push(h.projectDir);
      }
    }
    return list;
  }, []);
  const [projectOptions, setProjectOptions] = useState<string[]>(() => mergeProjects());
  useEffect(() => {
    const sync = () => setProjectOptions(mergeProjects());
    window.addEventListener(OPEN_PROJECTS_CHANGE_EVENT, sync);
    // 进入页面时也同步一次：可能在别处刚打开/关闭过项目
    sync();
    return () => window.removeEventListener(OPEN_PROJECTS_CHANGE_EVENT, sync);
  }, [mergeProjects]);

  // 「模型设置」里的 Agent 默认后端：没自己绑定过的会话都跟随它。默认变化时只改
  // 这个"默认值"，绑定过的会话各自跟自己的配置走，互不影响。
  const [defaultProfileName, setDefaultProfileName] = useState<string>(
    () => getAgentDefaultBackendProfile() || getBackendProfileNames()[0] || '',
  );
  useEffect(() => {
    const sync = (e: Event) => {
      const next = (e as CustomEvent<string>).detail || '';
      if (next) setDefaultProfileName(next);
    };
    window.addEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync as EventListener);
    const cur = getAgentDefaultBackendProfile();
    if (cur) setDefaultProfileName(cur);
    return () => window.removeEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync as EventListener);
  }, []);

  const [projectDir, setProjectDir] = useState<string>(() => projectOptions[0] || '');
  const [configFileName, setConfigFileName] = useState<string>(() =>
    projectOptions[0] ? readConfigFileName(projectOptions[0]) : 'config.yaml',
  );
  const [backendProfileNames] = useState<string[]>(() => getBackendProfileNames());
  const [goal, setGoal] = useState('');

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<string>('idle');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 后端配置/模型选择小菜单（点 composer 的 chip 打开）
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  // 项目 chip 的小菜单：只有"会话还没开始"时它才可点（选完项目、首条消息之前），
  // 那时换项目没成本；一旦开聊，项目就是这次会话的锚点，chip 退化成纯标签。
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  // 界面上的「发送中」乐观态：消息已发出但后端尚未确认
  const [sending, setSending] = useState(false);
  // 已用上下文/上下文窗口（composer 右下角指示器）：
  // 基线来自会话状态快照，运行中由 context_usage 事件实时更新。
  const [contextUsage, setContextUsage] = useState<AgentContextUsage | null>(null);
  // 排队中的消息（运行中发的）：显示在 composer 上方的队列面板里，不进聊天框。
  // 后端是权威来源：状态快照 snap.queued + 实时 queue 事件都整份推送。
  const [queued, setQueued] = useState<QueuedMessage[]>([]);
  // 正在就地编辑的队列条目（id + 草稿文本）
  const [editingQueued, setEditingQueued] = useState<{ id: string; text: string } | null>(null);
  // 会话列表按项目分组：projectDir -> 该项目的会话列表
  const [sessionsByProject, setSessionsByProject] = useState<Record<string, AgentSessionMeta[]>>({});
  // 侧边栏每个项目分组的折叠态（默认当前活动项目展开，其余折叠）
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  // 侧边栏状态灯里"跑完了但还没点开看过"的那些：session_id -> done（绿）/ failed（橙）。
  // 工作中的会话不在这里——它的蓝灯直接由后端 status（或本会话的 running）推出来。
  // 用户点开该会话（激活）就把它清掉，灯随之消失。
  const [unseenLights, setUnseenLights] = useState<Record<string, 'done' | 'failed'>>({});
  // 上一次看到的各会话状态：用来发现"刚才还在跑、现在不跑了"的那个收尾瞬间
  const prevSessionStatusRef = useRef<Record<string, string>>({});
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const first = projectOptions[0];
    return first ? loadActiveSessionId(first) : '';
  });
  // 每会话绑定的后端配置（'' 键 = 新会话还没发出第一条消息时选的草稿）
  const [sessionBackends, setSessionBackends] = useState<SessionBackendMap>(() => loadSessionBackends());
  const setSessionBackend = useCallback(
    (project: string, sessionId: string, name: string | null) => {
      setSessionBackends((prev) => {
        const bySession = { ...(prev[project] || {}) };
        if (name === null) delete bySession[sessionId];
        else bySession[sessionId] = name;
        const next = { ...prev, [project]: bySession };
        saveSessionBackends(next);
        return next;
      });
    },
    [],
  );
  // 当前会话生效的后端配置：自己绑定过用绑定的，空态用草稿，否则跟随 Agent 默认
  const boundBackendProfile = activeSessionId
    ? sessionBackends[projectDir]?.[activeSessionId] || ''
    : sessionBackends[projectDir]?.[''] || '';
  const backendProfileName = boundBackendProfile || defaultProfileName || getBackendProfileNames()[0] || '';
  // 本地已乐观追加的 user_message 的临时 id 集合，SSE 回放时据此去重，
  // 避免同一条消息渲染两次（发送时本地先显示，后端确认后回放同一条）
  const localMsgIdsRef = useRef<Set<string>>(new Set());
  // 发送流程自身的会话过渡：首条消息 create→start→subscribe 期间
  // activeSessionId 从空变到新会话，会触发会话切换 effect；它的恢复/对账
  // 逻辑会清掉乐观气泡并把 running 打回 false（fetch 先于 startAgent 完成，
  // 拿到 idle），界面就像没在跑一样。用 ref 标记该窗口让 effect 跳过；
  // 用户在此窗口内手动切到别的会话则不拦（id 不匹配，正常恢复）。
  const sendingRef = useRef(false);
  const sendTransitionRef = useRef<string | null>(null);

  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  // 与 stickToBottomRef 同义，但"回到最新"按钮要随滚动出现/消失，得能触发渲染
  const [atBottom, setAtBottom] = useState(true);
  const startRef = useRef(0);
  // 本地已见的最大事件 step（SSE 续订的 after_step 起点 + 兜底去重）。
  // 状态快照对账/持久化恢复时同步更新。
  const lastStepRef = useRef(0);
  // 是否已在后端建立会话（首条消息 startAgent 成功后置 true；reset 清空）。
  // 不能用 events.length 判断：乐观追加后它立即 >0，但会话可能还没建好。
  const hasBackendSessionRef = useRef(false);
  // 从 hero 选择项目 / 顶部＋新建空会话：切项目后不应自动加载该项目上次
  // 记忆的会话，而要保持空态等用户发消息创建新会话。置位后项目 effect
  // 会把 activeSessionId 清空而非取 remembered，随后清掉一次性标志。
  const skipRememberedSessionRef = useRef(false);
  // 后端配置小菜单的容器：点外面要能关掉
  const profilePickerRef = useRef<HTMLDivElement | null>(null);
  // 项目 chip 小菜单的容器：同上
  const projectPickerRef = useRef<HTMLDivElement | null>(null);
  // 当前激活的会话 id，供回调读取（避免闭包读到旧值）
  const activeSessionRef = useRef('');
  const statusSyncVersionRef = useRef(0);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
    // 后端配置绑定在会话上：切会话时 chip 自动切到该会话绑定的那份（没有就跟随
    // Agent 默认），不需要也没有"重置"动作。
    // 点开（激活）这个会话 → 它的状态灯熄灭（"跑完了，等你回来看"的信号已经送达）
    setUnseenLights((prev) => {
      if (!(activeSessionId in prev)) return prev;
      const next = { ...prev };
      delete next[activeSessionId];
      return next;
    });
  }, [activeSessionId]);
  // 当前活动项目，供回调读取（refreshSessions 判断是否接管 activeSessionId）
  const effectiveProjectRef = useRef(projectDir);
  useEffect(() => {
    effectiveProjectRef.current = projectDir;
  }, [projectDir]);

  // 后端配置小菜单：点外部 / Esc 关闭；Agent 跑起来后也收起（此时不能切配置）
  useEffect(() => {
    if (!profileMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!profilePickerRef.current?.contains(e.target as Node)) setProfileMenuOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setProfileMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [profileMenuOpen]);

  // 项目 chip 的小菜单：同样点外部 / Esc 关闭
  useEffect(() => {
    if (!projectMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!projectPickerRef.current?.contains(e.target as Node)) setProjectMenuOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setProjectMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [projectMenuOpen]);

  useEffect(() => {
    if (running) setProfileMenuOpen(false);
  }, [running]);

  const effectiveProject = projectDir;

  /** 拉取某项目的会话列表并写进按项目分组的 map（不动其他项目的会话）。
   *  preferredId 命中则切到该会话；allowAutoPick 为真（默认）且列表非空时取第一个，
   *  否则保持现状（hero 选项目等空态场景不自动加载历史会话）。 */
  const refreshSessions = useCallback(
    async (dir: string, preferredId?: string, allowAutoPick = true): Promise<AgentSessionMeta[]> => {
      try {
        const list = await listAgentSessions(dir);
        setSessionsByProject((prev) => ({ ...prev, [dir]: list }));
        // 状态灯：和后端状态比对，找出"刚跑完"的会话（上一次 running、这次不是了）。
        // 你正看着它（当前项目 + 当前会话）就当作已读，不亮灯。
        const prevStatuses = prevSessionStatusRef.current;
        const nextStatuses: Record<string, string> = {};
        const justFinished: Record<string, 'done' | 'failed'> = {};
        for (const s of list) {
          const status = s.status || '';
          nextStatuses[s.session_id] = status;
          if (prevStatuses[s.session_id] === 'running' && status !== 'running') {
            justFinished[s.session_id] = status === 'failed' ? 'failed' : 'done';
          }
        }
        // 基线按会话 id 合并、而不是整份替换：现在会按需拉单个项目（展开时），
        // 整份替换的话，拉 B 项目会把 A 项目的基线抹掉，A 那边"刚跑完"的灯就点不亮。
        prevSessionStatusRef.current = { ...prevStatuses, ...nextStatuses };
        if (Object.keys(justFinished).length) {
          setUnseenLights((prev) => {
            const merged = { ...prev };
            for (const [sid, light] of Object.entries(justFinished)) {
              if (dir === effectiveProjectRef.current && sid === activeSessionRef.current) delete merged[sid];
              else merged[sid] = light;
            }
            return merged;
          });
        }
        // 仅当 dir 恰好是当前活动项目时才接管 activeSessionId 选择，
        // 否则（点别的项目的 + / 删了另一项目的会话）不强改主区。
        if (dir === effectiveProjectRef.current) {
          const want = preferredId || activeSessionRef.current;
          if (want && list.some((s) => s.session_id === want)) {
            setActiveSessionId(want);
            activeSessionRef.current = want;
            saveActiveSessionId(dir, want);
          } else if (allowAutoPick && list.length) {
            setActiveSessionId(list[0].session_id);
            activeSessionRef.current = list[0].session_id;
            saveActiveSessionId(dir, list[0].session_id);
          }
        }
        return list;
      } catch {
        // 后端不可达时保留现有列表
        return [];
      }
    },
    [],
  );

  /* 有会话在跑时轻量轮询会话列表：侧边栏的蓝灯要跟着后端的实际状态走——切到别的会话
     （或别的项目）后，原来那个会话跑完了也得收到，灯才能从蓝转绿/橙。没有会话在跑
     就停掉轮询，不做无谓请求。 */
  useEffect(() => {
    const dirs = Object.keys(sessionsByProject).filter((dir) =>
      (sessionsByProject[dir] || []).some((s) => s.status === 'running'),
    );
    // 当前活动会话自己的 running 也要算上：它刚开跑、列表里可能还没反映出来
    if (running && effectiveProject && !dirs.includes(effectiveProject)) dirs.push(effectiveProject);
    if (!dirs.length) return;
    const timer = window.setInterval(() => {
      for (const dir of dirs) void refreshSessions(dir, undefined, false);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [running, sessionsByProject, effectiveProject, refreshSessions]);

  /* Project change: adopt the remembered session for this project, load its
     session list into the grouped map (without clobbering other projects),
     and accordion-collapse so only the active project is expanded. The actual
     transcript load happens in the session effect below (keyed on activeSessionId). */
  useEffect(() => {
    localMsgIdsRef.current.clear();
    if (!effectiveProject) {
      hasBackendSessionRef.current = false;
      activeSessionRef.current = '';
      setActiveSessionId('');
      setEvents([]);
      setRunning(false);
      setStatus('idle');
      return;
    }
    const skip = skipRememberedSessionRef.current;
    skipRememberedSessionRef.current = false;
    // hero 选项目 / 顶部＋：保持空态等用户发消息创建新会话，不取历史会话，
    // 也不让 refreshSessions 自动切到该项目最近的会话
    const remembered = skip ? '' : loadActiveSessionId(effectiveProject);
    setActiveSessionId(remembered);
    activeSessionRef.current = remembered;
    // accordion：展开当前项目、收起其他（用户仍可手动再展开别的）
    setCollapsedProjects(() => {
      const next: Record<string, boolean> = {};
      for (const d of projectOptions) next[d] = d !== effectiveProject;
      return next;
    });
    void refreshSessions(effectiveProject, remembered, !skip);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveProject]);

  /* Session change: restore this session's transcript, then reconcile with the
     backend (which may have a run in flight, or history restored from disk). */
  useEffect(() => {
    let cancelled = false;
    const syncVersion = statusSyncVersionRef.current;
    // 发送流程自身触发的会话过渡（create→start→subscribe）不按"用户切会话"
    // 恢复：此时 startAgent 还在路上，fetch 会拿到 idle 把 running 打回去。
    if (sendingRef.current && activeSessionId === sendTransitionRef.current) {
      return;
    }
    abortRef.current?.();
    abortRef.current = null;
    localMsgIdsRef.current.clear();
    if (!effectiveProject || !activeSessionId) {
      // 项目下还没有任何会话：显示空态，等用户发第一条消息
      hasBackendSessionRef.current = false;
      setEvents([]);
      setRunning(false);
      setStatus('idle');
      setContextUsage(null);
      setQueued([]);
      return;
    }
    // 先用 localStorage 的缓存立刻渲染，避免切换时闪白
    const persisted = loadSession(effectiveProject, activeSessionId);
    setEvents(persisted?.events || []);
    setStatus(persisted?.status || 'idle');
    startRef.current = persisted?.startedAt || 0;
    lastStepRef.current = maxStep(persisted?.events || []);
    hasBackendSessionRef.current = Boolean(persisted?.events.length);

    fetchAgentStatus(effectiveProject, activeSessionId)
      .then(async (snap) => {
        if (cancelled || syncVersion !== statusSyncVersionRef.current) return;
        if (sendingRef.current && activeSessionId === sendTransitionRef.current) return;
        // 转录的权威来源是后端会话日志（回放已提交事件，不受内存事件窗口与本地
        // 缓存条数限制）；拿不到（旧后端/网络抖动）就退回本地缓存。
        const transcript = await fetchAgentTranscript(effectiveProject, activeSessionId).catch(() => null);
        if (cancelled || syncVersion !== statusSyncVersionRef.current) return;
        const snapEvents = snap.events || [];
        const base = transcript && transcript.length ? transcript : persisted?.events || [];
        if (!base.length) {
          // 新建空会话：后端空快照应清掉可能残留的本地缓存，避免把别的会话的
          // 内容糊在新建会话上。
          setEvents([]);
          lastStepRef.current = 0;
          hasBackendSessionRef.current = false;
        } else {
          // 日志与快照按 step 合并（同 step 以快照为准，它能恢复出首条 user_message）；
          // 再把"正在生成的那半条消息"接在尾部（进行中的助手消息）。
          const mergedEvents = mergeTranscriptEvents(base, snapEvents);
          setEvents(seedStreaming(mergedEvents, snap.streaming));
          // 续订游标要跳过快照里已经包含的增量，否则会把同一段增量补第二遍
          lastStepRef.current = Math.max(maxStep(mergedEvents), snap.streaming?.step ?? 0);
          hasBackendSessionRef.current = true;
        }
        const snapRunning = snap.status === 'running';
        setStatus(snap.status);
        setRunning(snapRunning);
        // 上下文用量以快照为准（切会话/刷新后不用等下一次 LLM 请求）
        setContextUsage(snap.context || null);
        // 队列面板同理：切会话/刷新后直接把当前队列摆出来
        setQueued(snap.queued || []);
        if (snapRunning) subscribeStream(effectiveProject, activeSessionId);
      })
      .catch(() => {
        // Backend not ready — keep the persisted view.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveProject, activeSessionId]);

  useEffect(() => {
    if (projectDir) setConfigFileName(readConfigFileName(projectDir));
  }, [projectDir]);

  // Persist transcript whenever it settles.
  useEffect(() => {
    if (!effectiveProject || !activeSessionId || !events.length) return;
    saveSession({
      projectDir: effectiveProject,
      sessionId: activeSessionId,
      events,
      status,
      goal: '',
      startedAt: startRef.current,
      finishedAt: status === 'running' ? 0 : Date.now(),
    });
  }, [events, status, effectiveProject, activeSessionId]);

  // Follow the tail unless the user scrolled away.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
    setAtBottom(true);
  }, [events]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const stick = distance < 80;
    stickToBottomRef.current = stick;
    // 只在状态真的翻转时 setState，滚动期间不会每帧触发渲染
    setAtBottom((prev) => (prev === stick ? prev : stick));
  }, []);

  /** 回到转录最底部（并恢复"跟随新消息"）。 */
  const handleJumpToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    setAtBottom(true);
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduced ? 'auto' : 'smooth' });
  }, []);

  useEffect(() => {
    return () => {
      abortRef.current?.();
      abortRef.current = null;
    };
  }, []);

  const subscribeStream = useCallback((dir: string, sessionId?: string) => {
    abortRef.current?.();
    // 续订时从本地已见的最大 step 之后开始拉，避免后端重放旧回合的事件
    const afterStep = lastStepRef.current;
    abortRef.current = subscribeAgentStream(
      dir,
      (ev) => {
        if (ev.type === 'close') {
          // 后端明确关流才收尾（可能整段回放完才到）
          setRunning(false);
          return;
        }
        if (ev.type === 'status') {
          if (ev.status) setStatus(ev.status);
          // 订阅到一个已在运行的会话时，首帧快照里就带着上下文用量
          if (ev.context) setContextUsage(ev.context);
          // 后端说在跑就一定给停止按钮（自愈：万一前面某条事件把运行态打回去了，
          // 这里能把界面拉回来）
          if (ev.status === 'running') setRunning(true);
          // status 快照绝不主动 abort：订阅时回合可能已结束（首帧即终态），
          // 但事件还在流里没发完，掐流会吞掉全部内容。流的关闭交给 close 帧
          // 或 finish/stopped/error 事件。
          if (ev.status && isTerminal(ev.status)) {
            setRunning(false);
          }
          return;
        }
        // 队列快照：队列面板的权威数据（发送/被消费/删除/编辑都会推一份整表）。
        // 是控制事件，和 status 一样放在去重之前处理，保证不会漏。
        if (ev.type === 'queue') {
          setQueued(ev.queued || []);
          return;
        }
        // 写类工具可能改了缓存文件（结果里带 filename / deleted_files）：清掉引用卡片的
        // 取数记忆并让它重拉。不做的话，回复里现渲染的 $transl_cache 卡片、以及已经挂在
        // 屏幕上的旧卡片，拿到的都是改动前那份快照——模型明明改了，界面还是旧译文。
        if (ev.type === 'tool_result' && ev.ok) {
          invalidateCacheFilesForToolResult(ev.result);
        }
        // 兜底去重：SSE 重放/竞态下 step 已见过的事件直接丢弃
        // （本地乐观的 user_message 用 step=-1，不参与该判断）
        if (typeof ev.step === 'number' && ev.step >= 0) {
          if (ev.step <= lastStepRef.current) return;
          lastStepRef.current = ev.step;
        }
        // 上下文用量：只更新指示器，不进对话转录（否则每次请求都会在
        // 界面上多出一条无意义记录，刷新后由 status 快照兜底）
        if (ev.type === 'context_usage') {
          if (ev.context) setContextUsage(ev.context);
          return;
        }
        if (ev.type === 'user_message') {
          // 后端已收录这条消息：用真实事件替换本地乐观的占位（step=-1），
          // 保持顺序正确且刷新后可完整回放
          const localId = `local:${ev.message}`;
          if (localMsgIdsRef.current.has(localId)) {
            localMsgIdsRef.current.delete(localId);
            const text = ev.message;
            setEvents((prev) => {
              const idx = prev.findIndex(
                (it) => it.type === 'user_message' && it.step === -1 && it.message === text,
              );
              if (idx === -1) return [...prev, ev];
              const next = [...prev];
              next[idx] = ev;
              return next;
            });
            return;
          }
        }
        setEvents((prev) => [...prev, ev]);
        if (ev.type === 'finish' || ev.type === 'error' || ev.type === 'stopped') {
          // 后端若已安排好 followup 回合（点「立即」发送、或滞留插话转新回合），
          // 下一步马上又在跑：这里绝不能把运行态打回 false——否则停止按钮会消失、
          // 顶栏还会因为 running=false 显示成"空闲"，而后端其实在跑。
          if (!ev.followup) setRunning(false);
          // 不主动 abort：followup 回合的后续事件会走同一条流；
          // 流的关闭由后端 close 帧决定。
        }
      },
      (err) => {
        setError(normalizeError(err, 'Agent 事件流中断'));
        setRunning(false);
      },
      afterStep,
      sessionId,
    );
  }, []);

  const handleSend = useCallback(async () => {
    const text = goal.trim();
    if (!text) return;
    setError(null);
    // 发出去就等于会话开始：项目 chip 马上要变成不可点的纯标签，菜单顺手收掉
    setProjectMenuOpen(false);
    if (!effectiveProject) {
      setError('请先选择一个项目');
      return;
    }
    const profile = getBackendProfile(backendProfileName);
    if (!profile) {
      setError('请先选择一个翻译后端配置（并在「模型设置」页填写 token/模型）');
      setProfileMenuOpen(true);
      return;
    }

    // 运行中发的消息不进聊天框：后端把它放进队列，显示在 composer 上方的队列
    // 面板里（想马上发就点「立即」）。空闲时才是普通对话气泡——本地立刻显示，
    // 不等后端确认。
    if (!running) {
      const localId = `local:${text}`;
      localMsgIdsRef.current.add(localId);
      setEvents((prev) => [
        ...prev,
        { type: 'user_message', step: -1, message: text },
      ]);
    }
    setGoal('');
    setSending(true);
    sendingRef.current = true;
    sendTransitionRef.current = activeSessionRef.current;
    statusSyncVersionRef.current += 1;
    startRef.current = Date.now();
    setStatus('running');
    setRunning(true);
    try {
      // 目标会话：优先用当前选中会话；没有就先建一个空会话再往里发消息。
      // 走 create → message 两段式，让侧边栏立刻能看到这个新会话。
      let sid = activeSessionRef.current;
      if (!sid) {
        const created = await createAgentSession(effectiveProject);
        sid = created.session_id;
        // 会话切换 effect 已在本 await 期间被触发（activeSessionId 仍为空，
        // 走的是"清空"分支）；标记过渡窗口，随后的 setActiveSessionId 不再
        // 触发恢复逻辑，running 保持 true。
        sendTransitionRef.current = sid;
        activeSessionRef.current = sid;
        setActiveSessionId(sid);
        saveActiveSessionId(effectiveProject, sid);
        // 空态时选的后端是「新会话草稿」：会话建好了，把它迁移绑到这个 sid 上
        setSessionBackends((prev) => {
          const bySession = { ...(prev[effectiveProject] || {}) };
          const draft = bySession[''];
          if (draft === undefined) return prev;
          delete bySession[''];
          bySession[sid] = draft;
          const next = { ...prev, [effectiveProject]: bySession };
          saveSessionBackends(next);
          return next;
        });
      }
      // 后端上下文：配置名只存在前端 localStorage，后端拿不到；而「了解项目」要
      // 如实报出"本会话在用的后端"和"翻译任务会用的后端"，所以随消息一起送过去。
      // token 不落盘，重启后继续历史会话也必须重新提供本会话的配置内容。
      const backendContext = {
        ...(backendProfileName ? { backend_profile_name: backendProfileName } : {}),
        backend_profile_data: profile,
        ...getAgentTranslatorBackendContext(effectiveProject),
        // 权限模式同理只存在前端，随请求送过去（后端每次工具调用前按它判定）
        permission_mode: permissionMode,
      };
      if (!hasBackendSessionRef.current) {
        // 空会话：第一条消息启动首个回合
          const snap = await startAgent({
            project_dir: effectiveProject,
            config_file_name: configFileName || 'config.yaml',
            goal: text,
          session_id: sid,
          ...backendContext,
        });
        hasBackendSessionRef.current = true;
        if (snap.session_id) {
          sid = snap.session_id;
          sendTransitionRef.current = sid;
          activeSessionRef.current = sid;
          setActiveSessionId(sid);
          saveActiveSessionId(effectiveProject, sid);
        }
      } else {
        // 已有会话：运行中→进队列（面板显示）；已结束→同会话继续下一回合。
        // 返回值就是最新状态快照，队列面板据此立刻更新。
        const snap = await sendAgentMessage(effectiveProject, text, sid, backendContext);
        setQueued(snap.queued || []);
      }
      void refreshSessions(effectiveProject, sid);
      subscribeStream(effectiveProject, sid);
    } catch (err) {
      setError(normalizeError(err, '发送失败'));
      setRunning(false);
      setStatus('failed');
    } finally {
      setSending(false);
      sendingRef.current = false;
      statusSyncVersionRef.current += 1;
      sendTransitionRef.current = null;
    }
  }, [
    effectiveProject,
    backendProfileName,
    configFileName,
    goal,
    running,
    subscribeStream,
    refreshSessions,
  ]);

  /** 队列面板「立即」：打断当前回合，马上把这条发出去。 */
  const handleQueuedSendNow = useCallback(
    async (id: string) => {
      if (!effectiveProject) return;
      try {
        const snap = await sendAgentQueuedNow(
          effectiveProject,
          id,
          activeSessionRef.current || undefined,
        );
        setQueued(snap.queued || []);
        setEditingQueued(null);
        // 「立即」= 打断当前回合 + 马上发这条。打断那一瞬间后端可能正好读成
        // stopped（回合刚收尾、followup 还没接上），所以这里只往"运行中"推，
        // 绝不打回停止——否则停止按钮会消失而 Agent 其实还在跑。
        setStatus('running');
        setRunning(true);
        // 立即发送 = 打断再重启回合。流通常没断（后端收尾后立刻又 running），
        // 但万一已经关了，这里补一次订阅，别让新回合的事件没人收。
        if (!abortRef.current && snap.status === 'running') {
          subscribeStream(effectiveProject, activeSessionRef.current || undefined);
        }
      } catch (err) {
        setError(normalizeError(err, '立即发送失败'));
      }
    },
    [effectiveProject, subscribeStream],
  );

  /** 队列面板「删除」：这条不发了。 */
  const handleQueuedDelete = useCallback(
    async (id: string) => {
      if (!effectiveProject) return;
      try {
        const snap = await deleteAgentQueued(
          effectiveProject,
          id,
          activeSessionRef.current || undefined,
        );
        setQueued(snap.queued || []);
        if (editingQueued?.id === id) setEditingQueued(null);
      } catch (err) {
        setError(normalizeError(err, '删除排队消息失败'));
      }
    },
    [effectiveProject, editingQueued],
  );

  /** 队列面板「保存」：提交就地编辑。 */
  const handleQueuedSaveEdit = useCallback(async () => {
    if (!effectiveProject || !editingQueued) return;
    const text = editingQueued.text.trim();
    if (!text) return;
    try {
      const snap = await updateAgentQueued(
        effectiveProject,
        editingQueued.id,
        text,
        activeSessionRef.current || undefined,
      );
      setQueued(snap.queued || []);
      setEditingQueued(null);
    } catch (err) {
      setError(normalizeError(err, '修改排队消息失败'));
    }
  }, [effectiveProject, editingQueued]);

  const handleStop = useCallback(async () => {
    if (!effectiveProject) return;
    try {
      await stopAgent(effectiveProject, activeSessionRef.current || undefined);
      // 先给出即时反馈；收尾事件随后由流补上。
      setStatus('stopped');
      setRunning(false);
      // 这里**不能**掐断 SSE：收尾事件（队列快照、stopped、close）还在流里，
      // 而流是 0.5s 轮询的——一掐就全丢了。后端收尾后会自己发 close。
      // 点了「立即」的排队消息也一样：它要等回合收尾才发 user_message。
    } catch (err) {
      setError(normalizeError(err, '停止 Agent 失败'));
    }
  }, [effectiveProject]);

  const handleOpenProject = useCallback(async () => {
    // 项目选择集中在主区 hero：任何时候都允许用"打开项目"选/换一个项目。
    try {
      const selected = await openDialog({ directory: true, multiple: false });
      if (typeof selected === 'string' && selected) {
        const cfg = readConfigFileName(selected);
        // 经 hero"打开项目"选的项目，保持空态等发消息建新会话，不取历史会话
        skipRememberedSessionRef.current = true;
        setProjectDir(selected);
        setConfigFileName(cfg);
        setGoal('');
        // 同步进翻译器的"已打开项目"列表（写盘 + 广播），让全局侧边栏
        // 和 Agent 自己的侧边栏分组都出现这个项目。
        addOpenProject(selected, cfg);
      }
    } catch {
      // User cancelled.
    }
  }, []);

  /** 从 hero 的"已打开项目"列表里直接选中一个项目开始。幂等：addOpenProject
   *  保证该项目在翻译器已打开列表里。选中后保持空态等用户发消息创建新会话，
   *  不自动加载该项目上次的历史会话。 */
  const chooseProject = useCallback((dir: string) => {
    if (!dir) return;
    const cfg = readConfigFileName(dir);
    skipRememberedSessionRef.current = true;
    setProjectDir(dir);
    setConfigFileName(cfg);
    setGoal('');
    addOpenProject(dir, cfg);
  }, []);

  /** 会话开始前从 composer 的 chip 换项目：与 chooseProject 同一套，但**不动已输入的指令**。
   *  这个动作发生在"项目已选、还没开聊"的窗口里，用户很可能已经把任务描述写好了——
   *  换个项目接着用同一条指令是常态，清掉反而要重打。 */
  const switchProjectBeforeSession = useCallback((dir: string) => {
    if (!dir) return;
    const cfg = readConfigFileName(dir);
    skipRememberedSessionRef.current = true;
    setProjectDir(dir);
    setConfigFileName(cfg);
    addOpenProject(dir, cfg);
  }, []);

  /** 顶部 ＋：新建"未打开项目"的会话 —— 清空当前项目选择，主区回空态，
   *  让用户重新选/打开一个项目再发消息。侧边栏的其他项目会话分组保留显示，
   *  不会被清掉（不再清空 sessionsByProject）。 */
  const handleCreateBlankSession = useCallback(async () => {
    if (running) await handleStop();
    abortRef.current?.();
    abortRef.current = null;
    localMsgIdsRef.current.clear();
    hasBackendSessionRef.current = false;
    lastStepRef.current = 0;
    activeSessionRef.current = '';
    setActiveSessionId('');
    setEvents([]);
    setStatus('idle');
    setRunning(false);
    setError(null);
    // 只清当前主区项目/目标/活动会话；不动 sessionsByProject，侧边栏保留历史
    setProjectDir('');
    setGoal('');
  }, [running, handleStop]);

  /** 项目分组行 ＋：在指定项目下新建一个会话。
   *  - 若 dir 是当前活动项目 → 切到新会话（主区跟着切）。
   *  - 若 dir 是别的项目 → 只在该分组列表里增一行，主区不变（用户点该会话才切过去）。 */
  const handleCreateSessionInProject = useCallback(
    async (dir: string) => {
      if (!dir) return;
      try {
        const created = await createAgentSession(dir);
        setSessionsByProject((prev) => ({
          ...prev,
          [dir]: [created, ...(prev[dir] || [])],
        }));
        // 确保该分组展开可见
        setCollapsedProjects((prev) => ({ ...prev, [dir]: false }));
        if (dir === effectiveProjectRef.current) {
          // 当前项目：切到新会话，主区随之加载（空会话 → 等首条消息）
          if (running) await handleStop();
          abortRef.current?.();
          abortRef.current = null;
          activeSessionRef.current = created.session_id;
          setActiveSessionId(created.session_id);
          saveActiveSessionId(dir, created.session_id);
          setEvents([]);
          setStatus('idle');
          setRunning(false);
          setError(null);
          hasBackendSessionRef.current = false;
          lastStepRef.current = 0;
        } else {
          // 非活动项目：上面只是本地插了一条，而这个项目的列表可能根本没拉过
          // （懒加载下是展开才拉）。补一次完整列表，免得那一列只剩刚建的会话。
          void refreshSessions(dir, undefined, false);
        }
      } catch (err) {
        setError(normalizeError(err, '新建会话失败'));
      }
    },
    [running, handleStop, refreshSessions],
  );

  /** 折叠/展开某项目分组（手风琴外的自由切换：点击只翻转这一个）。
   *
   *  展开时按需拉一次该项目的会话列表（懒加载）：只有活动项目会在打开页面时拉，
   *  其余项目等到真正展开才请求——会话多的机器上，这一步能省掉大部分首屏请求。
   *
   *  判定要和渲染用同一套默认值（`collapsed[dir] ?? dir !== effectiveProject`）：
   *  直接写 `!prev[dir]` 的话，没手动点过的非活动项目本来就是"默认收起"，第一次点
   *  它只是往 map 里塞了个 true，看起来像点了没反应，得点两次才展开。 */
  const handleToggleProject = useCallback((dir: string) => {
    const isCollapsed = collapsedProjects[dir] ?? dir !== effectiveProject;
    if (isCollapsed && sessionsByProject[dir] === undefined) {
      // allowAutoPick=false：只补列表，不因为"这个项目有会话"就把主区切过去
      void refreshSessions(dir, undefined, false);
    }
    setCollapsedProjects((prev) => ({ ...prev, [dir]: !isCollapsed }));
  }, [collapsedProjects, effectiveProject, sessionsByProject, refreshSessions]);

  /** 选中某项目下的某会话：停 SSE、清视图，切项目+会话；转录由 session effect 加载。 */
  const handleSelectSession = useCallback(
    (dir: string, sessionId: string) => {
      if (sessionId === activeSessionRef.current && dir === effectiveProjectRef.current) return;
      abortRef.current?.();
      abortRef.current = null;
      setEvents([]);
      setError(null);
      activeSessionRef.current = sessionId;
      setActiveSessionId(sessionId);
      saveActiveSessionId(dir, sessionId);
      if (dir !== effectiveProjectRef.current) {
        // 切到别的项目：项目 effect 会触发加载该项目的会话列表，
        // session effect 会加载该会话转录。展开目标项目分组。
        setConfigFileName(readConfigFileName(dir));
        setCollapsedProjects((prev) => ({ ...prev, [dir]: false }));
        setProjectDir(dir);
      }
    },
    [],
  );

  const handleDeleteSession = useCallback(
    async (dir: string, session: AgentSessionMeta) => {
      if (!window.confirm(`删除会话「${session.title}」？该会话的对话记录会被一并删除。`)) return;
      try {
        await deleteAgentSession(dir, session.session_id);
      } catch (err) {
        setError(normalizeError(err, '删除会话失败'));
        return;
      }
      const prevList = sessionsByProject[dir] || [];
      const remaining = prevList.filter((s) => s.session_id !== session.session_id);
      setSessionsByProject((prev) => ({ ...prev, [dir]: remaining }));
      try {
        localStorage.removeItem(sessionsKey(dir, session.session_id));
      } catch {
        // ignore
      }
      // 删的是当前主区的活动会话 → 切到该分组剩下的第一个，没有则回空态
      if (dir === effectiveProjectRef.current && session.session_id === activeSessionRef.current) {
        abortRef.current?.();
        abortRef.current = null;
        const next = remaining[0]?.session_id || '';
        setActiveSessionId(next);
        activeSessionRef.current = next;
        saveActiveSessionId(dir, next);
        setEvents([]);
        setStatus('idle');
        hasBackendSessionRef.current = false;
        lastStepRef.current = 0;
      }
    },
    [sessionsByProject],
  );

  const handleClear = useCallback(async () => {
    if (!effectiveProject || !activeSessionRef.current) return;
    try {
      // 清空 = 重置当前会话：停掉运行中的回合并丢弃后端历史
      await resetAgent(effectiveProject, activeSessionRef.current);
    } catch {
      // 后端不可达也要清本地视图
    }
    setEvents([]);
    setStatus('idle');
    setError(null);
    setContextUsage(null);
    setQueued([]);
    setEditingQueued(null);
    localMsgIdsRef.current.clear();
    hasBackendSessionRef.current = false;
    lastStepRef.current = 0;
    try {
      localStorage.removeItem(sessionsKey(effectiveProject, activeSessionRef.current));
    } catch {
      // ignore
    }
    void refreshSessions(effectiveProject, activeSessionRef.current);
  }, [effectiveProject, refreshSessions]);

  const timeline = useMemo(() => buildTimeline(events), [events]);
  const hasSession = events.length > 0;
  const canSend = Boolean(projectDir) && Boolean(backendProfileName) && goal.trim().length > 0 && !sending;
  // 正在等用户回答的 ask_user：这条工具调用**还没有结果**，说明后端那个工具
  // 正阻塞着等这一下（结果一到就说明答过了/被跳过了）。从后往前找最新的那条。
  const pendingAsk = useMemo(() => {
    for (let gi = timeline.length - 1; gi >= 0; gi -= 1) {
      const group = timeline[gi];
      if (group.type !== 'activity') continue;
      for (let i = group.items.length - 1; i >= 0; i -= 1) {
        const it = group.items[i];
        if (it.kind === 'tool' && it.name === 'ask_user' && it.result === undefined && it.error === undefined) {
          return it;
        }
      }
    }
    return null;
  }, [timeline]);
  const [askSubmitting, setAskSubmitting] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  // 提交成功后先把卡片收起来（工具结果马上就到，避免卡片闪一下再消失）
  const [answeredAskId, setAnsweredAskId] = useState('');
  const pendingAskIdRef = useRef('');
  const askId = pendingAsk?.id || '';
  useEffect(() => {
    pendingAskIdRef.current = askId;
    setAskError(null);
    setAskSubmitting(false);
    // 提问卡片在转录里，可能落在视口外（用户正翻前面的内容时尤其容易）。新问题一到
    // 就带到最底部：Agent 正卡在这儿等答复，让用户自己发现"要回答"比轻微打断更糟。
    // 钉在输入框上方时不会有这个问题。
    if (askId) handleJumpToBottom();
  }, [askId, handleJumpToBottom]);
  const handleAskSubmit = useCallback(
    async (answers: Array<string[] | null>) => {
      const target = pendingAskIdRef.current;
      if (!target) return;
      setAskSubmitting(true);
      setAskError(null);
      try {
        await answerAgentAsk(effectiveProject, answers, activeSessionRef.current || undefined);
        setAnsweredAskId(target);
      } catch (err) {
        setAskError(normalizeError(err, '回答提交失败'));
      } finally {
        setAskSubmitting(false);
      }
    },
    [effectiveProject],
  );
  // 正在等用户点批准的权限请求：那条工具调用还没结果（结果一到就说明批过了、拒了或
  // 超时了）。与 pendingAsk 同一套推导——从后往前找最新的那条。
  const pendingPermission = useMemo(() => {
    for (let gi = timeline.length - 1; gi >= 0; gi -= 1) {
      const group = timeline[gi];
      if (group.type !== 'activity') continue;
      for (let i = group.items.length - 1; i >= 0; i -= 1) {
        const it = group.items[i];
        if (it.kind === 'tool' && it.permission && it.result === undefined && it.error === undefined) {
          return it;
        }
      }
    }
    return null;
  }, [timeline]);
  const [permissionSubmitting, setPermissionSubmitting] = useState(false);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  // 同上：提交后先把卡片收起来，工具结果一到就自然消失
  const [answeredPermissionId, setAnsweredPermissionId] = useState('');
  const pendingPermissionIdRef = useRef('');
  const permissionId = pendingPermission?.permission?.id || '';
  useEffect(() => {
    pendingPermissionIdRef.current = permissionId;
    setPermissionError(null);
    setPermissionSubmitting(false);
    // 审批卡同样摆在转录里：Agent 正卡在这儿等一个点击，带到最底部
    if (permissionId) handleJumpToBottom();
  }, [permissionId, handleJumpToBottom]);
  const handlePermissionDecide = useCallback(
    async (decision: PermissionDecision, reason?: string) => {
      const target = pendingPermissionIdRef.current;
      if (!target) return;
      setPermissionSubmitting(true);
      setPermissionError(null);
      try {
        await answerAgentPermission(
          effectiveProject,
          decision,
          activeSessionRef.current || undefined,
          // 拒绝原因只在拒绝时送（后端也只认拒绝那条）
          decision === 'deny' ? reason : undefined,
        );
        setAnsweredPermissionId(target);
      } catch (err) {
        setPermissionError(normalizeError(err, '提交失败'));
      } finally {
        setPermissionSubmitting(false);
      }
    },
    [effectiveProject],
  );

  // 权限模式：本地存一份（下次打开还是这个档），同时立刻推给后端——
  // 跑着也能改，下一次工具调用就按新档判。选择器参考「后端配置」那个 chip：点开是菜单。
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(() => loadPermissionMode());
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false);
  const permissionPickerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!permissionMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!permissionPickerRef.current?.contains(e.target as Node)) setPermissionMenuOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPermissionMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [permissionMenuOpen]);
  const handlePickPermissionMode = useCallback(
    (mode: PermissionMode) => {
      setPermissionMode(mode);
      savePermissionMode(mode);
      setPermissionMenuOpen(false);
      // 立刻推给后端：回合跑着也能改，下一次工具调用就按新档判。没有会话时不用推——
      // 本地已经存了，首条消息的 start（以及后续 message）会带上，两边一致。
      if (!effectiveProject || !hasBackendSessionRef.current) return;
      void setAgentPermissionMode(
        effectiveProject,
        mode,
        activeSessionRef.current || undefined,
      ).catch((err) => setError(normalizeError(err, '权限模式修改失败')));
    },
    [effectiveProject],
  );
  // 展示「后端配置文件名/模型名」：模型名从当前配置里取，与「模型设置」页同一口径
  const backendProfileLabel = useMemo(
    () => (backendProfileName ? formatProfileLabel(backendProfileName, getBackendProfile(backendProfileName)) : ''),
    [backendProfileName],
  );

  return (
    <div className="agent-console">
      <AgentSessionSidebar
        sessionsByProject={sessionsByProject}
        projects={projectOptions}
        activeProject={effectiveProject}
        activeSessionId={activeSessionId}
        collapsed={collapsedProjects}
        disabled={running}
        activeRunning={running}
        unseenLights={unseenLights}
        onCreateBlank={() => void handleCreateBlankSession()}
        onCreateInProject={(dir) => void handleCreateSessionInProject(dir)}
        onToggleProject={handleToggleProject}
        onSelectSession={handleSelectSession}
        onDeleteSession={(dir, s) => void handleDeleteSession(dir, s)}
      />
      <div className="agent-console__main agent-cockpit">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar" aria-hidden><Icon name="bot" /></span>
          <div className="agent-console__bar-copy">
            <div className="agent-console__bar-title">
              <span className="agent-console__bar-name">GalTransl Agent</span>
            </div>
            <div className="agent-console__project-static">
              {projectDir ? (
                <>
                  <span className="agent-console__project-name">{shortName(projectDir)}</span>
                  <span className="agent-console__project-path">{projectDir}</span>
                </>
              ) : (
                <span className="agent-console__project-empty">未选择项目</span>
              )}
            </div>
          </div>
        </div>

        <div className="agent-console__bar-right">
          <StatusPill status={status} running={running} />
          <button
            type="button"
            className="agent-console__icon-btn"
            onClick={() => { if (projectDir) void invoke('open_folder', { path: projectDir }); }}
            disabled={!projectDir}
            title={projectDir || '打开项目文件夹'}
            aria-label="打开项目文件夹"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M3 7.2c0-1.12.9-2.02 2-2.02h4.17c.53 0 1.04.21 1.41.59L12 7.2h7c1.1 0 2 .9 2 2.02v7.77c0 1.12-.9 2.02-2 2.02H5c-1.1 0-2-.9-2-2.02V7.2z"
              />
            </svg>
          </button>
          <button
            type="button"
            className="agent-console__icon-btn"
            onClick={() => void handleClear()}
            disabled={running || !events.length}
            title="重置会话（清空全部对话与后端历史）"
          >
            <Icon name="trash" />
          </button>
        </div>
      </header>

      <div className="agent-console__thread" ref={scrollRef} onScroll={handleScroll}>
        <div className="agent-thread">
          {timeline.length === 0 ? (
            <div className="agent-hero">
              <div className="agent-hero__mark"><Icon name="bot" /></div>
              <h2 className="agent-hero__title">让 Agent 替你跑完整个翻译流程</h2>
              <p className="agent-hero__subtitle">
                发送第一条消息启动会话，它会自主了解项目、准备字典、启动翻译、跟进进度，并复核修复发现的问题。运行中你可以随时插话或点停止打断，之后继续发消息它会在原会话上接着干。
              </p>
              <div className="agent-hero__steps">
                <span>① 了解项目</span>
                <span>② 准备字典</span>
                <span>③ 启动翻译</span>
                <span>④ 复核修复</span>
              </div>
              <div className="agent-hero__project-panel">
                {projectOptions.length > 0 ? (
                  <>
                    <span className="agent-hero__open-projects-label">选择一个已打开的项目开始</span>
                    <div className="agent-hero__project-chips">
                      {projectOptions.map((dir) => {
                        // 选中的那个常亮：只靠 hover 的话鼠标一移开就看不出当前选的是谁
                        const selected = dir === projectDir;
                        return (
                          <button
                            key={dir}
                            type="button"
                            className={`agent-hero__project-chip${selected ? ' is-selected' : ''}`}
                            onClick={() => chooseProject(dir)}
                            title={dir}
                            aria-pressed={selected}
                          >
                            <span className="agent-hero__project-chip-icon"><Icon name="folder" /></span>
                            <span className="agent-hero__project-chip-name">{shortName(dir)}</span>
                          </button>
                        );
                      })}
                    </div>
                  </>
                ) : (
                  <span className="agent-hero__open-projects-label">
                    还没有打开的项目，从下方新建或打开一个吧
                  </span>
                )}
                <div className="agent-hero__actions">
                  <button
                    type="button"
                    className="agent-hero__action"
                    onClick={() => void handleOpenProject()}
                    title="从文件夹打开一个已有项目"
                  >
                    <Icon name="folder-open" /> 打开项目
                  </button>
                  <button
                    type="button"
                    className="agent-hero__action agent-hero__action--secondary"
                    onClick={() => navigate('/new-project')}
                    title="新建项目向导"
                  >
                    <Icon name="sparkle" /> 新建项目
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <>
              {timeline.map((group, index) => (
                <Fragment key={group.id}>
                  <AgentGroupView
                    group={group}
                    isLive={running && index === timeline.length - 1}
                    projectDir={effectiveProject}
                    persistKey={`${effectiveProject}::${activeSessionId}`}
                  />
                  {/* ask_user 的提问卡片就摆在那个工具行下面（同一回合内），不钉在输入框
                      上方——"在什么上下文里问了什么"一眼对得上。key 用 askId，换一题就重挂，
                      免得上一题的草稿被带到下一题。 */}
                  {group.type === 'activity' &&
                  pendingAsk &&
                  askId !== answeredAskId &&
                  group.items.some((it) => it.id === askId) ? (
                    <AskUserCard
                      key={askId}
                      item={pendingAsk}
                      submitting={askSubmitting}
                      error={askError}
                      onSubmit={(answers) => void handleAskSubmit(answers)}
                    />
                  ) : null}
                  {/* 权限确认卡：与提问卡同一套摆法——就在那次工具调用下面（同一回合内） */}
                  {group.type === 'activity' &&
                  pendingPermission &&
                  permissionId !== answeredPermissionId &&
                  group.items.some((it) => it.id === pendingPermission.id) ? (
                    <PermissionCard
                      key={permissionId}
                      item={pendingPermission}
                      submitting={permissionSubmitting}
                      error={permissionError}
                      onDecide={(decision, reason) =>
                        void handlePermissionDecide(decision, reason)
                      }
                    />
                  ) : null}
                </Fragment>
              ))}

              {running ? (
                <div className="agent-working">
                  <span className="agent-working__dots">
                    <span />
                    <span />
                    <span />
                  </span>
                  <span className={`agent-working__label${lastActivityItem(timeline)?.kind === 'retry' ? ' is-retry' : ''}`}>
                    {workingLabel(timeline)}
                  </span>
                </div>
              ) : null}
            </>
          )}

          {error ? (
            <div className="agent-notice agent-notice--error">
              <span className="agent-notice__icon"><Icon name="warning" /></span>
              <div className="agent-notice__body">
                <div className="agent-notice__title">{error}</div>
              </div>
            </div>
          ) : null}
        </div>
        {/* 不在底部时贴右下角的圆形"回到最新"（sticky 跟着滚动口走，见 CSS） */}
        {!atBottom ? (
          <div className="agent-thread__jump">
            <button
              type="button"
              className="agent-jump-bottom"
              onClick={handleJumpToBottom}
              title="回到最新"
              aria-label="回到最新"
            >
              <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
                {/* 只要一个 V 形雪佛龙，不带竖棍 */}
                <path
                  d="M6 9l6 6 6-6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </div>
        ) : null}
      </div>

      <div className="agent-console__composer">
        {/* 排队中的消息：运行中发的消息先落到这里（不进聊天框）。
            每条可以「立即」打断模型马上发，也可以就地编辑或删掉。 */}
        {queued.length ? (
          <div className="agent-queue">
            <div className="agent-queue__list">
              {queued.map((item) => {
                const editing = editingQueued?.id === item.id;
                return (
                  <div className="agent-queue__item" key={item.id}>
                    {editing ? (
                      <>
                        <input
                          className="agent-queue__input"
                          value={editingQueued?.text ?? ''}
                          autoFocus
                          onChange={(e) => setEditingQueued({ id: item.id, text: e.target.value })}
                          onKeyDown={(e) => {
                            if (e.key === 'Escape') {
                              setEditingQueued(null);
                              return;
                            }
                            if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                              e.preventDefault();
                              void handleQueuedSaveEdit();
                            }
                          }}
                        />
                        <button
                          type="button"
                          className="agent-queue__act is-primary"
                          onClick={() => void handleQueuedSaveEdit()}
                        >
                          保存
                        </button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          onClick={() => setEditingQueued(null)}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="agent-queue__text" title={item.text}>
                          {item.text}
                        </span>
                        <button
                          type="button"
                          className="agent-queue__act is-primary"
                          title="打断 Agent，马上发送这条"
                          onClick={() => void handleQueuedSendNow(item.id)}
                        >
                          <span className="agent-queue__act-icon"><Icon name="send-now" /></span>立即
                        </button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          title="编辑这条"
                          aria-label="编辑这条"
                          onClick={() => setEditingQueued({ id: item.id, text: item.text })}
                        >
                          <Icon name="pencil" />
                        </button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          title="删除这条"
                          aria-label="删除这条"
                          onClick={() => void handleQueuedDelete(item.id)}
                        >
                          <Icon name="trash" />
                        </button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}
        <div className={`agent-composer${running ? ' is-running' : ''}${queued.length ? ' has-queue' : ''}`}>
          <textarea
            className="agent-composer__input"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder={
              running
                ? '继续输入以排队后续消息，本轮做完自动发出；想提前发就点队列里的「立即」。'
                : hasSession
                  ? '给 Agent 下一步指令，它会接着当前进度继续。'
                  : '描述你希望 Agent 完成的任务，例如：按标准流程完成本项目的翻译。'
            }
            rows={2}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' || e.metaKey || e.ctrlKey) return;
              // Shift+Enter 换行；输入法组词中的回车（选词）不触发发送
              if (e.shiftKey) return;
              if (e.nativeEvent.isComposing) return;
              if (!canSend) return;
              e.preventDefault();
              void handleSend();
            }}
          />
          <div className="agent-composer__toolbar">
            <div className="agent-composer__left">
              {hasSession ? (
                // 会话已经开聊：项目是这次会话的锚点，换它等于换会话——chip 退化成纯标签，
                // 光标与 hover 底色一并收掉（见 .agent-composer__chip--static），别看着能点
                <span
                  className="agent-composer__chip agent-composer__chip--static"
                  title={`${projectDir || '未选择项目'}（会话已开始；要换项目请从侧边栏新建会话）`}
                >
                  <span className="agent-composer__chip-icon"><Icon name="folder" /></span>
                  <span className="agent-composer__chip-label">{projectDir ? shortName(projectDir) : '未选择项目'}</span>
                </span>
              ) : (
                // 项目已选、还没开聊：这时换项目零成本，chip 就是入口（菜单与旁边两个 chip 同款）
                <div className="agent-profile-picker" ref={projectPickerRef}>
                  <button
                    type="button"
                    className={`agent-composer__chip${projectMenuOpen ? ' is-open' : ''}`}
                    onClick={() => setProjectMenuOpen((v) => !v)}
                    aria-haspopup="menu"
                    aria-expanded={projectMenuOpen}
                    title={`${projectDir || '未选择项目'} · 点击换一个项目（会话开始后就固定了）`}
                  >
                    <span className="agent-composer__chip-icon"><Icon name="folder" /></span>
                    <span className="agent-composer__chip-label">{projectDir ? shortName(projectDir) : '未选择项目'}</span>
                  </button>
                  {projectMenuOpen ? (
                    <div className="agent-profile-menu" role="menu">
                      {projectOptions.length === 0 ? (
                        <div className="agent-profile-menu__empty">还没有打开的项目</div>
                      ) : (
                        projectOptions.map((dir) => (
                          <button
                            key={dir}
                            type="button"
                            role="menuitemradio"
                            aria-checked={dir === projectDir}
                            className="agent-profile-menu__item"
                            onClick={() => {
                              switchProjectBeforeSession(dir);
                              setProjectMenuOpen(false);
                            }}
                            title={dir}
                          >
                            <span className="agent-profile-menu__label">{shortName(dir)}</span>
                            {dir === projectDir ? (
                              <span className="agent-profile-menu__check" aria-hidden><Icon name="check" /></span>
                            ) : null}
                          </button>
                        ))
                      )}
                      <div className="agent-profile-menu__sep" />
                      <button
                        type="button"
                        role="menuitem"
                        className="agent-profile-menu__item agent-profile-menu__item--action"
                        onClick={() => {
                          setProjectMenuOpen(false);
                          void handleOpenProject();
                        }}
                      >
                        <span className="agent-profile-menu__label">打开其它项目…</span>
                        <span className="agent-profile-menu__chev" aria-hidden>›</span>
                      </button>
                    </div>
                  ) : null}
                </div>
              )}
              <div className="agent-profile-picker" ref={profilePickerRef}>
                <button
                  type="button"
                  className={`agent-composer__chip${profileMenuOpen ? ' is-open' : ''}`}
                  onClick={() => setProfileMenuOpen((v) => !v)}
                  disabled={running}
                  aria-haspopup="menu"
                  aria-expanded={profileMenuOpen}
                  title={
                    running
                      ? 'Agent 运行中，暂不能切换后端配置'
                      : `${backendProfileLabel || '未配置后端'}${
                          boundBackendProfile ? ' · 已绑定到本会话' : ' · 跟随 Agent 默认'
                        } · 点击切换（只影响当前会话）`
                  }
                >
                  <span className="agent-composer__chip-icon"><Icon name="settings" /></span>
                  <span className="agent-composer__chip-label">{backendProfileLabel || '未配置后端'}</span>
                </button>
                {profileMenuOpen ? (
                  <div className="agent-profile-menu" role="menu">
                    {backendProfileNames.length === 0 ? (
                      <div className="agent-profile-menu__empty">还没有后端配置</div>
                    ) : (
                      <>
                        {boundBackendProfile ? (
                          <button
                            type="button"
                            role="menuitemradio"
                            aria-checked={false}
                            className="agent-profile-menu__item agent-profile-menu__item--action"
                            onClick={() => {
                              // 解除本会话的绑定，回到跟随 Agent 默认
                              setSessionBackend(effectiveProject, activeSessionId, null);
                              setProfileMenuOpen(false);
                            }}
                          >
                            <span className="agent-profile-menu__label">
                              跟随 Agent 默认（{formatProfileLabel(defaultProfileName, getBackendProfile(defaultProfileName))}）
                            </span>
                          </button>
                        ) : null}
                        {backendProfileNames.map((name) => (
                          <button
                            key={name}
                            type="button"
                            role="menuitemradio"
                            aria-checked={name === backendProfileName}
                            className="agent-profile-menu__item"
                            onClick={() => {
                              // 只改当前会话绑定的配置（空态则是新会话草稿），不影响别的会话
                              setSessionBackend(effectiveProject, activeSessionId, name);
                              setProfileMenuOpen(false);
                            }}
                          >
                            <span className="agent-profile-menu__label">
                              {formatProfileLabel(name, getBackendProfile(name))}
                            </span>
                            {name === backendProfileName ? (
                              <span className="agent-profile-menu__check" aria-hidden><Icon name="check" /></span>
                            ) : null}
                          </button>
                        ))}
                      </>
                    )}
                    <div className="agent-profile-menu__sep" />
                    <button
                      type="button"
                      role="menuitem"
                      className="agent-profile-menu__item agent-profile-menu__item--action"
                      onClick={() => {
                        setProfileMenuOpen(false);
                        navigate('/backend-profiles');
                      }}
                    >
                      <span className="agent-profile-menu__label">管理后端配置</span>
                      <span className="agent-profile-menu__chev" aria-hidden>›</span>
                    </button>
                  </div>
                ) : null}
              </div>
              {/* 权限模式：跟「后端配置」同一个 chip 样子，三档直接选 */}
              <div className="agent-profile-picker" ref={permissionPickerRef}>
                <button
                  type="button"
                  className={`agent-composer__chip${permissionMenuOpen ? ' is-open' : ''}`}
                  onClick={() => setPermissionMenuOpen((v) => !v)}
                  aria-haspopup="menu"
                  aria-expanded={permissionMenuOpen}
                  title={`权限模式：${PERMISSION_MODE_LABELS[permissionMode]} —— ${PERMISSION_MODE_HINTS[permissionMode]}${
                    running ? '（运行中改也会立刻生效：下一次工具调用就按新档判）' : ''
                  }`}
                >
                  <span className="agent-composer__chip-icon"><Icon name="shield" /></span>
                  <span className="agent-composer__chip-label">{PERMISSION_MODE_LABELS[permissionMode]}</span>
                </button>
                {permissionMenuOpen ? (
                  <div className="agent-profile-menu" role="menu">
                    {PERMISSION_MODES.map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        role="menuitemradio"
                        aria-checked={mode === permissionMode}
                        className="agent-profile-menu__item agent-profile-menu__item--stacked"
                        onClick={() => handlePickPermissionMode(mode)}
                      >
                        <span className="agent-profile-menu__label">
                          {PERMISSION_MODE_LABELS[mode]}
                          <span className="agent-profile-menu__hint">{PERMISSION_MODE_HINTS[mode]}</span>
                        </span>
                        {mode === permissionMode ? (
                          <span className="agent-profile-menu__check" aria-hidden><Icon name="check" /></span>
                        ) : null}
                      </button>
                    ))}
                    <div className="agent-profile-menu__sep" />
                    <div className="agent-profile-menu__note">
                      换档会清空本会话「允许」过的工具，之后会重新询问。
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
            <div className="agent-composer__right">
              {contextUsage && contextUsage.used_tokens > 0 ? (
                <ContextMeter usage={contextUsage} />
              ) : null}
              {running ? (
                <>
                  <button
                    type="button"
                    className="agent-composer__send"
                    onClick={() => void handleSend()}
                    disabled={!canSend}
                    title="发送插话（Agent 会在下一步看到，Enter 发送 / Shift+Enter 换行）"
                    aria-label="发送插话"
                  >
                    <SendIcon />
                  </button>
                  <button type="button" className="agent-composer__stop" onClick={handleStop} title="停止 Agent">
                    <StopIcon />
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="agent-composer__send"
                  onClick={() => void handleSend()}
                  disabled={!canSend}
                  title={hasSession ? '发送并继续（Enter 发送 / Shift+Enter 换行）' : '发送并启动 Agent（Enter 发送 / Shift+Enter 换行）'}
                  aria-label="发送消息"
                >
                  <SendIcon />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
      </div>
    </div>
  );
}

function isTerminal(s: string): boolean {
  return s === 'awaiting_input' || s === 'done' || s === 'failed' || s === 'idle';
}

/** 事件数组里的最大 step（忽略本地乐观的 -1）。 */
function maxStep(events: AgentEvent[]): number {
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
function seedStreaming(
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

function mergeTranscriptEvents(cached: AgentEvent[], backend: AgentEvent[]): AgentEvent[] {
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

/** 当前活动组最后一条被渲染的条目（决定运行指示器的文案与配色）。 */
function lastActivityItem(timeline: TimelineGroup[]): ActivityItem | null {
  const last = timeline[timeline.length - 1];
  if (last && last.type === 'activity' && last.items.length) {
    return last.items[last.items.length - 1];
  }
  return null;
}

function workingLabel(timeline: TimelineGroup[]): string {
  const item = lastActivityItem(timeline);
  if (item) {
    if (item.kind === 'content' || item.kind === 'reasoning') return '思考中';
    // 重试/压缩行不是工具调用、没有 name，走 toolMeta 会误显示成「调用工具」
    if (item.kind === 'retry') {
      const attempt = item.attempt ?? 1;
      const max = item.maxAttempts ?? 0;
      return max > 0 ? `重试中（第 ${attempt}/${max} 次）` : '重试中';
    }
    if (item.kind === 'compact') return '整理上下文';
    // 工具行只有在**还没结果**时才代表"正在做这件事"（判定同 toolRowPhases）。
    // 结果一到这行就完成了——继续挂它的 running 文案会让"等你回答…"一直亮着，
    // 明明已经答完、后端都开始请求下一轮了。
    if (item.ok === undefined && item.result === undefined && item.error === undefined) {
      return `${toolMeta(item.name).running}…`;
    }
    return '处理中';
  }
  return '正在开始';
}

function StatusPill({ status, running }: { status: string; running: boolean }) {
  const tone = running ? 'running' : status;
  const label = running
    ? '运行中'
    : status === 'awaiting_input' || status === 'done'
      ? '等待指令'
      : status === 'stopped'
        ? '已停止'
        : status === 'failed'
          ? '出错'
          : '空闲';
  return (
    <span className={`agent-status-pill agent-status-pill--${tone}`}>{label}</span>
  );
}

/* ── Activity group (thinking + tool calls collapsed into one row) ── */

/** 用户手动开合过的折叠状态（模块级）：切页面/切会话会把组件卸载重建，
 *  useState 里的展开状态会丢。这里按 persistKey（项目::会话::组/条目）记住，
 *  重挂时恢复。只记「用户点过」的，自动跟随逻辑不受影响。 */
const manualOpenState = new Map<string, boolean>();

function AgentGroupView({
  group,
  isLive,
  projectDir,
  persistKey,
}: {
  group: TimelineGroup;
  isLive: boolean;
  projectDir: string;
  persistKey: string;
}) {
  // Terminal groups render as notices and hold no disclosure state; dispatch
  // them before the activity component so its hooks never run conditionally.
  if (group.type === 'user') return <UserMessageRow message={group.message} />;
  if (group.type === 'error') return <ErrorNotice group={group} />;
  if (group.type === 'stopped') return <StoppedNotice group={group} />;
  if (group.type === 'activity' && group.finalContent) {
    return (
      <>
        {group.items.length > 0 ? (
          <AgentActivityGroup group={group} isLive={isLive} projectDir={projectDir} persistKey={persistKey} />
        ) : null}
        <FinalMessage item={group.finalContent} projectDir={projectDir} />
      </>
    );
  }
  return <AgentActivityGroup group={group} isLive={isLive} projectDir={projectDir} persistKey={persistKey} />;
}

/** 回合收尾回复：顶层普通消息，像聊天里最后一条回答。 */
function FinalMessage({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  return <AgentMarkdown text={item.content || ''} projectDir={projectDir} className="agent-final" />;
}

function UserMessageRow({ message }: { message: string }) {
  return (
    <div className="agent-row agent-row--user">
      <div className="agent-bubble agent-bubble--user">
        <div className="agent-bubble__label">我</div>
        <div className="agent-bubble__text">{message}</div>
      </div>
    </div>
  );
}

function AgentActivityGroup({
  group,
  isLive,
  projectDir,
  persistKey,
}: {
  group: Extract<TimelineGroup, { type: 'activity' }>;
  isLive: boolean;
  projectDir: string;
  persistKey: string;
}) {
  const stateKey = `${persistKey}::${group.id}`;
  const [open, setOpenRaw] = useState(() => {
    const saved = manualOpenState.get(stateKey);
    return saved === undefined ? isLive : saved;
  });
  // 恢复过用户选择的组从挂载之初就属于“手动控制”。否则下面的 effect 会在
  // 历史回合 isLive=false 时立刻把刚恢复的展开态重新收起。
  const userToggledRef = useRef(manualOpenState.has(stateKey));
  const setManualOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };
  const items = group.items;

  // Follow the live run: auto-expand while working, auto-collapse when settled,
  // unless the user took manual control of this group.
  useEffect(() => {
    if (userToggledRef.current) return;
    // 自动状态不写入 manualOpenState；只有用户点击才应取得永久控制权。
    setOpenRaw(isLive);
  }, [isLive]);

  // 运行中墙钟计时：live 时每秒跳动，结束冻结在最后值。
  const [now, setNow] = useState(() => Date.now());
  const liveStartedRef = useRef<number | null>(null);
  const wasLiveRef = useRef(isLive);
  const [frozenSec, setFrozenSec] = useState<number | null>(null);
  useEffect(() => {
    // 墙钟起点：进入 live 时记一次。首次挂载时 wasLiveRef 已等于 isLive，只会走
    // 「还没有起点」这一支——否则新建的活动组永远拿不到起点，头部会一直显示
    // 0ms（重试这类没有 durationMs 的活动尤其明显）。
    if (isLive) {
      if (liveStartedRef.current == null || !wasLiveRef.current) liveStartedRef.current = Date.now();
    } else if (wasLiveRef.current && liveStartedRef.current != null) {
      setFrozenSec(Math.max(0, Math.floor((Date.now() - liveStartedRef.current) / 1000)));
    }
    wasLiveRef.current = isLive;
    if (!isLive) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [isLive]);
  // 结束后展示用「事件耗时之和」兜底：恢复会话/刷新后没有墙钟起点。
  const totalMs = items.reduce((sum, it) => sum + (it.durationMs || 0), 0);
  const wallSec = liveStartedRef.current != null ? Math.max(0, Math.floor((now - liveStartedRef.current) / 1000)) : 0;
  const shownSec = isLive
    ? wallSec > 0
      ? wallSec
      : Math.max(0, Math.floor(totalMs / 1000))
    : frozenSec != null && frozenSec > 0
      ? frozenSec
      : Math.max(0, Math.floor(totalMs / 1000));

  // 「想」（reasoning）也算思考内容：只有思考流的回合标签用「思考」而非「处理」
  const hasContent = items.some((it) => it.kind === 'content' || it.kind === 'reasoning');
  const toolCount = items.filter((it) => it.kind === 'tool').length;
  // 压缩与重试提示都不算"工作步骤"：前者是后台维护动作，后者只是同一次请求
  // 的重发（一次重试一个步骤会把「重试 10 次」显示成 10 个步骤）
  const visibleCount = items.filter((it) => it.kind !== 'compact' && it.kind !== 'retry').length;

  // 文案：运行中「思考中/处理中 · Ns」，结束「已思考/已处理 Ns」
  const label = isLive
    ? `${hasContent && !toolCount ? '思考中' : '处理中'} · ${formatDuration(shownSec * 1000)}`
    : hasContent && !toolCount
      ? shownSec > 0 ? `已思考 ${formatDuration(shownSec * 1000)}` : '思考'
      : shownSec > 0
        ? `已处理 ${formatDuration(shownSec * 1000)}`
        : '工作';

  const parts: string[] = [];
  if (visibleCount > 1) parts.push(`${visibleCount} 个步骤`);

  // 预览小字：只在折叠且回合仍在跑时显示（展开时内容全可见，无需预览）
  const tail = isLive && !open ? liveTail(items) : '';

  // 每个工具行"轮到哪一步"（在跑 / 等批准 / 排队 / 断了）：见 toolRowPhases 的说明
  const phases = toolRowPhases(items, isLive);

  return (
    <div className={`agent-activity${open ? ' is-open' : ''}${isLive ? ' is-live' : ''}`}>
      <button
        type="button"
        className="agent-activity__header"
        onClick={() => {
          userToggledRef.current = true;
          setManualOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-activity__icon"><Icon name="spark" /></span>
        <span className={`agent-activity__label${isLive ? ' is-running' : ''}`}>{label}</span>
        {parts.length ? <span className="agent-activity__meta">{parts.join(' · ')}</span> : null}
        <span className="agent-activity__caret">›</span>
      </button>
      {tail ? <div className="agent-activity__preview">{tail}</div> : null}
      <div className="agent-activity__collapse">
        <div className="agent-activity__collapse-inner">
          <div className="agent-activity__body">
            {items.map((item, i) =>
              item.kind === 'content' ? (
                <ContentRow key={`t-${i}`} item={item} projectDir={projectDir} />
              ) : item.kind === 'reasoning' ? (
                <ReasoningRow key={`r-${i}`} item={item} projectDir={projectDir} persistKey={persistKey} />
              ) : item.kind === 'compact' ? (
                <CompactRow key={`c-${i}`} item={item} />
              ) : item.kind === 'retry' ? (
                <RetryRow key={`rt-${i}`} item={item} />
              ) : item.kind === 'tool' && item.name === 'start_translation' && translationJobId(item) ? (
                // 启动翻译换成工作台顶部卡的迷你版（带实时进度），不再是一坨 JSON
                <TranslationJobCard key={`j-${item.id || i}`} item={item} projectDir={projectDir} />
              ) : (
                <ToolRow key={`x-${item.id || i}`} item={item} phase={phases[i]} persistKey={persistKey} />
              ),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function liveTail(items: ActivityItem[]): string {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const it = items[i];
    if ((it.kind === 'content' || it.kind === 'reasoning') && it.content) {
      // 取思考文本最后一行：折叠头部读起来像实时滚动的一行预览
      return lastReasoningLine(it.content);
    }
    if (it.kind === 'tool') {
      const s = toolMeta(it.name).summary(asArgs(it.arguments));
      return [toolMeta(it.name).action, s].filter(Boolean).join(' ');
    }
  }
  return '';
}

function asArgs(args: unknown): Record<string, unknown> | undefined {
  if (args && typeof args === 'object' && !Array.isArray(args)) return args as Record<string, unknown>;
  return undefined;
}

function ContentRow({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  // 模型「说」的回复：直接渲染为普通黑体纯文本，不再用可折叠卡片包裹。
  const text = item.content || '';
  const streaming = Boolean(item.streaming);

  return (
    <div className={`agent-content${streaming ? ' is-streaming' : ''}`}>
      <AgentMarkdown
        text={text}
        projectDir={projectDir}
        cursor={streaming}
        className="agent-content__text"
      />
    </div>
  );
}

/* ── Reasoning row（模型「想」的思考过程）──
   与「说」分开：想用可折叠卡片，**默认折叠**（思考过程不自动铺开，要看细节点一下）。
   折叠且正在思考时，标题右侧用跑马灯滚动最近一行，保留"能感知在思考"的实时感；
   展开后内容就在眼前，跑马灯随即消失。 */

/** 折叠时跑马灯最多滚多少字符（取最近的一段，太长会滚得让人看不清）。 */
const REASONING_MARQUEE_CHARS = 160;

/** 思考文本压成一行：去掉标题/加粗标记，取最后一行（活动组头部的静态预览用）。 */
function lastReasoningLine(text: string): string {
  const lines = text
    .split('\n')
    .map((line) => line.replace(/^#+\s*|\*\*/g, '').trim())
    .filter(Boolean);
  return lines[lines.length - 1] || '';
}

/** 折叠跑马灯显示的文本：整段思考**压成一行**后取末尾一段。
 *
 *  刻意不按"最后一行"取：模型换行后新行往往只有一两个字（甚至先来一串空行），
 *  窗口会瞬间缩成空白。压成一行（换行/连续空白折叠为空格）则前后文字连成一句，
 *  换行在预览里不显示，滚动也不断线。 */
function reasoningOneLiner(text: string): string {
  return text
    .replace(/^[ \t]*#{1,6}[ \t]*/gm, '') // 行首标题标记
    .replace(/\*\*/g, '') // 加粗标记
    .replace(/\s+/g, ' ') // 换行 / 连续空白 -> 单个空格
    .trim()
    .slice(-REASONING_MARQUEE_CHARS);
}

function ReasoningRow({
  item,
  projectDir,
  persistKey,
}: {
  item: ActivityItem;
  projectDir: string;
  persistKey: string;
}) {
  const streaming = Boolean(item.streaming);
  // 默认折叠，且不跟随流式自动展开（用户手动开过就一直是开的——包括切页面重挂后）
  const stateKey = `${persistKey}::r-${item.step}-${item.id || ''}`;
  const [open, setOpenRaw] = useState(() => manualOpenState.get(stateKey) === true);
  const setOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };

  const text = item.content || '';
  const label = streaming ? '思考中' : item.durationMs ? `已思考 ${formatDuration(item.durationMs)}` : '已思考';
  // 只在「折叠 + 正在思考」时滚动最近内容；展开后不再显示
  const marquee = !open && streaming ? reasoningOneLiner(text) : '';

  return (
    <div className={`agent-reasoning${open ? ' is-open' : ''}${streaming ? ' is-streaming' : ''}`}>
      <button
        type="button"
        className="agent-reasoning__header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-reasoning__icon"><Icon name="spark" /></span>
        <span className={`agent-reasoning__label${streaming ? ' is-running' : ''}`}>{label}</span>
        {marquee ? (
          // 装饰性的一行滚动预览：整段思考在展开区里，读屏不必重复
          <span className="agent-reasoning__marquee" aria-hidden="true">
            <span className="agent-reasoning__marquee-text">{marquee}</span>
          </span>
        ) : null}
        <span className="agent-reasoning__caret">›</span>
      </button>
      <div className="agent-reasoning__collapse">
        <div className="agent-reasoning__collapse-inner">
          <AgentMarkdown
            text={text}
            projectDir={projectDir}
            cursor={streaming}
            className="agent-reasoning__text"
          />
        </div>
      </div>
    </div>
  );
}

/* ── Compact row (上下文压缩提示) ──
   压缩是后台维护动作，不是用户要读的内容，所以只做一行轻量提示。 */

function CompactRow({ item }: { item: ActivityItem }) {
  const removed = item.removed || 0;
  const before = item.tokensBefore || 0;
  const after = item.tokensAfter || 0;
  // 压缩后的大小是后端在**重建出来的真实历史**上估的：保留的尾部（可能带着很大的工具
  // 结果）都算在内。所以这里显示 before → after，别拿摘要长度当"压缩后的大小"。
  const size = after > 0
    ? `（估算 ${before > 0 ? formatTokenCount(before) : '?'} → ${formatTokenCount(after)} tokens）`
    : '';
  return (
    <div className="agent-compact-note" title="早期对话已被摘要压缩，以腾出上下文空间">
      <span className="agent-compact-note__icon"><Icon name="compress" /></span>
      <span className="agent-compact-note__text">
        已压缩上下文 · 摘要 {removed} 条早期消息{size}
      </span>
    </div>
  );
}

/* ── Retry row (LLM 请求失败自动重试) ──
   退避等待期间每秒刷新剩余秒数，读起来像「3 秒后重试 · 第 1/3 次」；
   退避结束（llm_retry_end）后定格成「已重试」，不再跳动。
   失败原因挂在 title 上，鼠标悬停可看。 */

const RETRY_CODE_LABELS: Record<string, string> = {
  NETWORK_ERROR: '连接失败',
  TIMEOUT: '请求超时',
  RATE_LIMITED: '被限流',
  PROVIDER_ERROR: '服务端错误',
  STREAM_FAILED: '响应中断',
};

function RetryRow({ item }: { item: ActivityItem }) {
  const live = !item.retryDone;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [live]);

  const startedAt = item.retryStartedAtMs ?? now;
  const delayMs = item.retryDelayMs ?? 0;
  const remainingSec = Math.max(0, Math.ceil((delayMs - (now - startedAt)) / 1000));
  const attempt = item.attempt ?? 1;
  const maxAttempts = item.maxAttempts ?? 0;
  const attemptText = maxAttempts > 0 ? `第 ${attempt}/${maxAttempts} 次` : `第 ${attempt} 次`;
  const cause = item.retryCode ? RETRY_CODE_LABELS[item.retryCode] || '请求失败' : '请求失败';
  const title = item.retryReason ? `${cause}：${item.retryReason}` : cause;

  return (
    <div className={`agent-retry-note${live ? ' is-live' : ''}`} title={title}>
      <span className="agent-retry-note__icon"><Icon name="refresh" /></span>
      <span className="agent-retry-note__text">
        {live ? `${cause}，${remainingSec} 秒后重试` : '已重试'}
        <span className="agent-retry-note__count"> · {attemptText}</span>
      </span>
    </div>
  );
}

/* ── 启动翻译卡片（翻译工作台顶部卡的迷你版） ──
   原本这里是「启动翻译 · ForGal-json · 仅 1 个文件」加一段原始参数/结果 JSON。
   现在换成工作台那张顶部卡的瘦身版：百分比 + 进度条 + 已译/总数，外加实时速度、
   预计剩余、已用时长三个关键数字。翻译期间每秒拉一次运行时快照并保持展开，
   跑完自动折叠（用户自己点过就听用户的）；原始参数/结果收在开关后面。 */

const JOB_CARD_POLL_MS = 1000;

function TranslationJobCard({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  const args = asArgs(item.arguments);
  const result =
    item.result && typeof item.result === 'object' ? (item.result as Record<string, unknown>) : {};
  const jobId = str(result.job_id);
  const translator = str(args?.translator) || str(result.translator);
  const fileCount = Array.isArray(args?.files) ? args.files.length : 0;

  const [runtime, setRuntime] = useState<ProjectRuntimeResponse | null>(null);
  const [lastStatus, setLastStatus] = useState<string>(str(result.status));
  const [open, setOpen] = useState(true);
  const [rawOpen, setRawOpen] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const userToggledRef = useRef(false);
  const seenJobRef = useRef(false);
  const fallbackRef = useRef(false);

  // 快照里"当前任务"就是这张卡的任务时才认它（换了新任务就不再跟）
  const snapJob: RuntimeJob | null = runtime && jobId && runtime.job?.job_id === jobId ? runtime.job : null;
  const status = snapJob?.status ?? lastStatus;
  const isRunning = status === 'pending' || status === 'running';

  // 翻译期间每秒拉一次运行时快照（进度/速度/剩余/时长都从它来）；不在跑了就停。
  useEffect(() => {
    if (!jobId || !projectDir || !isRunning) return undefined;
    const projectId = encodeProjectDir(projectDir);
    let cancelled = false;
    const pull = async () => {
      try {
        const data = await fetchProjectRuntime(projectId);
        if (cancelled) return;
        setRuntime(data);
        if (data.job && data.job.job_id === jobId) {
          seenJobRef.current = true;
          setLastStatus(data.job.status);
          return;
        }
        // 快照里的"当前任务"不是这张卡的（刷新页面后回放旧转录、或工作台又开了新任务）：
        // 去任务列表里查一次它的最终状态，别一直按"翻译中"空转轮询。
        if (!seenJobRef.current && !fallbackRef.current) {
          fallbackRef.current = true;
          const jobs = await fetchJobs();
          if (cancelled) return;
          const mine = jobs.find((j) => j.job_id === jobId);
          setLastStatus(mine ? mine.status : 'completed');
        }
      } catch {
        // 后端不可达：静默跳过，下一拍再试（卡片保持上一帧数据）
      }
    };
    void pull();
    const poll = window.setInterval(() => void pull(), JOB_CARD_POLL_MS);
    const tick = window.setInterval(() => setNowMs(Date.now()), JOB_CARD_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, [jobId, projectDir, isRunning]);

  // 翻译期间自动展开、跑完自动折叠
  useEffect(() => {
    if (userToggledRef.current) return;
    setOpen(isRunning);
  }, [isRunning]);

  const summary = runtime?.summary ?? null;
  const percent = clampPercent(summary?.percent ?? 0);
  const translated = summary?.translated ?? 0;
  const total = summary?.total ?? 0;
  const remaining = Math.max(total - translated, 0);
  const failed = item.ok === false || status === 'failed';
  const tone = isRunning ? 'running' : failed ? 'error' : 'done';
  const stateLabel = isRunning
    ? status === 'pending'
      ? '等待中'
      : '翻译中'
    : failed
      ? '失败'
      : status === 'cancelled'
        ? '已取消'
        : '已完成';
  const resultText = formatPayload(item.ok === false ? item.error : item.result);

  return (
    <div className={`agent-tjob${open ? ' is-open' : ''}${isRunning ? ' is-live' : ''}`}>
      <button
        type="button"
        className="agent-tjob__header"
        onClick={() => {
          userToggledRef.current = true;
          setOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-tjob__icon"><Icon name="play" /></span>
        <span className="agent-tjob__action">启动翻译</span>
        <span className="agent-tjob__summary">
          {[translator, fileCount ? `仅 ${fileCount} 个文件` : ''].filter(Boolean).join(' · ')}
        </span>
        {total > 0 ? (
          <span className="agent-tjob__count">
            {translated} / {total} 句
          </span>
        ) : null}
        <span className={`agent-tjob__state is-${tone}`}>
          <span className="agent-tjob__state-dot" />
          {stateLabel}
        </span>
        <span className="agent-tjob__caret">›</span>
      </button>

      {open ? (
        <div className="agent-tjob__body">
          <div className="agent-tjob__gauge">
            <span className="agent-tjob__percent">
              {formatPercentDisplay(summary?.percent ?? 0)}
              <span className="agent-tjob__percent-sign">%</span>
            </span>
            <span className="agent-tjob__frac">
              已译 <b>{translated}</b> / {total} 句
              <span className="agent-tjob__frac-remain">剩余 {remaining}</span>
            </span>
          </div>

          <div className="agent-tjob__bar" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
            <div className="agent-tjob__bar-fill" style={{ width: `${percent}%` }} />
          </div>

          <div className="agent-tjob__stats">
            <span className="agent-tjob__stat">
              <b>{formatSpeed(summary?.translation_speed_lpm ?? 0)}</b>
              <i>实时速度</i>
            </span>
            <span className="agent-tjob__stat">
              <b>{formatEta(summary?.eta_seconds ?? 0)}</b>
              <i>预计剩余</i>
            </span>
            <span className="agent-tjob__stat">
              <b>{formatElapsedTime(snapJob, nowMs)}</b>
              <i>已用时长</i>
            </span>
          </div>

          <button
            type="button"
            className="agent-tjob__raw-toggle"
            onClick={() => setRawOpen((v) => !v)}
            aria-expanded={rawOpen}
          >
            {rawOpen ? '收起原始数据' : '原始参数 / 结果'}
          </button>
          {rawOpen ? (
            <>
              {item.arguments !== undefined ? (
                <ToolBlock title="参数" content={formatPayload(item.arguments)} mono />
              ) : null}
              {resultText ? (
                <ToolBlock
                  title={item.ok === false ? '错误' : '结果'}
                  content={resultText}
                  mono
                  truncate={1200}
                  tone={item.ok === false ? 'error' : 'default'}
                  durationMs={item.durationMs}
                />
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** 这张工具行是不是"启动翻译"且拿到了 job_id（拿不到就退回原来的工具行）。 */
function translationJobId(item: ActivityItem): string {
  const r = item.result;
  if (!r || typeof r !== 'object') return '';
  return str((r as Record<string, unknown>).job_id);
}

/* ── 询问用户卡片（ask_user）──
   卡片**就在转录里**、紧跟在那个 ask_user 工具行下面（由外层按 group 渲染），
   一题一步、选项按钮 + 固定的「自己填」入口，可以跳过单题或全部跳过。
   **没有倒计时**：后端那个工具不设超时，会一直等着；不想答就点全部跳过，
   或者干脆点停止让 Agent 自己判断。 */

type AskDraft = { values: string[]; custom: boolean; text: string; skipped: boolean };
type AskQuestion = {
  question: string;
  options: string[];
  multiSelect: boolean;
  /** 模型推荐的选项（后端「全自动-零打断」档位下会直接采用它代答） */
  recommended: string;
};

function AskUserCard({
  item,
  submitting,
  error,
  onSubmit,
}: {
  item: ActivityItem;
  submitting: boolean;
  error: string | null;
  onSubmit: (answers: Array<string[] | null>) => void;
}) {
  const argQuestions = asArgs(item.arguments)?.questions;
  const questions: AskQuestion[] = (Array.isArray(argQuestions) ? argQuestions : [])
    .filter((q): q is Record<string, unknown> => Boolean(q) && typeof q === 'object' && !Array.isArray(q))
    .map((q) => ({
      question: str(q.question),
      options: Array.isArray(q.options) ? q.options.map((o) => str(o)).filter(Boolean) : [],
      multiSelect: q.multiSelect === true,
      recommended: str(q.recommended),
    }));

  const [index, setIndex] = useState(0);
  const [drafts, setDrafts] = useState<AskDraft[]>(() =>
    questions.map(() => ({ values: [], custom: false, text: '', skipped: false })),
  );

  if (!questions.length) return null;
  const current = questions[Math.min(index, questions.length - 1)];
  const draft = drafts[Math.min(index, drafts.length - 1)];
  const last = index === questions.length - 1;

  // 一题的答案 = 勾选的选项 + 自己填的内容（都为空 = 跳过）
  const answerOf = (d: AskDraft): string[] => {
    const values = [...d.values];
    const text = d.text.trim();
    if (d.custom && text && !values.includes(text)) values.push(text);
    return values;
  };
  const update = (patch: Partial<AskDraft>) =>
    setDrafts((prev) => prev.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  const goNext = (list: AskDraft[]) => {
    if (last) onSubmit(list.map(answerOf));
    else setIndex((v) => v + 1);
  };
  const toggleOption = (option: string) => {
    // 多选：点几个勾几个，改完自己点「下一题」（点了就走就没法多选了）
    if (current.multiSelect) {
      update({
        values: draft.values.includes(option)
          ? draft.values.filter((v) => v !== option)
          : [...draft.values, option],
      });
      return;
    }
    // 单选：点一下就选中并直接进下一题（最后一题即提交），不必再点「下一题」。
    // 「自己填…」不走这里——它是那一行就地变成输入框，填完回车或点下一题才走。
    const list = drafts.map((d, i) => (i === index ? { ...d, values: [option], custom: false } : d));
    setDrafts(list);
    goNext(list);
  };
  const skipCurrent = () => {
    const list = drafts.map((d, i) =>
      i === index ? { values: [], custom: false, text: '', skipped: true } : d,
    );
    setDrafts(list);
    goNext(list);
  };

  return (
    <div className="agent-ask" role="form" aria-label="Agent 提问">
      <div className="agent-ask__head">
        <span className="agent-ask__icon" aria-hidden><Icon name="help" /></span>
        <span className="agent-ask__title">Agent 想先问你</span>
        {questions.length > 1 ? (
          <span className="agent-ask__progress">
            第 {index + 1} / {questions.length} 题
          </span>
        ) : null}
        <button
          type="button"
          className="agent-ask__decline"
          onClick={() => onSubmit(questions.map(() => null))}
          disabled={submitting}
          title="全部跳过，让 Agent 按自己的判断继续"
        >
          全部跳过
        </button>
      </div>

      {questions.length > 1 ? (
        <div className="agent-ask__dots">
          {questions.map((q, i) => {
            const d = drafts[i];
            const state = answerOf(d).length ? 'is-answered' : d.skipped ? 'is-skipped' : '';
            return (
              <button
                key={`${i}-${q.question}`}
                type="button"
                className={`agent-ask__dot ${state}${i === index ? ' is-current' : ''}`}
                onClick={() => setIndex(i)}
                title={`第 ${i + 1} 题：${q.question}`}
                aria-label={`第 ${i + 1} 题`}
              />
            );
          })}
        </div>
      ) : null}

      <h4 className="agent-ask__question">{current.question}</h4>

      <div className="agent-ask__options" role={current.multiSelect ? 'group' : 'radiogroup'}>
        {current.options.map((option) => {
          const selected = draft.values.includes(option);
          return (
            <button
              key={option}
              type="button"
              role={current.multiSelect ? 'checkbox' : 'radio'}
              aria-checked={selected}
              className={`agent-ask__option${selected ? ' is-selected' : ''}`}
              onClick={() => toggleOption(option)}
              disabled={submitting}
            >
              <span className="agent-ask__mark" aria-hidden>{selected ? <Icon name="check" /> : null}</span>
              <span>{option}</span>
              {option === current.recommended ? (
                <span className="agent-ask__tag" title="Agent 推荐这一项（零打断档位会直接选它）">
                  推荐
                </span>
              ) : null}
            </button>
          );
        })}
        {draft.custom ? (
          /* 「自己填」就地变输入框：点开的是这一行本身，不再在下面另起一个浮出的
             输入框。整行包在 label 里，点行的空白处也能聚焦到输入。 */
          <label className="agent-ask__option agent-ask__option--editing is-selected">
            <span className="agent-ask__mark" aria-hidden><Icon name="check" /></span>
            <input
              className="agent-ask__option-input"
              autoFocus
              value={draft.text}
              placeholder="自己填…"
              aria-label="自己填"
              onChange={(e) => update({ text: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing && draft.text.trim()) {
                  e.preventDefault();
                  goNext(drafts);
                }
                // Esc 退出编辑：与再点一下「自己填…」对称，填的内容一并丢掉
                if (e.key === 'Escape') {
                  e.preventDefault();
                  update({ custom: false, text: '' });
                }
              }}
              // 空着离开就退回未选中的「自己填…」，不留一个空输入框挂在列表里
              onBlur={() => { if (!draft.text.trim()) update({ custom: false }); }}
              disabled={submitting}
            />
          </label>
        ) : (
          <button
            type="button"
            role={current.multiSelect ? 'checkbox' : 'radio'}
            aria-checked={false}
            className="agent-ask__option"
            onClick={() =>
              update({
                custom: true,
                // 单选选中「自己填」要把已选选项让开；多选则各自独立
                ...(current.multiSelect ? {} : { values: [] }),
              })
            }
            disabled={submitting}
          >
            <span className="agent-ask__mark" aria-hidden />
            <span>自己填…</span>
          </button>
        )}
      </div>

      {error ? <div className="agent-ask__error">{error}</div> : null}

      <div className="agent-ask__foot">
        <button type="button" className="agent-ask__btn" onClick={skipCurrent} disabled={submitting}>
          跳过
        </button>
        <button
          type="button"
          className="agent-ask__btn is-primary"
          onClick={() => goNext(drafts)}
          disabled={submitting}
        >
          {submitting ? '提交中…' : last ? '提交' : '下一题'}
        </button>
        <span className="agent-ask__note">Agent 正等着这条回答；不想答就跳过或点停止。</span>
      </div>
    </div>
  );
}

/* ── 权限确认卡片 ──
   写操作执行前，后端会挂起并推一条 permission_request；卡片就摆在那次工具调用下面
   （与 ask_user 同一套位置）。三个动作对应后端的三种答复：允许一次 / 本会话允许
   （只对这个工具、只在这个会话）/ 拒绝。**没有倒计时、也不会自动拒绝**：后端不设超时，
   不点就一直等着（刷新、切走再回来卡片都还在）；要收场就点三个按钮之一，或者点停止
   让整个回合收尾。拒绝会让模型收到一条"用户拒绝权限"的工具错误（可附你填的原因），
   它据此换策略——而不是把"没执行"当成"执行成功"。

   卡上还会带上「将要变更」：后端在挂起前就用同一套规则把 before→after 算好了
   （见后端的 _preview_tool_changes，只读、不落盘），写类工具（改译文数据、改项目配置、
   改问题过滤、写项目规范）因此不必先展开工具行的原始参数才敢点允许——你看到的就是这次
   要写下去的东西。算不出来（整文件删缓存、启动翻译、派子代理这些没有可比对的 diff）
   就没有这块，卡片只给摘要。 */

function PermissionCard({
  item,
  submitting,
  error,
  onDecide,
}: {
  item: ActivityItem;
  submitting: boolean;
  error: string | null;
  onDecide: (decision: PermissionDecision, reason?: string) => void;
}) {
  const perm = item.permission;
  // 拒绝原因（可选，输入框里那份）：只有点「拒绝」才送出去，会随那条工具结果一起给模型看。
  // 别和下面那个 `reason`（模型填在入参里的"为什么做这件事"）搞混，那个是只读展示用的。
  const [denyReason, setDenyReason] = useState('');
  if (!perm) return null;
  const meta = toolMeta(perm.name || item.name || '');
  const args = perm.arguments;
  const summary = meta.summary(args);
  const reason = typeof args?.reason === 'string' ? args.reason.trim() : '';
  const toolLabel = perm.label || meta.action;
  const editable = perm.risk === 'edit';
  // 徽标只留最要紧的几个字（会改什么），完整解释挪进 tooltip——照 PI-Desktop 那张卡：
  // 标题行一行说完，正文只留"允许什么 + 为什么"，说明性长句不再铺在卡面上。
  // 派子代理单独一档说法：它既不改设置、也不是改译文，但「允许编辑」档照样要问它
  // （见后端 PERMISSION_TOOL_RISK），归进"改设置 / 启动任务"会让人看不懂为什么要问。
  const riskKind: 'edit' | 'delegate' | 'high' =
    perm.name === 'run_subagents' ? 'delegate' : editable ? 'edit' : 'high';
  const riskLabel = { edit: '改译文数据', delegate: '派子代理', high: '改设置 / 启动任务' }[riskKind];
  const riskHint = {
    edit: '改动译文数据（缓存 / 字典 / 人名表）',
    delegate:
      '派一批子代理并行跑：每个都会调模型（校对子代理还会往缓存里写意见，原文探索会通读原文、很费 token）',
    high: '改动项目设置 / 规范，或启动翻译任务',
  }[riskKind];
  // 正文第二行的细节：参数摘要 + 当前档位（为什么现在要问）。都是短标签，逗号分不开的
  // 那种长句就省了——用户要的是"这次要动什么"，不是复述一遍权限模型。
  const detail = [PERMISSION_MODE_LABELS[normalizePermissionMode(perm.mode)], summary]
    .filter(Boolean)
    .join(' · ');
  // 「将要变更」（后端只读算出来的 diff）：认不出来就是没有——卡上不给空壳。
  const preview = extractChangeList(perm.preview);

  return (
    <section className="agent-perm" aria-label={`权限请求：${toolLabel}`}>
      <header className="agent-perm__head">
        <span className="agent-perm__icon"><Icon name="shield" /></span>
        <span className="agent-perm__title" role="status" aria-live="polite">{toolLabel}</span>
        <span
          className={`agent-perm__risk${editable ? ' is-edit' : ''}`}
          title={riskHint}
        >
          {riskLabel}
        </span>
      </header>
      <p className="agent-perm__lead">允许「{toolLabel}」运行吗？</p>
      <p className="agent-perm__meta">{detail}</p>
      {reason ? <p className="agent-perm__reason">原因：{reason}</p> : null}
      {/* 将要变更：摆在这一屏里而不是藏在工具行的展开里——用户要点的就是这个 */}
      {preview ? <ChangeListCard data={preview} title="将要变更" /> : null}
      {error ? <div className="agent-perm__error">{error}</div> : null}
      <div className="agent-perm__foot">
        <button
          type="button"
          className="agent-perm__btn is-primary"
          onClick={() => onDecide('allow-once')}
          disabled={submitting}
          title="只批准这一次调用"
        >
          允许一次
        </button>
        <button
          type="button"
          className="agent-perm__btn"
          onClick={() => onDecide('allow-session')}
          disabled={submitting}
          title={`本会话内不再询问「${toolLabel}」，会话结束即失效`}
        >
          本会话允许
        </button>
        <button
          type="button"
          className="agent-perm__btn"
          onClick={() => onDecide('deny', denyReason.trim())}
          disabled={submitting}
          title="这次调用不执行，Agent 会收到「用户拒绝」并换策略"
        >
          拒绝
        </button>
        {/* 拒绝原因（可选）：「不要」和「不要，因为 X」对模型是两回事——后者能让它
            直接换对方向，省掉一轮来回。留空就是单纯拒绝；填了按回车等于点「拒绝」。 */}
        <input
          type="text"
          className="agent-perm__reason-input"
          value={denyReason}
          onChange={(e) => setDenyReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || submitting) return;
            e.preventDefault();
            onDecide('deny', denyReason.trim());
          }}
          placeholder="拒绝原因（可选）"
          aria-label="拒绝原因（可选）"
          title="填了会在点「拒绝」时一起送给 Agent（显示在那次调用的结果里）"
          maxLength={500}
          disabled={submitting}
        />
      </div>
    </section>
  );
}

/* ── Tool row (disclosure, not a boxed card) ── */

/** 一批子代理：挂在发起它们的那次 run_subagents 调用下面，一行一个。

    参考 PI-Desktop 的 topology（一条主线 + 若干子行），这里只做一层——子代理没有子代理。
    每行能展开看它自己的工具调用与报告：跑着的时候默认展开（要看它在干什么），跑完自动收起
    （报告在行摘要的下一层，点开就能读）；用户手动点过就听用户的。 */
function SubagentList({ runs }: { runs: SubagentRun[] }) {
  const running = runs.filter((run) => run.status === 'running').length;
  const done = runs.filter((run) => run.status === 'done').length;
  const failed = runs.filter((run) => run.status === 'failed').length;
  const stopped = runs.length - running - done - failed;
  // 有在跑的就报"几个在跑"；全停了就按结局分账——中止/失败不算"已完成"
  let meta: string;
  if (running > 0) {
    meta = `${running}/${runs.length} 个在跑`;
  } else if (stopped > 0 || failed > 0) {
    const parts = [done > 0 ? `${done} 个完成` : '', failed > 0 ? `${failed} 个失败` : '', stopped > 0 ? `${stopped} 个中止` : ''];
    meta = parts.filter(Boolean).join('、');
  } else {
    meta = `${runs.length} 个已完成`;
  }
  return (
    <div className="agent-subagents">
      <div className="agent-subagents__head">
        <span className="agent-subagents__title">子代理</span>
        <span className="agent-subagents__meta">{meta}</span>
      </div>
      {runs.map((run) => (
        <SubagentRow key={run.id} run={run} />
      ))}
    </div>
  );
}

function SubagentRow({ run }: { run: SubagentRun }) {
  // **默认折叠**：一次派 16 个时是一行一个子代理，全铺开会把父行撑得很长；要看细节点开。
  // 折叠态不丢信息——"最新动作"就挂在行上（见 subagentLatest），在干什么一眼能扫到。
  const [open, setOpen] = useState(false);
  // 跑着的时候没有 duration_ms（那是完成时给的）：按起始时间 + 本地 tick 算。
  // 起始时间来自后端的 started_at，所以切页/刷新重建后不会归零；tick 保证没有新
  // 事件时秒数也在走（否则一行"5.0s"会一直不动，看着像卡住）。
  const running = run.status === 'running';
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [running]);
  const state = subagentState(run);
  const durationMs = run.durationMs ?? (run.startedAt ? now - run.startedAt : 0);
  const latest = subagentLatest(run);

  return (
    <div className={`agent-subagent is-${run.status}`}>
      <button
        type="button"
        className="agent-subagent__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-subagent__caret" aria-hidden>›</span>
        <span className="agent-subagent__label">{run.label}</span>
        <span className="agent-subagent__file" title={run.file}>
          {run.file}
          {run.indexes ? ` · ${run.indexes}` : ''}
        </span>
        {latest ? (
          <span
            className={`agent-subagent__latest${latest.tone === 'error' ? ' is-error' : ''}`}
            title={latest.full}
          >
            {latest.short}
          </span>
        ) : null}
        {run.toolCalls ? <span className="agent-subagent__badge">{run.toolCalls} 次工具</span> : null}
        {run.doubts ? <span className="agent-subagent__badge is-doubt">意见 {run.doubts}</span> : null}
        <span className={`agent-subagent__state is-${state.tone}`}>{state.label}</span>
        {run.retry ? (
          <span
            className="agent-subagent__badge is-retry"
            title={
              run.retry.reason
                ? `${run.retry.code || '请求失败'}：${run.retry.reason}`
                : '请求失败，正在自动重试'
            }
          >
            重试 {run.retry.attempt}/{run.retry.maxAttempts || '…'}
          </span>
        ) : null}
        {durationMs > 0 ? <span className="agent-subagent__time">{formatDuration(durationMs)}</span> : null}
      </button>
      {open ? (
        <div className="agent-subagent__body">
          {run.brief ? <div className="agent-subagent__brief">额外要求：{run.brief}</div> : null}
          {run.steps.map((step, i) =>
            step.kind === 'text' ? (
              <div key={`t-${i}`} className="agent-subagent__text">{step.text}</div>
            ) : (
              <SubagentStepRow key={step.id || `s-${i}`} step={step} />
            ),
          )}
          {run.report ? (
            <div className="agent-subagent__report">
              <span className="agent-subagent__report-label">报告</span>
              <span className="agent-subagent__report-text">{run.report}</span>
            </div>
          ) : null}
          {run.error ? <div className="agent-subagent__error">{run.error}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

/** 子代理写下的校对批注：从 patch_transl_cache 的入参里读（它写的就是 proofread_comment；
 *  历史会话里是旧名 doub_content，一并认）。 */
function stepDoubts(args: unknown): { index: number; text: string }[] {
  const patches = (args as Record<string, unknown> | undefined)?.patches;
  if (!Array.isArray(patches)) return [];
  return patches.flatMap((patch) => {
    const item = patch as Record<string, unknown> | undefined;
    const raw = item?.proofread_comment ?? item?.doub_content;
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) return [];
    const index = Number(item?.index);
    return [{ index: Number.isFinite(index) ? index : -1, text }];
  });
}

/** 折叠态显示的那句"最新动作"：最后一步是什么（说了什么 / 调了什么工具）。

    读类工具给参数摘要（读哪个文件、哪几行），写意见那步给条数；失败给错误首句。
    跑完且一步都没有（刷新过：逐步活动是瞬态的、不落盘）就退回报告首行，至少还剩一句总结。 */
function subagentLatest(run: SubagentRun): { short: string; full: string; tone?: 'error' } | null {
  const step = run.steps[run.steps.length - 1];
  if (step?.kind === 'text') {
    const text = step.text.trim().replace(/\s+/g, ' ');
    return text ? { short: clipText(text, 68), full: text } : null;
  }
  if (step?.kind === 'tool') {
    if (step.ok === false) {
      const error = (step.error || '失败').trim();
      return { short: `失败：${clipText(error, 40)}`, full: error, tone: 'error' };
    }
    const doubts = stepDoubts(step.args);
    if (step.name === 'patch_transl_cache' && doubts.length) {
      return {
        short: `写下 ${doubts.length} 条校对意见`,
        full: doubts.map((doubt) => `#${doubt.index} ${doubt.text}`).join('\n'),
      };
    }
    const meta = toolMeta(step.name);
    const detail = meta.summary(asArgs(step.args));
    return {
      short: detail ? `${meta.action} · ${detail}` : meta.action,
      full: detail ? `${meta.action}：${detail}` : meta.action,
    };
  }
  const report = (run.report || '').trim().replace(/\s+/g, ' ');
  return report ? { short: clipText(report, 68), full: report } : null;
}

/** 截断到 max 个字符，超出补省略号。 */
function clipText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** 子代理的一步工具调用。写意见那一步特殊处理：把每条意见都摊出来——那才是用户要看的产出，
    只显示"修改译文 · 3 条"等于让他自己去翻缓存文件。 */
function SubagentStepRow({ step }: { step: Extract<SubagentStep, { kind: 'tool' }> }) {
  const doubts = stepDoubts(step.args);
  if (step.name === 'patch_transl_cache' && doubts.length) {
    return (
      <div className="agent-subagent__step is-doubt">
        <span className="agent-subagent__step-name">写校对意见</span>
        <div className="agent-subagent__doubts">
          {doubts.map((doubt) => (
            <div key={doubt.index} className="agent-subagent__doubt">
              <span className="agent-subagent__doubt-index">#{doubt.index}</span>
              <span className="agent-subagent__doubt-text">{doubt.text}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className={`agent-subagent__step${step.ok === false ? ' is-error' : ''}`}>
      <span className="agent-subagent__step-name">{toolMeta(step.name).action}</span>
      <span className="agent-subagent__step-detail">
        {step.ok === false
          ? step.error || '失败'
          : toolMeta(step.name).summary(asArgs(step.args)) || '完成'}
      </span>
    </div>
  );
}

/** 子代理的状态标签与配色。 */
function subagentState(run: SubagentRun): {
  label: string;
  tone: 'running' | 'done' | 'error' | 'muted';
} {
  if (run.status === 'running') return { label: '进行中', tone: 'running' };
  if (run.status === 'done') return { label: '完成', tone: 'done' };
  if (run.status === 'failed') return { label: '失败', tone: 'error' };
  if (run.status === 'stopped') return { label: '已停止', tone: 'muted' };
  return { label: '轮数到顶', tone: 'muted' };
}

/** 工具行的"轮到哪一步"。一批工具调用在后端是**挨个执行**的（runtime 里
    `for tc in tool_calls`），但助手消息的 parts 会先把整批一次性画成行——所以"哪一行在跑"
    不能看是不是最后一行，得看谁还没有结果：

    - running：第一个还没有结果的行，正在执行；
    - awaiting：它卡在权限门禁上等用户批准（那张审批卡就画在这行下面）；
    - queued：排在它后面、还没轮到的（后端还没轮到它们，界面上不该显示成"进行中"）；
    - stale：不在运行中的组里却也没有结果——历史里断在半路的那次调用（进程被杀等）。

    返回数组与 items 下标对齐；非工具行、已有结果的行都是 undefined。 */
type ToolPhase = 'running' | 'awaiting' | 'queued' | 'stale';

function toolRowPhases(items: ActivityItem[], isLive: boolean): (ToolPhase | undefined)[] {
  const phases: (ToolPhase | undefined)[] = items.map(() => undefined);
  let isFirstPending = true;
  items.forEach((it, i) => {
    if (it.kind !== 'tool') return;
    if (it.ok !== undefined || it.result !== undefined || it.error !== undefined) return;
    if (!isLive) phases[i] = 'stale';
    else if (isFirstPending) phases[i] = it.permission ? 'awaiting' : 'running';
    else phases[i] = 'queued';
    isFirstPending = false;
  });
  return phases;
}

function ToolRow({
  item,
  phase,
  persistKey,
}: {
  item: ActivityItem;
  /** 这一行在本次执行里轮到哪一步（见 toolRowPhases） */
  phase?: ToolPhase;
  persistKey: string;
}) {
  const stateKey = `${persistKey}::tool-${item.id || item.step}`;

  // 写入类工具的变更卡片数据（后端在结果里带回）：
  // - changes: [{path, before, after, kind}] 键值级 before→after
  // - line_diff: {rows: [{op: add|del, line}], truncated} 行级 diff（save_dict）
  // - deleted_preview: [{index, text}] 被删条目（delete_transl_cache）
  const changeList = extractChangeList(item.result);
  // 写类工具的可选入参 reason（模型说明"为什么改"）：有变更卡就画在卡里，没有
  // （如 create_dict_file 的返回不含 changes）就在正文里单独给一行。
  const reason = extractReason(item.result);
  const ok = item.ok !== false;
  const pending = item.ok === undefined && item.result === undefined && !item.error;
  const awaiting = phase === 'awaiting';
  const isRunning = phase === 'running';
  const subagents = item.subagents ?? [];
  const hasSubagents = subagents.length > 0;
  // 正等批准时后端已经算好的「将要变更」（审批卡上摆的那份，见 PermissionCard）：
  // 有它就不必为了"它准备改什么"把这一行撑开去铺原始 JSON。
  const permPreview = awaiting ? extractChangeList(item.permission?.preview) : null;

  // 行**默认展开**的三种情形：
  // 1) 已有变更卡 —— 改了什么是这次调用的重点，diff 不该藏在一次点击后面；
  // 2) 有 reason 却没有变更卡（create_dict_file 之类不产生 changes）——理由也该直接可见；
  // 3) **正等着批准、但算不出 diff 的调用** —— 整文件删缓存、启动翻译、派子代理这类没有
  //    可比对的 before→after，用户要判断就只能看原始参数，那还是替它铺开（能算 diff 的
  //    都摆在审批卡上了，这一行不必再展开一次）。
  // manualOpenState 里只记"用户手动点过"的选择——记过就听用户的，没记过才用这个默认值
  // （重挂/刷新后同一规则）。
  const autoOpen = Boolean(changeList || reason) || hasSubagents || (awaiting && !permPreview);
  const [open, setOpenRaw] = useState(() => manualOpenState.get(stateKey) ?? autoOpen);
  const setOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };
  // 状态比行晚到（结果比行晚到、批准请求也可能晚一拍）：该展开了就展开。用户手动点过就不抢，
  // 否则会跟"刚点开又自己收起/展开"打架。
  const autoOpenedRef = useRef(autoOpen);
  useEffect(() => {
    if (!autoOpen || autoOpenedRef.current) return;
    autoOpenedRef.current = true;
    if (!manualOpenState.has(stateKey)) setOpenRaw(true);
  }, [autoOpen, stateKey]);

  const meta = toolMeta(item.name);
  const summary = meta.summary(asArgs(item.arguments));

  // 把原始参数/结果收进折叠菜单：写入调用要看的通常是 diff，JSON 参数与整份结果只是证据；
  // 派子代理的调用同理想——任务清单和各份报告已经在子代理行里逐条显示了，那串 JSON 是重复的。
  // 失败时例外：错误全文要直接可见（那时子代理行也未必建得起来）。
  const foldRaw = ok && (hasSubagents || (Boolean(changeList) && !pending));

  // wait 行：等待期间显示倒计时（只出秒数，不画进度条）。
  const isWait = item.name === 'wait';
  const waiting = isWait && pending && typeof item.waitTotalMs === 'number';
  const waitTotal = item.waitTotalMs || 0;
  const waitRemaining = item.waitRemainingMs ?? waitTotal;

  // 询问用户的结果：后端已经给出「问题：答案」的可读文本，转录里别再甩一坨 JSON
  const askSummary =
    item.name === 'ask_user' && ok
      ? str((item.result as Record<string, unknown> | undefined)?.summary)
      : '';
  const resultText = askSummary || formatPayload(ok ? item.result : item.error);
  const hasDetails = Boolean(summary || resultText || item.arguments || changeList);
  const longResult = resultText.length > 400;

  // 权限被拒不是故障，是用户的决定：状态标签说"已拒绝"，免得看着像系统出错。
  // 「用户没有在」是旧会话里"审批到点自动拒绝"留下的文案——现在没有超时了，留着这句
  // 只是为了让老转录仍显示成"已拒绝"而不是"失败"。
  const denied =
    !ok && typeof item.error === 'string' && /^(用户拒绝权限|用户没有在|回合被停止)/.test(item.error);
  // 状态标签以**这一行自己的进度**为准（见 toolRowPhases）：没有结果就不是"完成"。
  // 以前按"是不是最后一行"判断，整批里等在中间的那行会写成完成——最刺眼的就是
  // "还在等你批准，却已经显示完成"。排队中/未完成走默认的灰，不给"在跑"那种蓝色呼吸点。
  const state = waiting
    ? { label: formatCountdown(waitRemaining), tone: 'running', countdown: true }
    : awaiting
      ? { label: '待批准', tone: 'running', countdown: false }
      : isRunning
        ? { label: '进行中', tone: 'running', countdown: false }
        : phase === 'queued'
          ? { label: '排队中', tone: '', countdown: false }
          : phase === 'stale'
            ? { label: '未完成', tone: '', countdown: false }
            : isWait && item.waitInterrupted
              ? { label: '已中断', tone: '', countdown: false }
              : denied
                ? { label: '已拒绝', tone: 'error', countdown: false }
                : ok
                  ? { label: '完成', tone: 'done', countdown: false }
                  : { label: '失败', tone: 'error', countdown: false };

  return (
    <div className={`agent-tool${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="agent-tool__header"
        onClick={() => hasDetails && setOpen((v) => !v)}
        disabled={!hasDetails}
        aria-expanded={open}
      >
        <span className="agent-tool__icon"><Icon name={meta.icon} /></span>
        <span className={`agent-tool__name${state.tone === 'running' ? ' is-running' : ''}`}>{meta.action}</span>
        {summary ? <span className="agent-tool__summary">{summary}</span> : null}
        {changeList ? <span className="agent-tool__diffbadge">±{changeList.total}</span> : null}
        <span className={`agent-tool__state${state.tone ? ` is-${state.tone}` : ''}${state.countdown ? ' is-countdown' : ''}`}>
          <span className="agent-tool__state-dot" />
          {state.label}
        </span>
        <span className="agent-tool__caret">›</span>
      </button>
      {open ? (
        <div className="agent-tool__body">
          {hasSubagents ? <SubagentList runs={subagents} /> : null}
          {changeList ? (
            <ChangeListCard data={changeList} />
          ) : reason ? (
            <ChangeReason text={reason} />
          ) : null}
          {foldRaw ? (
            <RawToolData
              args={item.arguments}
              resultText={resultText}
              resultTitle={ok ? '结果' : '错误'}
              tone={ok ? 'default' : 'error'}
              durationMs={item.durationMs}
              truncate={longResult ? 1200 : 0}
            />
          ) : (
            <>
              {item.arguments !== undefined ? (
                <ToolBlock title="参数" content={formatPayload(item.arguments)} mono />
              ) : null}
              {resultText ? (
                <ToolBlock
                  title={ok ? '结果' : '错误'}
                  content={resultText}
                  mono
                  truncate={longResult ? 1200 : 0}
                  tone={ok ? 'default' : 'error'}
                  durationMs={item.durationMs}
                />
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** 参数 / 结果块：一行标题 + 一块等宽正文（内容被截断时给「展开全部」）。 */
function ToolBlock({
  title,
  content,
  mono,
  truncate = 0,
  tone = 'default',
  durationMs,
}: {
  title: string;
  content: string;
  mono?: boolean;
  truncate?: number;
  tone?: 'default' | 'error';
  durationMs?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const clipped = truncate > 0 && content.length > truncate && !expanded;
  const shown = clipped ? content.slice(0, truncate) + '…' : content;
  return (
    <div className={`agent-toolblock${tone === 'error' ? ' is-error' : ''}`}>
      <div className="agent-toolblock__head">
        <span className="agent-toolblock__title">{title}</span>
        {typeof durationMs === 'number' && title === '结果' ? (
          <span className="agent-toolblock__duration">{formatDuration(durationMs)}</span>
        ) : null}
      </div>
      <pre className={`agent-toolblock__pre${mono ? ' is-mono' : ''}`}>{shown}</pre>
      {truncate > 0 && content.length > truncate ? (
        <button type="button" className="agent-toolblock__toggle" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : `展开全部（${content.length} 字符）`}
        </button>
      ) : null}
    </div>
  );
}

/** 写入类调用的「原始参数/结果」：合成一个折叠菜单，默认折起。

    变更卡已经说清改了什么，原始 JSON 与整份结果只是证据——要看再展开。折成两行
    （参数一行、结果一行）点起来目标太小、还容易点错，合成一行开门更像"翻原始数据"；
    折叠态右侧给总字符数，一眼知道里面有多少东西。 */
function RawToolData({
  args,
  resultText,
  resultTitle,
  tone,
  durationMs,
  truncate,
}: {
  args: unknown;
  resultText: string;
  resultTitle: string;
  tone: 'default' | 'error';
  durationMs?: number;
  truncate?: number;
}) {
  const [open, setOpen] = useState(false);
  const argsText = args === undefined ? '' : formatPayload(args);
  if (!argsText && !resultText) return null;
  return (
    <div className="agent-rawdata">
      <button
        type="button"
        className="agent-toolblock__head is-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-toolblock__label">
          <span className="agent-toolblock__caret" aria-hidden>›</span>
          <span className="agent-toolblock__title">原始参数/结果</span>
        </span>
        <span className="agent-toolblock__meta">
          {open ? null : (
            <span className="agent-toolblock__len">{argsText.length + resultText.length} 字符</span>
          )}
        </span>
      </button>
      {open ? (
        <>
          {argsText ? <ToolBlock title="参数" content={argsText} mono /> : null}
          {resultText ? (
            <ToolBlock
              title={resultTitle}
              content={resultText}
              mono
              truncate={truncate}
              tone={tone}
              durationMs={durationMs}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/* ── 写入类工具的变更卡片（diff 风格） ──
   后端在写入结果里带回三类结构之一/组合：
   - changes: [{path, before, after, kind}] —— update_project_config、
     manage_problem_filter、patch_transl_cache、save_name_table
   - line_diff: {rows, truncated} —— save_dict 的整文本行级 diff
   - deleted_preview: [{index, text}] —— delete_transl_cache 被删条目

   审批卡上的「将要变更」用的是**同一份结构**（后端在挂起前只读算出来，见
   _preview_tool_changes），所以两处共用这一个渲染，没有第二套 diff 视图。 */

type ChangeEntry = { path: string; before?: unknown; after?: unknown; kind?: string };
type DiffRow = { op: 'add' | 'del'; line: string };

type ChangeData = {
  changes?: ChangeEntry[];
  line_diff?: { rows?: DiffRow[]; truncated?: boolean };
  deleted_preview?: { index: number; text: string }[];
  /** 模型填的「为什么改」（写类工具的可选入参 reason，后端原样回传） */
  reason?: string;
  total: number;
};

/** 写类工具结果里模型交代的原因（可选）。空/非字符串都当没填。 */
function extractReason(result: unknown): string {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return '';
  const value = (result as Record<string, unknown>).reason;
  return typeof value === 'string' ? value.trim() : '';
}

function extractChangeList(result: unknown): ChangeData | null {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
  const r = result as Record<string, unknown>;
  const changes = Array.isArray(r.changes) ? (r.changes as ChangeEntry[]) : undefined;
  const lineDiff =
    r.line_diff && typeof r.line_diff === 'object' && Array.isArray((r.line_diff as Record<string, unknown>).rows)
      ? (r.line_diff as { rows?: DiffRow[]; truncated?: boolean })
      : undefined;
  const deletedPreview = Array.isArray(r.deleted_preview) ? (r.deleted_preview as { index: number; text: string }[]) : undefined;
  const total =
    (changes?.length || 0) +
    (lineDiff?.rows?.length || 0) +
    (deletedPreview?.length || 0);
  if (!total) return null;
  return {
    changes,
    line_diff: lineDiff,
    deleted_preview: deletedPreview,
    reason: extractReason(result) || undefined,
    total,
  };
}

function fmtChangeValue(v: unknown): string {
  if (v === null || v === undefined) return '（空）';
  if (typeof v === 'string') return v.length > 80 ? v.slice(0, 77) + '…' : v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** 模型填的「为什么改」：变更卡顶部一行，没有变更卡时（如 create_dict_file 不产生
    changes）在工具正文里单独显示，不然填了原因却没地方看。 */
function ChangeReason({ text }: { text: string }) {
  return (
    <div className="agent-changes__reason">
      <span className="agent-changes__reason-label">原因</span>
      <span className="agent-changes__reason-text">{text}</span>
    </div>
  );
}

/** 变更卡。title 只有审批卡会换（那边的同一份数据是"将要变更"，还没真写）。 */
function ChangeListCard({ data, title = '变更' }: { data: ChangeData; title?: string }) {
  const rows: ReactNode[] = [];

  if (data.line_diff?.rows?.length) {
    // diff 全量渲染、不再折叠：容器限高 + 内部滚动（见 .agent-changes__body 的
    // max-height）。改了什么应当一眼看完，不该先点一次"展开全部"。
    const diffRows = data.line_diff.rows;
    for (const [i, r] of diffRows.entries()) {
      rows.push(
        <div key={`d-${i}`} className={`agent-changes__dline agent-changes__dline--${r.op}`}>
          <span className="agent-changes__sign">{r.op === 'add' ? '+' : '−'}</span>
          <span className="agent-changes__dtext">{r.line || ' '}</span>
        </div>,
      );
    }
    if (data.line_diff.truncated) {
      // 后端生成 diff 时就截断过（总行数上限），如实说明，不是界面的折叠
      rows.push(<div key="d-trunc" className="agent-changes__more">diff 过长已截断</div>);
    }
  }

  if (data.deleted_preview?.length) {
    for (const p of data.deleted_preview) {
      rows.push(
        <div key={`del-${p.index}`} className="agent-changes__dline agent-changes__dline--del">
          <span className="agent-changes__sign">−</span>
          <span className="agent-changes__path">#{p.index}</span>
          <span className="agent-changes__dtext">{p.text || '（空译文）'}</span>
        </div>,
      );
    }
  }

  if (data.changes?.length) {
    for (const [i, c] of data.changes.entries()) {
      rows.push(
        <div key={`c-${i}`} className="agent-changes__item">
          <span className="agent-changes__path">{c.path}</span>
          {c.kind !== 'add' ? (
            <span className="agent-changes__old">
              <span className="agent-changes__sign">−</span>
              {fmtChangeValue(c.before)}
            </span>
          ) : null}
          {c.kind !== 'remove' ? (
            <span className="agent-changes__new">
              <span className="agent-changes__sign">+</span>
              {fmtChangeValue(c.after)}
            </span>
          ) : null}
        </div>,
      );
    }
  }

  return (
    <div className="agent-changes">
      <div className="agent-changes__head">
        <span className="agent-changes__title">{title}</span>
        <span className="agent-changes__count">±{data.total}</span>
      </div>
      {data.reason ? <ChangeReason text={data.reason} /> : null}
      {rows.length ? <div className="agent-changes__body">{rows}</div> : null}
    </div>
  );
}

/* ── Terminal notices ── */

function ErrorNotice({ group }: { group: Extract<TimelineGroup, { type: 'error' }> }) {
  return (
    <div className="agent-notice agent-notice--error">
      <span className="agent-notice__icon"><Icon name="warning" /></span>
      <div className="agent-notice__body">
        <div className="agent-notice__title">执行出错</div>
        <div className="agent-notice__text">{group.message}</div>
      </div>
    </div>
  );
}

/** 回合停止：一条灰线 + 一行说明就够了，不用整块提示卡片（太重、还抢眼）。
 *  后端文案原样保留（runtime 不动）：只有"用户点停止"那条按界面口径显示成
 *  「用户已停止」，其他原因（如「立即」打断）原样展示。 */
function StoppedNotice({ group }: { group: Extract<TimelineGroup, { type: 'stopped' }> }) {
  const text = group.reason === '用户停止' ? '用户已停止' : group.reason;
  return (
    <div className="agent-stopped">
      <span className="agent-stopped__text">{text}</span>
    </div>
  );
}

function formatPayload(payload: unknown): string {
  if (payload === undefined || payload === null) return '';
  if (typeof payload === 'string') return payload;
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}
