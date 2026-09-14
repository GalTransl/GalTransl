import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import {
  addOpenProject,
  AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  encodeProjectDir,
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
  listAgentSessions,
  createAgentSession,
  deleteAgentSession,
  type AgentEvent,
  type AgentSession as AgentSessionMeta,
} from '../lib/api';
import { normalizeError } from '../lib/errors';
import { renderMarkdown } from '../lib/markdown';
import { formatProfileLabel } from '../lib/backendProfile';

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
]);

function persistedTranscriptEvents(events: AgentEvent[]): AgentEvent[] {
  return events.filter((event) => !TRANSIENT_EVENT_TYPES.has(event.type));
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
  // 流式 content/reasoning：正在收 delta、还没收到对应的 *_end
  streaming?: boolean;
  // 回合的收尾回复：渲染时提升为顶层普通消息（不折进活动组）
  final?: boolean;
  // wait tool: 倒计时快照
  waitTotalMs?: number;
  waitRemainingMs?: number;
  waitInterrupted?: boolean;
  // compact: 上下文压缩提示
  removed?: number;
  summaryChars?: number;
  // retry: LLM 请求失败自动重试（倒计时 + 第 N/M 次）
  attempt?: number;
  maxAttempts?: number;
  retryDelayMs?: number;
  retryStartedAtMs?: number;
  retryCode?: string;
  retryReason?: string;
  retryDone?: boolean;
};

type TimelineGroup =
  | { type: 'activity'; id: string; items: ActivityItem[]; finalContent?: ActivityItem }
  | { type: 'user'; id: string; step: number; message: string }
  | { type: 'error'; id: string; step: number; message: string; traceback?: string }
  | { type: 'stopped'; id: string; step: number; reason: string };

function buildTimeline(events: AgentEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = [];
  let current: Extract<TimelineGroup, { type: 'activity' }> | null = null;

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

  for (const ev of events) {
    if (ev.type === 'status' || ev.type === 'close') continue;

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

    // 思考流结束：卡片收尾（撤光标、记耗时）。只挂在已有卡片上，不新建
    // （恢复会话时 delta 是瞬态的、已不可回放，没有内容就没有卡片）。
    if (ev.type === 'reasoning_end') {
      endStreamKind('reasoning', ev.duration_ms);
      continue;
    }

    if (ev.type === 'tool_call') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const existing = ev.id ? current.items.find((it) => it.kind === 'tool' && it.id === ev.id) : undefined;
      if (existing) {
        existing.name = ev.name ?? existing.name;
        existing.arguments = ev.arguments;
      } else {
        current.items.push({
          kind: 'tool',
          step: ev.step,
          id: ev.id,
          name: ev.name,
          arguments: ev.arguments,
        });
      }
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

    // wait 工具的倒计时事件：挂到对应的 wait 工具行上，不单独成行。
    if (ev.type === 'wait_start' || ev.type === 'wait_tick' || ev.type === 'wait_end') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = current.items.find((it) => it.kind === 'tool' && it.name === 'wait');
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
      groups.push({ type: 'stopped', id: `s-${ev.step}`, step: ev.step, reason: ev.reason || '用户停止' });
    }
  }

  closeActivity();
  return groups;
}

/* ── Tool presentation ──
   Each backend tool maps to an action verb + icon + the most salient argument,
   so a row reads like "启动翻译 ForGal-json" rather than a raw function name. */

type ToolMeta = {
  action: string;
  running: string;
  icon: ReactNode;
  summary: (args: Record<string, unknown> | undefined) => string;
  verb: string;
};

