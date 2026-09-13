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
    return parsed;
  } catch {
    return null;
  }
}

function saveSession(session: TranscriptSession): void {
  if (!session.sessionId) return;
  try {
    // Bound the payload: keep the tail of very long transcripts.
    const events = session.events.length > 600 ? session.events.slice(-600) : session.events;
    localStorage.setItem(sessionsKey(session.projectDir, session.sessionId), JSON.stringify({ ...session, events }));
  } catch {
    // Quota or serialization failure is non-fatal.
  }
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
  kind: 'thought' | 'tool' | 'compact';
  step: number;
  content?: string;
  id?: string;
  name?: string;
  arguments?: unknown;
  ok?: boolean;
  result?: unknown;
  error?: string;
  durationMs?: number;
  // 流式 thought：正在收 delta、还没收到 thought_end
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
};

type TimelineGroup =
  | { type: 'activity'; id: string; items: ActivityItem[]; finalThought?: ActivityItem }
  | { type: 'user'; id: string; step: number; message: string }
  | { type: 'error'; id: string; step: number; message: string; traceback?: string }
  | { type: 'stopped'; id: string; step: number; reason: string };

function buildTimeline(events: AgentEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = [];
  let current: Extract<TimelineGroup, { type: 'activity' }> | null = null;

  const closeActivity = () => {
    // finalThought 也算有效内容：纯文字回复的回合里 items 会被清空
    // （finish 把同文的流式 thought 移出折叠区），只剩 finalThought 也要入组。
    if (current && (current.items.length || current.finalThought)) groups.push(current);
    current = null;
  };

  for (const ev of events) {
    if (ev.type === 'status' || ev.type === 'close') continue;

    // 用户消息独立成行（右对齐气泡），并打断当前活动组。
    if (ev.type === 'user_message') {
      closeActivity();
      groups.push({ type: 'user', id: `u-${ev.step}`, step: ev.step, message: ev.message || '' });
      continue;
    }

    if (ev.type === 'thought') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({ kind: 'thought', step: ev.step, content: ev.content });
      continue;
    }

    // 流式思考增量：拼到活动组里最后一条流式 thought 上（打字机效果）。
    // 没有可拼接的 thought（比如恢复会话时第一事件就是 delta）时新起一条。
    if (ev.type === 'thought_delta') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const last = current.items[current.items.length - 1];
      if (last && last.kind === 'thought' && last.streaming) {
        last.content = (last.content || '') + (ev.delta || '');
      } else {
        current.items.push({
          kind: 'thought',
          step: ev.step,
          content: ev.delta || '',
          streaming: true,
        });
      }
      continue;
    }

    // 一段流式文本结束：撤掉打字机光标
    if (ev.type === 'thought_end') {
      const last = current?.items[current.items.length - 1];
      if (last && last.kind === 'thought') last.streaming = false;
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

    // finish 是回合的收尾回复：并入当前活动组作为普通"回复"项，
    // finish 是回合的收尾回复：作为活动组的 final 消息，渲染时提升为
    // 顶层普通文本（不折进折叠区），像对话里最后一条普通消息。
    if (ev.type === 'finish') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const summary = ev.summary || '';
      const last = current.items[current.items.length - 1];
      // 无工具调用的收尾：流式期间已展示同一段文本，把它直接转为 final，
      // 从折叠 items 里移出（避免既在组内折叠区又在顶层出现两次）。
      if (last && last.kind === 'thought' && !last.streaming && last.content === summary) {
        current.items.pop();
      }
      if (summary) {
        current.finalThought = { kind: 'thought', step: ev.step, content: summary, final: true };
      }
      closeActivity(); // 回合结束：把组压进 groups，后续事件（新的 user_message 等）起新组
      continue;
    }

    // Terminal moments close the current activity run.
    closeActivity();
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
  list_dict_files: { action: '查看字典', running: '查看字典', verb: '', icon: '📚', summary: () => '列出项目字典文件' },
  read_dict: { action: '读取字典', running: '读取字典', verb: '', icon: '📖', summary: (a) => str(a?.file_key) },
  save_dict: { action: '保存字典', running: '保存字典', verb: '', icon: '💾', summary: (a) => str(a?.file_key) },
  create_dict_file: { action: '新建字典', running: '新建字典', verb: '', icon: '🗂️', summary: (a) => str(a?.filename) },
  get_name_table: { action: '读取人名表', running: '读取人名表', verb: '', icon: '👤', summary: () => 'name替换表' },
  save_name_table: { action: '保存人名表', running: '保存人名表', verb: '', icon: '👥', summary: (a) => (Array.isArray(a?.names) ? `${a.names.length} 条` : '') },
  start_translation: { action: '启动翻译', running: '启动翻译', verb: '', icon: '▶️', summary: (a) => str(a?.translator) },
  stop_translation: { action: '停止翻译', running: '停止翻译', verb: '', icon: '⏹️', summary: () => '' },
  wait: { action: '等待', running: '等待中', verb: '', icon: '⏳', summary: (a) => waitSummary(a) },
  get_progress: { action: '查询进度', running: '查询进度', verb: '', icon: '📊', summary: () => '' },
  get_runtime: { action: '查询运行时', running: '查询运行时', verb: '', icon: '⚙️', summary: () => '' },
  list_problems: { action: '检查问题', running: '检查问题', verb: '', icon: '🔍', summary: () => '' },
  read_cache: { action: '读取缓存', running: '读取缓存', verb: '', icon: '📄', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  search_cache: { action: '搜索缓存', running: '搜索缓存', verb: '', icon: '🔎', summary: (a) => str(a?.query) },
  patch_cache: { action: '修改译文', running: '修改译文', verb: '', icon: '✏️', summary: (a) => (Array.isArray(a?.patches) ? `${a.patches.length} 条` : str(a?.filename)) },
};