const TOOL_META: Record<string, ToolMeta> = {
  get_project_overview: { action: '了解项目', running: '了解项目', verb: '', icon: '📂', summary: () => '读取项目概况' },
  update_project_config: { action: '修改项目配置', running: '修改项目配置', verb: '', icon: '🛠️', summary: () => '调整翻译参数/规范等设置' },
  list_input_files: { action: '查看原文文件清单', running: '查看原文文件清单', verb: '', icon: '🗃️', summary: () => '列出待翻译文件' },
  read_input_file: { action: '读取原文', running: '读取原文', verb: '', icon: '📄', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  read_guideline: { action: '读取翻译规范', running: '读取翻译规范', verb: '', icon: '📜', summary: (a) => str(a?.name) },
  list_dict_files: { action: '查看字典清单', running: '查看字典清单', verb: '', icon: '📚', summary: () => '列出项目字典文件' },
  read_dict: { action: '读取字典', running: '读取字典', verb: '', icon: '📖', summary: (a) => str(a?.file_key) },
  save_dict: { action: '保存字典', running: '保存字典', verb: '', icon: '💾', summary: (a) => str(a?.file_key) },
  create_dict_file: { action: '新建字典', running: '新建字典', verb: '', icon: '🗂️', summary: (a) => str(a?.filename) },
  get_name_table: { action: '读取人名表', running: '读取人名表', verb: '', icon: '👤', summary: () => 'name替换表' },
  save_name_table: { action: '保存人名表', running: '保存人名表', verb: '', icon: '👥', summary: (a) => (Array.isArray(a?.names) ? `${a.names.length} 条` : '') },
  start_translation: { action: '启动翻译', running: '启动翻译', verb: '', icon: '▶️', summary: (a) => [str(a?.translator), ...(Array.isArray(a?.files) ? [`仅 ${a.files.length} 个文件`] : [])].filter(Boolean).join(' · ') },
  stop_translation: { action: '停止翻译', running: '停止翻译', verb: '', icon: '⏹️', summary: () => '' },
  wait: { action: '等待', running: '等待中', verb: '', icon: '⏳', summary: (a) => waitSummary(a) },
  get_progress: { action: '查询进度', running: '查询进度', verb: '', icon: '📊', summary: () => '' },
  get_runtime: { action: '查询运行时', running: '查询运行时', verb: '', icon: '⚙️', summary: () => '' },
  list_problems: { action: '检查问题清单', running: '检查问题清单', verb: '', icon: '🔍', summary: (a) => str(a?.problem_type) || '问题类型统计' },
  manage_problem_filter: { action: '管理问题过滤', running: '管理问题过滤', verb: '', icon: '🧹', summary: (a) => [str(a?.action), Array.isArray(a?.keyword) ? a.keyword.map((k) => str(k)).join('、') : str(a?.keyword)].filter(Boolean).join(' · ') },
  list_transl_cache: { action: '查看缓存清单', running: '查看缓存清单', verb: '', icon: '🗃️', summary: () => '列出缓存文件' },
  read_transl_cache: { action: '读取缓存', running: '读取缓存', verb: '', icon: '📄', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  search_transl_cache: { action: '搜索缓存', running: '搜索缓存', verb: '', icon: '🔎', summary: (a) => str(a?.query) },
  patch_transl_cache: { action: '修改译文', running: '修改译文', verb: '', icon: '✏️', summary: (a) => (Array.isArray(a?.patches) ? `${a.patches.length} 条` : str(a?.filename)) },
  delete_transl_cache: { action: '删除缓存', running: '删除缓存', verb: '', icon: '🗑️', summary: (a) => [str(a?.filename), str(a?.indexes)].filter(Boolean).join(' · ') },
};

const DEFAULT_TOOL_META: ToolMeta = { action: '调用工具', running: '调用工具', verb: '', icon: '🔧', summary: () => '' };

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

/** wait 工具的参数摘要：把 seconds/minutes 归一成"等待 2 分钟"。 */
function waitSummary(args: Record<string, unknown> | undefined): string {
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const totalSeconds = num(args?.seconds) + num(args?.minutes) * 60;
  const reason = typeof args?.reason === 'string' ? args.reason.trim() : '';
  if (totalSeconds <= 0) return reason;
  const duration = totalSeconds % 60 === 0 && totalSeconds >= 60
    ? `${totalSeconds / 60} 分钟`
    : `${totalSeconds} 秒`;
  return reason ? `${duration} · ${reason}` : duration;
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

function formatDuration(ms: number | undefined): string {
  if (!ms || ms < 1000) return `${ms || 0}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/* ── Session sidebar ──
   一个项目下可以有多个会话；这里负责新建、切换、删除。
   标题目前由后端按"项目名+序号"生成。 */

function AgentSessionSidebar({
  sessionsByProject,
  projects,
  activeProject,
  activeSessionId,
  collapsed,
  disabled,
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
  onCreateBlank: () => void;
  onCreateInProject: (dir: string) => void;
  onToggleProject: (dir: string) => void;
  onSelectSession: (dir: string, sid: string) => void;
  onDeleteSession: (dir: string, session: AgentSessionMeta) => void;
}) {
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
            const list = sessionsByProject[dir] || [];
            const isCollapsed = collapsed[dir] ?? dir !== activeProject;
            const isGroupActive = dir === activeProject;
            const shortDir = shortName(dir);
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
                    <span className={`agent-sessions__caret${isCollapsed ? '' : ' is-open'}`}>▾</span>
                    <span className="agent-sessions__group-icon" aria-hidden>
                      {isCollapsed ? '📁' : '📂'}
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
                    disabled={disabled}
                    title={disabled ? 'Agent 运行中' : `在「${shortDir}」下新建会话`}
                    aria-label={`在 ${shortDir} 新建会话`}
                  >
                    ＋
                  </button>
                </div>
                <div className="agent-sessions__group-collapse">
                  <div className="agent-sessions__group-collapse-inner">
                    <div className="agent-sessions__group-list">
                      {list.length === 0 ? (
                        <div className="agent-sessions__group-empty">暂无会话</div>
                      ) : (
                        list.map((s) => (
                          <div
                            key={s.session_id}
                            className={`agent-session-item${
                              isGroupActive && s.session_id === activeSessionId ? ' is-active' : ''
                            }`}
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
                            <button
                              type="button"
                              className="agent-session-item__delete"
                              onClick={(e) => {
                                e.stopPropagation();
                                onDeleteSession(dir, s);
                              }}
                              disabled={disabled}
                              title="删除该会话"
                              aria-label={`删除会话 ${s.title}`}
                            >
                              ✕
                            </button>
                          </div>
                        ))
                      )}
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

function formatSessionTime(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return `${hh}:${mm}`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
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

  // 跟随 Agent 默认后端配置：默认标签变化时同步过来；但用户在⚙下拉里
  // 临时改过的本次会话不再覆盖（profileTouchedRef），切会话时重置该标记。
  useEffect(() => {
    const sync = (e: Event) => {
      if (profileTouchedRef.current) return;
      const next = (e as CustomEvent<string>).detail || '';
      if (next) setBackendProfileName(next);
    };
    window.addEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync as EventListener);
    // 进入页面时也对齐一次当前 Agent 默认（若本次会话还没临时改过）
    if (!profileTouchedRef.current) {
      const cur = getAgentDefaultBackendProfile();
      if (cur) setBackendProfileName(cur);
    }
    return () => window.removeEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync as EventListener);
  }, []);

  const [projectDir, setProjectDir] = useState<string>(() => projectOptions[0] || '');
  const [configFileName, setConfigFileName] = useState<string>(() =>
    projectOptions[0] ? readConfigFileName(projectOptions[0]) : 'config.yaml',
  );
  const [backendProfileNames] = useState<string[]>(() => getBackendProfileNames());
  const [backendProfileName, setBackendProfileName] = useState<string>(
    () => getAgentDefaultBackendProfile() || getBackendProfileNames()[0] || '',
  );
  const [goal, setGoal] = useState('');

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<string>('idle');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // 界面上的「发送中」乐观态：消息已发出但后端尚未确认
  const [sending, setSending] = useState(false);
  // 会话列表按项目分组：projectDir -> 该项目的会话列表
  const [sessionsByProject, setSessionsByProject] = useState<Record<string, AgentSessionMeta[]>>({});
  // 侧边栏每个项目分组的折叠态（默认当前活动项目展开，其余折叠）
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const first = projectOptions[0];
    return first ? loadActiveSessionId(first) : '';
  });
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
  const startRef = useRef(0);
  // 本地已见的最大事件 step（SSE 续订的 after_step 起点 + 兜底去重）。
  // 状态快照对账/持久化恢复时同步更新。
  const lastStepRef = useRef(0);
  // 是否已在后端建立会话（首条消息 startAgent 成功后置 true；reset 清空）。
  // 不能用 events.length 判断：乐观追加后它立即 >0，但会话可能还没建好。
  const hasBackendSessionRef = useRef(false);
  // 用户在⚙下拉里临时改过后端配置，则本次会话内 Agent 默认标签的变更不再覆盖。
  // 新建/切换会话时重置，恢复跟随 Agent 默认。
  const profileTouchedRef = useRef(false);
  // 从 hero 选择项目 / 顶部＋新建空会话：切项目后不应自动加载该项目上次
  // 记忆的会话，而要保持空态等用户发消息创建新会话。置位后项目 effect
  // 会把 activeSessionId 清空而非取 remembered，随后清掉一次性标志。
  const skipRememberedSessionRef = useRef(false);
  // 当前激活的会话 id，供回调读取（避免闭包读到旧值）
  const activeSessionRef = useRef('');
  const statusSyncVersionRef = useRef(0);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
    // 切会话时重置"已临时改过"标记，让后端配置回到跟随 Agent 默认
    profileTouchedRef.current = false;
  }, [activeSessionId]);
  // 当前活动项目，供回调读取（refreshSessions 判断是否接管 activeSessionId）
  const effectiveProjectRef = useRef(projectDir);
  useEffect(() => {
    effectiveProjectRef.current = projectDir;
  }, [projectDir]);

  const effectiveProject = projectDir;

  /** 拉取某项目的会话列表并写进按项目分组的 map（不动其他项目的会话）。
   *  preferredId 命中则切到该会话；allowAutoPick 为真（默认）且列表非空时取第一个，
   *  否则保持现状（hero 选项目等空态场景不自动加载历史会话）。 */
  const refreshSessions = useCallback(
    async (dir: string, preferredId?: string, allowAutoPick = true): Promise<AgentSessionMeta[]> => {
      try {
        const list = await listAgentSessions(dir);
        setSessionsByProject((prev) => ({ ...prev, [dir]: list }));
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
      .then((snap) => {
        if (cancelled || syncVersion !== statusSyncVersionRef.current) return;
        if (sendingRef.current && activeSessionId === sendTransitionRef.current) return;
        // 后端是权威来源：新会话后端空（events=0）时必须覆盖本地可能残留的
        // 旧缓存，否则会错把别的会话的内容糊在新建会话上。
        const snapEvents = snap.events || [];
        const snapLen = snapEvents.length;
        if (snapLen === 0) {
          // 新建空会话：后端空快照应清掉可能残留的本地缓存。
          setEvents([]);
          lastStepRef.current = 0;
          hasBackendSessionRef.current = false;
        } else {
          // 本地缓存上限为 600、后端内存窗口为 500，不能再用数组长度判断
          // 谁“更权威”。按 step 合并，既保留本地较早历史，也接纳后端恢复时
          // 补出的首条 user_message。
          const mergedEvents = mergeTranscriptEvents(persisted?.events || [], snapEvents);
          setEvents(mergedEvents);
          lastStepRef.current = maxStep(mergedEvents);
          hasBackendSessionRef.current = true;
        }
        const snapRunning = snap.status === 'running';
        setStatus(snap.status);
        setRunning(snapRunning);
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
  }, [events]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < 80;
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
          // status 快照绝不主动 abort：订阅时回合可能已结束（首帧即终态），
          // 但事件还在流里没发完，掐流会吞掉全部内容。流的关闭交给 close 帧
          // 或 finish/stopped/error 事件。
          if (ev.status && isTerminal(ev.status)) {
            setRunning(false);
          }
          return;
        }
        // 兜底去重：SSE 重放/竞态下 step 已见过的事件直接丢弃
        // （本地乐观的 user_message 用 step=-1，不参与该判断）
        if (typeof ev.step === 'number' && ev.step >= 0) {
          if (ev.step <= lastStepRef.current) return;
          lastStepRef.current = ev.step;
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
          setRunning(false);
          // 不主动 abort：后端若因滞留插话自动开 followup 回合，
          // 同一条流会继续推后续事件；流的关闭由后端 close 帧决定。
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
    if (!effectiveProject) {
      setError('请先选择一个项目');
      return;
    }
    const profile = getBackendProfile(backendProfileName);
    if (!profile) {
      setError('请先选择一个翻译后端配置（并在「翻译后端配置」页填写 token/模型）');
      setSettingsOpen(true);
      return;
    }

    // 像聊天一样：发送时本地立刻把这条消息显示成气泡，不等后端确认
    const localId = `local:${text}`;
    localMsgIdsRef.current.add(localId);
    setEvents((prev) => [
      ...prev,
      { type: 'user_message', step: -1, message: text },
    ]);
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
      }
      if (!hasBackendSessionRef.current) {
        // 空会话：第一条消息启动首个回合
        const snap = await startAgent({
          project_dir: effectiveProject,
          config_file_name: configFileName || 'config.yaml',
          backend_profile_data: profile,
          goal: text,
          session_id: sid,
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
        // 已有会话：运行中→插话排队；已结束→同会话继续下一回合
        await sendAgentMessage(effectiveProject, text, sid);
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
  }, [effectiveProject, backendProfileName, configFileName, goal, subscribeStream, refreshSessions]);

  const handleStop = useCallback(async () => {
    if (!effectiveProject) return;
    try {
      await stopAgent(effectiveProject, activeSessionRef.current || undefined);
      setStatus('stopped');
      setRunning(false);
      abortRef.current?.();
      abortRef.current = null;
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
        }
      } catch (err) {
        setError(normalizeError(err, '新建会话失败'));
      }
    },
    [running, handleStop],
  );

  /** 折叠/展开某项目分组（手风琴外的自由切换：点击只翻转这一个）。 */
  const handleToggleProject = useCallback((dir: string) => {
    setCollapsedProjects((prev) => ({ ...prev, [dir]: !prev[dir] }));
  }, []);

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
  const stepCount = useMemo(
    () =>
      timeline.reduce(
        (n, g) =>
          n +
          (g.type === 'activity'
            ? g.items.filter((it) => it.kind !== 'compact').length
            : 1),
        0,
      ),
    [timeline],
  );
  const hasSession = events.length > 0;
  const canSend = Boolean(projectDir) && Boolean(backendProfileName) && goal.trim().length > 0 && !sending;
  const activeTitle =
    (sessionsByProject[effectiveProject] || []).find((s) => s.session_id === activeSessionId)?.title || '';
  // 展示「后端配置文件名/模型名」：模型名从当前配置里取，与「翻译后端配置」页同一口径
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
        onCreateBlank={() => void handleCreateBlankSession()}
        onCreateInProject={(dir) => void handleCreateSessionInProject(dir)}
        onToggleProject={handleToggleProject}
        onSelectSession={handleSelectSession}
        onDeleteSession={(dir, s) => void handleDeleteSession(dir, s)}
      />
      <div className="agent-console__main agent-cockpit">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar" aria-hidden>🤖</span>
          <div className="agent-console__bar-copy">
            <div className="agent-console__bar-title">
              <span className="agent-console__bar-name">翻译 Agent</span>
              {activeTitle ? <span className="agent-console__bar-session">{activeTitle}</span> : null}
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
          <StatusPill status={status} running={running} />
        </div>

        <div className="agent-console__bar-right">
          <span className="agent-console__metric" title="已记录步骤">
            <span className="agent-console__metric-value">{stepCount}</span>
            <span className="agent-console__metric-label">步数</span>
          </span>
          {backendProfileName ? (
            <span className="agent-console__chip" title="翻译后端配置">⚙ {backendProfileName}</span>
          ) : (
            <span className="agent-console__chip agent-console__chip--warn">未配置后端</span>
          )}
          <button
            type="button"
            className={`agent-console__icon-btn${settingsOpen ? ' is-active' : ''}`}
            onClick={() => setSettingsOpen((v) => !v)}
            title="运行设置"
            aria-expanded={settingsOpen}
          >
            ⚙
          </button>
          <button
            type="button"
            className="agent-console__icon-btn"
            onClick={() => void handleClear()}
            disabled={running || !events.length}
            title="重置会话（清空全部对话与后端历史）"
          >
            🗑
          </button>
        </div>
      </header>

      {settingsOpen ? (
        <div className="agent-settings">
          <label className="agent-settings__field">
            <span>配置文件</span>
            <input
              type="text"
              value={configFileName}
              onChange={(e) => setConfigFileName(e.target.value)}
              disabled={running}
            />
          </label>
          <label className="agent-settings__field">
            <span>翻译后端配置</span>
            <select
              value={backendProfileName}
              onChange={(e) => {
                setBackendProfileName(e.target.value);
                profileTouchedRef.current = true;
              }}
              disabled={running}
            >
              {backendProfileNames.length === 0 ? (
                <option value="">（未配置，请在「翻译后端配置」页添加）</option>
              ) : (
                backendProfileNames.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))
              )}
            </select>
          </label>
          {projectDir ? (
            <button
              type="button"
              className="agent-settings__link"
              onClick={() => navigate(`/project/${encodeProjectDir(projectDir)}/translate`)}
            >
              在工作台查看该项目 →
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="agent-console__thread" ref={scrollRef} onScroll={handleScroll}>
        <div className="agent-thread">
          {timeline.length === 0 ? (
            <div className="agent-hero">
              <div className="agent-hero__mark">🤖</div>
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
                      {projectOptions.map((dir) => (
                        <button
                          key={dir}
                          type="button"
                          className="agent-hero__project-chip"
                          onClick={() => chooseProject(dir)}
                          title={dir}
                        >
                          <span className="agent-hero__project-chip-icon">📁</span>
                          <span className="agent-hero__project-chip-name">{shortName(dir)}</span>
                        </button>
                      ))}
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
                    📂 打开项目
                  </button>
                  <button
                    type="button"
                    className="agent-hero__action agent-hero__action--secondary"
                    onClick={() => navigate('/new-project')}
                    title="新建项目向导"
                  >
                    ✨ 新建项目
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <>
              {timeline.map((group, index) => (
                <AgentGroupView
                  key={group.id}
                  group={group}
                  isLive={running && index === timeline.length - 1}
                />
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
              <span className="agent-notice__icon">⚠</span>
              <div className="agent-notice__body">
                <div className="agent-notice__title">{error}</div>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="agent-console__composer">
        <div className={`agent-composer${running ? ' is-running' : ''}`}>
          <textarea
            className="agent-composer__input"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder={
              running
                ? 'Agent 正在工作中，输入消息插话或留空等待；也可以点右侧 ■ 停止。'
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
              <span className="agent-composer__chip agent-composer__chip--static" title={projectDir || '未选择项目'}>
                <span className="agent-composer__chip-icon">📁</span>
                <span className="agent-composer__chip-label">{projectDir ? shortName(projectDir) : '未选择项目'}</span>
              </span>
              <button
                type="button"
                className="agent-composer__chip"
                onClick={() => setSettingsOpen((v) => !v)}
                title={backendProfileLabel ? `${backendProfileLabel} · 点击打开模型与配置` : '模型与配置'}
              >
                <span className="agent-composer__chip-icon">⚙</span>
                <span className="agent-composer__chip-label">{backendProfileLabel || '未配置后端'}</span>
              </button>
            </div>
            <div className="agent-composer__right">
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
                    ↑
                  </button>
                  <button type="button" className="agent-composer__stop" onClick={handleStop} title="停止 Agent">
                    <span className="agent-composer__stop-icon" />
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
                  ↑
                </button>
              )}
            </div>
          </div>
        </div>
        {!projectDir ? (
          <div className="agent-composer__hint">先选择一个项目再启动 Agent</div>
        ) : !backendProfileName ? (
          <div className="agent-composer__hint agent-composer__hint--warn">
            尚未选择翻译后端配置，点击上方 ⚙ 设置
          </div>
        ) : running ? null : hasSession ? (
          <div className="agent-composer__hint">会话保留中 · 发送消息即可继续，🗑 可重置</div>
        ) : null}
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
    return `${toolMeta(item.name).running}…`;
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
    <span className={`agent-status-pill agent-status-pill--${tone}`}>
      <span className="agent-status-pill__dot" />
      {label}
    </span>
  );
}

/* ── Activity group (thinking + tool calls collapsed into one row) ── */

function AgentGroupView({ group, isLive }: { group: TimelineGroup; isLive: boolean }) {
  // Terminal groups render as notices and hold no disclosure state; dispatch
  // them before the activity component so its hooks never run conditionally.
  if (group.type === 'user') return <UserMessageRow message={group.message} />;
  if (group.type === 'error') return <ErrorNotice group={group} />;
  if (group.type === 'stopped') return <StoppedNotice group={group} />;
  if (group.type === 'activity' && group.finalContent) {
    return (
      <>
        {group.items.length > 0 ? <AgentActivityGroup group={group} isLive={isLive} /> : null}
        <FinalMessage item={group.finalContent} />
      </>
    );
  }
  return <AgentActivityGroup group={group} isLive={isLive} />;
}

/** 回合收尾回复：顶层普通消息，像聊天里最后一条回答。 */
function FinalMessage({ item }: { item: ActivityItem }) {
  return (
    <div className="agent-final agent-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(item.content || '') }} />
  );
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
}: {
  group: Extract<TimelineGroup, { type: 'activity' }>;
  isLive: boolean;
}) {
  const [open, setOpen] = useState(isLive);
  const userToggledRef = useRef(false);
  const items = group.items;

  // Follow the live run: auto-expand while working, auto-collapse when settled,
  // unless the user took manual control of this group.
  useEffect(() => {
    if (userToggledRef.current) return;
    setOpen(isLive);
  }, [isLive]);

  // 运行中墙钟计时（对标 PI-Desktop）：live 时每秒跳动，结束冻结在最后值。
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
  // 压缩提示不算"工作步骤"，避免污染耗时与步数统计
  const visibleCount = items.filter((it) => it.kind !== 'compact').length;

  // 文案对齐 PI-Desktop zh-CN：运行中「思考中/处理中 · Ns」，结束「已思考/已处理 Ns」
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

  return (
    <div className={`agent-activity${open ? ' is-open' : ''}${isLive ? ' is-live' : ''}`}>
      <button
        type="button"
        className="agent-activity__header"
        onClick={() => {
          userToggledRef.current = true;
          setOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-activity__icon">✳</span>
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
                <ContentRow key={`t-${i}`} item={item} />
              ) : item.kind === 'reasoning' ? (
                <ReasoningRow key={`r-${i}`} item={item} />
              ) : item.kind === 'compact' ? (
                <CompactRow key={`c-${i}`} item={item} />
              ) : item.kind === 'retry' ? (
                <RetryRow key={`rt-${i}`} item={item} />
              ) : (
                <ToolRow key={`x-${item.id || i}`} item={item} live={isLive && i === items.length - 1} />
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
      // 取思考文本最后一行（对标 PI-Desktop）：折叠头部读起来像实时跑马灯
      const lines = it.content
        .split('\n')
        .map((line) => line.replace(/^#+\s*|\*\*/g, '').trim())
        .filter(Boolean);
      return lines[lines.length - 1] || '';
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

function ContentRow({ item }: { item: ActivityItem }) {
  // 模型「说」的回复：直接渲染为普通黑体纯文本，不再用可折叠卡片包裹。
  const text = item.content || '';
  const streaming = Boolean(item.streaming);

  return (
    <div className={`agent-content${streaming ? ' is-streaming' : ''}`}>
      <div
        className="agent-content__text agent-md"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(text, { cursor: streaming }) }}
      />
    </div>
  );
}

/* ── Reasoning row（模型「想」的思考过程）──
   与「说」分开：想用可折叠卡片，流式时展开实时滚动，结束后自动收起
   成「已思考 Ns」一行；用户手动展开/收起后不再被自动行为覆盖。 */

function ReasoningRow({ item }: { item: ActivityItem }) {
  const streaming = Boolean(item.streaming);
  const [open, setOpen] = useState(streaming);
  const userToggledRef = useRef(false);

  // 跟随思考流：来增量时展开，结束收起（除非用户接管了这张卡片）
  useEffect(() => {
    if (userToggledRef.current) return;
    setOpen(streaming);
  }, [streaming]);

  const text = item.content || '';
  const label = streaming ? '思考中' : item.durationMs ? `已思考 ${formatDuration(item.durationMs)}` : '已思考';

  return (
    <div className={`agent-reasoning${open ? ' is-open' : ''}${streaming ? ' is-streaming' : ''}`}>
      <button
        type="button"
        className="agent-reasoning__header"
        onClick={() => {
          userToggledRef.current = true;
          setOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-reasoning__icon">✳</span>
        <span className={`agent-reasoning__label${streaming ? ' is-running' : ''}`}>{label}</span>
        <span className="agent-reasoning__caret">›</span>
      </button>
      <div className="agent-reasoning__collapse">
        <div className="agent-reasoning__collapse-inner">
          <div
            className="agent-reasoning__text agent-md"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(text, { cursor: streaming }) }}
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
  const tokens = item.summaryChars ? Math.round(item.summaryChars / 4) : 0;
  return (
    <div className="agent-compact-note" title="早期对话已被摘要压缩，以腾出上下文空间">
      <span className="agent-compact-note__icon">🗜</span>
      <span className="agent-compact-note__text">
        已压缩上下文 · 摘要 {removed} 条早期消息
        {tokens > 0 ? `（约 ${tokens} 字）` : ''}
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
      <span className="agent-retry-note__icon">↻</span>
      <span className="agent-retry-note__text">
        {live ? `${cause}，${remainingSec} 秒后重试` : '已重试'}
        <span className="agent-retry-note__count"> · {attemptText}</span>
      </span>
    </div>
  );
}

/* ── Tool row (disclosure, not a boxed card) ── */

function ToolRow({ item, live }: { item: ActivityItem; live: boolean }) {
  const [open, setOpen] = useState(false);
  const meta = toolMeta(item.name);
  const summary = meta.summary(asArgs(item.arguments));
  const ok = item.ok !== false;
  const pending = item.ok === undefined && item.result === undefined && !item.error;
  const isRunning = live && pending;

  // 写入类工具的变更卡片数据（后端在结果里带回）：
  // - changes: [{path, before, after, kind}] 键值级 before→after
  // - line_diff: {rows: [{op: add|del, line}], truncated} 行级 diff（save_dict）
  // - deleted_preview: [{index, text}] 被删条目（delete_transl_cache）
  const changeList = extractChangeList(item.result);

  // wait 行：等待期间显示倒计时进度条。
  const isWait = item.name === 'wait';
  const waiting = isWait && pending && typeof item.waitTotalMs === 'number';
  const waitTotal = item.waitTotalMs || 0;
  const waitRemaining = item.waitRemainingMs ?? waitTotal;
  const waitRatio = waitTotal > 0 ? Math.min(1, Math.max(0, 1 - waitRemaining / waitTotal)) : 0;

  const resultText = formatPayload(ok ? item.result : item.error);
  const hasDetails = Boolean(summary || resultText || item.arguments || changeList);
  const longResult = resultText.length > 400;

  const stateLabel = waiting
    ? formatCountdown(waitRemaining)
    : isRunning
      ? '进行中'
      : isWait && item.waitInterrupted
        ? '已中断'
        : ok
          ? '完成'
          : '失败';
  const stateTone = waiting || isRunning ? 'running' : ok ? 'done' : 'error';

  return (
    <div className={`agent-tool${open ? ' is-open' : ''}${waiting ? ' is-waiting' : ''}`}>
      <button
        type="button"
        className="agent-tool__header"
        onClick={() => hasDetails && setOpen((v) => !v)}
        disabled={!hasDetails}
        aria-expanded={open}
      >
        <span className="agent-tool__icon">{meta.icon}</span>
        <span className={`agent-tool__name${waiting || isRunning ? ' is-running' : ''}`}>{meta.action}</span>
        {summary ? <span className="agent-tool__summary">{summary}</span> : null}
        {changeList ? <span className="agent-tool__diffbadge">±{changeList.total}</span> : null}
        <span className={`agent-tool__state is-${stateTone}${waiting ? ' is-countdown' : ''}`}>
          <span className="agent-tool__state-dot" />
          {stateLabel}
        </span>
        <span className="agent-tool__caret">›</span>
      </button>
      {waiting && waitTotal > 0 ? (
        <div className="agent-tool__waitbar" aria-hidden>
          <div className="agent-tool__waitbar-fill" style={{ width: `${Math.round(waitRatio * 100)}%` }} />
        </div>
      ) : null}
      {open ? (
        <div className="agent-tool__body">
          {changeList ? <ChangeListCard data={changeList} /> : null}
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
        </div>
      ) : null}
    </div>
  );
}

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

/* ── 写入类工具的变更卡片（diff 风格） ──
   后端在写入结果里带回三类结构之一/组合：
   - changes: [{path, before, after, kind}] —— update_project_config、
     manage_problem_filter、patch_transl_cache、save_name_table
   - line_diff: {rows, truncated} —— save_dict 的整文本行级 diff
   - deleted_preview: [{index, text}] —— delete_transl_cache 被删条目 */

type ChangeEntry = { path: string; before?: unknown; after?: unknown; kind?: string };
type DiffRow = { op: 'add' | 'del'; line: string };

type ChangeData = {
  changes?: ChangeEntry[];
  line_diff?: { rows?: DiffRow[]; truncated?: boolean };
  deleted_preview?: { index: number; text: string }[];
  total: number;
};

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
  return { changes, line_diff: lineDiff, deleted_preview: deletedPreview, total };
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

function ChangeListCard({ data }: { data: ChangeData }) {
  const [showAll, setShowAll] = useState(false);
  const rows: ReactNode[] = [];

  if (data.line_diff?.rows?.length) {
    const diffRows = data.line_diff.rows;
    const shown = showAll ? diffRows : diffRows.slice(0, 40);
    for (const [i, r] of shown.entries()) {
      rows.push(
        <div key={`d-${i}`} className={`agent-changes__dline agent-changes__dline--${r.op}`}>
          <span className="agent-changes__sign">{r.op === 'add' ? '+' : '−'}</span>
          <span className="agent-changes__dtext">{r.line || ' '}</span>
        </div>,
      );
    }
    if (data.line_diff.truncated && showAll) {
      rows.push(<div key="d-trunc" className="agent-changes__more">diff 过长已截断</div>);
    }
    if (diffRows.length > 40 && !showAll) {
      rows.push(
        <button key="d-more" type="button" className="agent-changes__morebtn" onClick={() => setShowAll(true)}>
          展开全部 {diffRows.length} 行 diff
        </button>,
      );
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
        <span className="agent-changes__title">变更</span>
        <span className="agent-changes__count">±{data.total}</span>
      </div>
      {rows.length ? <div className="agent-changes__body">{rows}</div> : null}
    </div>
  );
}

/* ── Terminal notices ── */

function ErrorNotice({ group }: { group: Extract<TimelineGroup, { type: 'error' }> }) {
  return (
    <div className="agent-notice agent-notice--error">
      <span className="agent-notice__icon">⚠</span>
      <div className="agent-notice__body">
        <div className="agent-notice__title">执行出错</div>
        <div className="agent-notice__text">{group.message}</div>
      </div>
    </div>
  );
}

function StoppedNotice({ group }: { group: Extract<TimelineGroup, { type: 'stopped' }> }) {
  return (
    <div className="agent-notice agent-notice--stopped">
      <span className="agent-notice__icon">⏹</span>
      <div className="agent-notice__body">
        <div className="agent-notice__title">已停止</div>
        <div className="agent-notice__text">{group.reason}</div>
      </div>
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