const DEFAULT_TOOL_META: ToolMeta = { action: '调用工具', running: '调用工具', verb: '', icon: '🔧', summary: () => '' };

function toolMeta(name: string | undefined): ToolMeta {
  if (!name) return DEFAULT_TOOL_META;
  return TOOL_META[name] || DEFAULT_TOOL_META;
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

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
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
  const [elapsed, setElapsed] = useState(0);
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
        if (cancelled) return;
        // 后端是权威来源：新会话后端空（events=0）时必须覆盖本地可能残留的
        // 旧缓存，否则会错把别的会话的内容糊在新建会话上。
        const snapEvents = snap.events || [];
        const snapLen = snapEvents.length;
        const cachedLen = persisted?.events.length || 0;
        if (snapLen >= cachedLen || snapLen === 0) {
          setEvents(snapEvents);
          lastStepRef.current = maxStep(snapEvents);
          hasBackendSessionRef.current = Boolean(snapLen);
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

  // Elapsed timer while running.
  useEffect(() => {
    if (!running) return;
    const tick = () => setElapsed(Math.max(0, Math.round((Date.now() - startRef.current) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [running]);

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
      <div className="agent-console__main">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar">🤖</span>
          <div className="agent-console__bar-copy">
            <div className="agent-console__bar-title">
              <span className="agent-console__bar-name">翻译 Agent</span>
              {activeTitle ? <span className="agent-console__bar-session">{activeTitle}</span> : null}
              <StatusPill status={status} running={running} elapsed={elapsed} />
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
          <span className="agent-console__metric" title="已记录步骤">
            <span className="agent-console__metric-value">{stepCount}</span>
            <span className="agent-console__metric-label">步</span>
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
                  <span className="agent-working__label">{workingLabel(timeline)}</span>
                  <span className="agent-working__elapsed">{formatElapsed(elapsed)}</span>
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
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && canSend) {
                e.preventDefault();
                void handleSend();
              }
            }}
          />
          <div className="agent-composer__toolbar">
            <div className="agent-composer__left">
              <span className="agent-composer__chip agent-composer__chip--static" title={projectDir || '未选择项目'}>
                <span className="agent-composer__chip-icon">📁</span>
                {projectDir ? shortName(projectDir) : '未选择项目'}
              </span>
              <button
                type="button"
                className="agent-composer__chip"
                onClick={() => setSettingsOpen((v) => !v)}
                title="模型与配置"
              >
                <span className="agent-composer__chip-icon">⚙</span>
                {backendProfileName || '未配置后端'}
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
                    title="发送插话（Agent 会在下一步看到，Ctrl/⌘ + Enter）"
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
                  title={hasSession ? '发送并继续（Ctrl/⌘ + Enter）' : '发送并启动 Agent（Ctrl/⌘ + Enter）'}
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

function workingLabel(timeline: TimelineGroup[]): string {
  const last = timeline[timeline.length - 1];
  if (last && last.type === 'activity' && last.items.length) {
    const item = last.items[last.items.length - 1];
    if (item.kind === 'thought') return '思考中';
    return `${toolMeta(item.name).running}…`;
  }
  return '正在开始';
}

function StatusPill({ status, running, elapsed }: { status: string; running: boolean; elapsed: number }) {
  const tone = running ? 'running' : status;
  const label = running
    ? `运行中 · ${formatElapsed(elapsed)}`
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
  if (group.type === 'activity' && group.finalThought) {
    return (
      <>
        {group.items.length > 0 ? <AgentActivityGroup group={group} isLive={isLive} /> : null}
        <FinalMessage item={group.finalThought} />
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

  // Follow the live run: auto-expand while working, auto-collapse when settled,
  // unless the user took manual control of this group.
  useEffect(() => {
    if (userToggledRef.current) return;
    setOpen(isLive);
  }, [isLive]);

  const items = group.items;
  const totalMs = items.reduce((sum, it) => sum + (it.durationMs || 0), 0);
  const hasThought = items.some((it) => it.kind === 'thought');
  const toolCount = items.filter((it) => it.kind === 'tool').length;
  // 压缩提示不算"工作步骤"，避免污染耗时与步数统计
  const visibleCount = items.filter((it) => it.kind !== 'compact').length;

  const label = isLive
    ? hasThought && !toolCount
      ? '思考中'
      : '工作中'
    : hasThought && !toolCount
      ? '思考'
      : '工作';

  const parts: string[] = [];
  if (totalMs > 0) parts.push(formatDuration(totalMs));
  if (visibleCount > 1) parts.push(`${visibleCount} 步`);

  const tail = isLive ? liveTail(items) : '';

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
              item.kind === 'thought' ? (
                <ThoughtRow key={`t-${i}`} item={item} />
              ) : item.kind === 'compact' ? (
                <CompactRow key={`c-${i}`} item={item} />
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
    if (it.kind === 'thought' && it.content) return collapse(it.content);
    if (it.kind === 'tool') {
      const s = toolMeta(it.name).summary(asArgs(it.arguments));
      return [toolMeta(it.name).action, s].filter(Boolean).join(' ');
    }
  }
  return '';
}

function collapse(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

function asArgs(args: unknown): Record<string, unknown> | undefined {
  if (args && typeof args === 'object' && !Array.isArray(args)) return args as Record<string, unknown>;
  return undefined;
}

function ThoughtRow({ item }: { item: ActivityItem }) {
  // 默认展开：模型「回复」始终可见，不随流式结束自动收起。
  // 用户仍可手动点 header 折叠/展开单个回复。
  const [open, setOpen] = useState(true);
  const text = item.content || '';
  const streaming = Boolean(item.streaming);
  const long = text.length > 180 || text.includes('\n');

  return (
    <div className={`agent-thought${open ? ' is-open' : ''}${streaming ? ' is-streaming' : ''}`}>
      <button
        type="button"
        className="agent-thought__header"
        onClick={() => setOpen((v) => !v)}
        disabled={!long}
        aria-expanded={open}
      >
        <span className="agent-thought__icon">✳</span>
        <span className="agent-thought__label">{streaming ? '回复中' : '回复'}</span>
        {!long ? (
          <span
            className="agent-thought__inline agent-md"
            // markdown 已在渲染器内整体转义，无注入面
            dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
          />
        ) : null}
        {long ? <span className="agent-thought__caret">›</span> : null}
      </button>
      {long ? (
        <div className="agent-thought__collapse">
          <div className="agent-thought__collapse-inner">
            <div
              className="agent-thought__text agent-md"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
            />
            {streaming ? <span className="agent-typing-cursor" aria-hidden /> : null}
          </div>
        </div>
      ) : streaming ? (
        <span className="agent-typing-cursor agent-typing-cursor--inline" aria-hidden />
      ) : null}
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

/* ── Tool row (disclosure, not a boxed card) ── */

function ToolRow({ item, live }: { item: ActivityItem; live: boolean }) {
  const [open, setOpen] = useState(false);
  const meta = toolMeta(item.name);
  const summary = meta.summary(asArgs(item.arguments));
  const ok = item.ok !== false;
  const pending = item.ok === undefined && item.result === undefined && !item.error;
  const isRunning = live && pending;

  // wait 行：等待期间显示倒计时进度条。
  const isWait = item.name === 'wait';
  const waiting = isWait && pending && typeof item.waitTotalMs === 'number';
  const waitTotal = item.waitTotalMs || 0;
  const waitRemaining = item.waitRemainingMs ?? waitTotal;
  const waitRatio = waitTotal > 0 ? Math.min(1, Math.max(0, 1 - waitRemaining / waitTotal)) : 0;

  const resultText = formatPayload(ok ? item.result : item.error);
  const hasDetails = Boolean(summary || resultText || item.arguments);
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