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
  encodeProjectDir,
  getBackendProfile,
  getBackendProfileNames,
  getDefaultBackendProfile,
  stopAgent,
  startAgent,
  subscribeAgentStream,
  fetchAgentStatus,
  type AgentEvent,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

const OPEN_PROJECTS_KEY = 'galtransl-open-projects';
const CONFIG_FILE_KEY = 'galtransl-config-file';
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

type AgentSession = {
  projectDir: string;
  events: AgentEvent[];
  status: string;
  goal: string;
  startedAt: number;
  finishedAt: number;
};

function sessionsKey(projectDir: string): string {
  return `galtransl-agent-session:${projectDir}`;
}

function loadSession(projectDir: string): AgentSession | null {
  try {
    const raw = localStorage.getItem(sessionsKey(projectDir));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AgentSession;
    if (!parsed || !Array.isArray(parsed.events)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveSession(session: AgentSession): void {
  try {
    // Bound the payload: keep the tail of very long transcripts.
    const events = session.events.length > 600 ? session.events.slice(-600) : session.events;
    localStorage.setItem(sessionsKey(session.projectDir), JSON.stringify({ ...session, events }));
  } catch {
    // Quota or serialization failure is non-fatal.
  }
}

function readOpenProjects(): string[] {
  try {
    const raw = localStorage.getItem(OPEN_PROJECTS_KEY);
    const parsed = raw ? (JSON.parse(raw) as string[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
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

function readConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
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
  kind: 'thought' | 'tool';
  step: number;
  content?: string;
  id?: string;
  name?: string;
  arguments?: unknown;
  ok?: boolean;
  result?: unknown;
  error?: string;
  durationMs?: number;
  // wait tool: 倒计时快照
  waitTotalMs?: number;
  waitRemainingMs?: number;
  waitInterrupted?: boolean;
};

type TimelineGroup =
  | { type: 'activity'; id: string; items: ActivityItem[] }
  | { type: 'finish'; id: string; step: number; summary: string; totalSteps?: number }
  | { type: 'error'; id: string; step: number; message: string; traceback?: string }
  | { type: 'stopped'; id: string; step: number; reason: string };

function buildTimeline(events: AgentEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = [];
  let current: Extract<TimelineGroup, { type: 'activity' }> | null = null;

  const closeActivity = () => {
    if (current && current.items.length) groups.push(current);
    current = null;
  };

  for (const ev of events) {
    if (ev.type === 'status' || ev.type === 'close') continue;

    if (ev.type === 'thought') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({ kind: 'thought', step: ev.step, content: ev.content });
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

    // Terminal moments close the current activity run.
    closeActivity();
    if (ev.type === 'finish') {
      groups.push({
        type: 'finish',
        id: `f-${ev.step}`,
        step: ev.step,
        summary: ev.summary || '',
        totalSteps: ev.total_steps,
      });
    } else if (ev.type === 'error') {
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

/* ── Main page ── */

export function AgentPage() {
  const navigate = useNavigate();

  const projectOptions = useMemo(() => {
    const seen = new Set<string>();
    const list: string[] = [];
    for (const d of readOpenProjects()) {
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

  const [projectDir, setProjectDir] = useState<string>(() => projectOptions[0] || '');
  const [configFileName, setConfigFileName] = useState<string>(() =>
    projectOptions[0] ? readConfigFileName(projectOptions[0]) : 'config.yaml',
  );
  const [backendProfileNames] = useState<string[]>(() => getBackendProfileNames());
  const [backendProfileName, setBackendProfileName] = useState<string>(
    () => getDefaultBackendProfile() || getBackendProfileNames()[0] || '',
  );
  const [goal, setGoal] = useState('按标准流程完成本项目的翻译：先准备字典，再启动翻译，最后复核结果与问题。');

  const [events, setEvents] = useState<AgentEvent[]>(() => {
    const first = projectOptions[0];
    return first ? loadSession(first)?.events || [] : [];
  });
  const [status, setStatus] = useState<string>('idle');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  const startRef = useRef(0);

  const effectiveProject = projectDir;

  /* On project change: restore persisted transcript, then reconcile with the
     backend (which may have a run in flight from a previous app session). */
  useEffect(() => {
    let cancelled = false;
    if (!effectiveProject) {
      setRunning(false);
      setStatus('idle');
      return;
    }
    const persisted = loadSession(effectiveProject);
    setEvents(persisted?.events || []);
    setStatus(persisted?.status || 'idle');
    startRef.current = persisted?.startedAt || 0;

    fetchAgentStatus(effectiveProject)
      .then((snap) => {
        if (cancelled) return;
        if (snap.events && snap.events.length >= (persisted?.events.length || 0)) {
          setEvents(snap.events);
        }
        const snapRunning = snap.status === 'running';
        setStatus(snap.status);
        setRunning(snapRunning);
        if (snapRunning) subscribeStream(effectiveProject);
      })
      .catch(() => {
        // Backend not ready — keep the persisted view.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveProject]);

  useEffect(() => {
    if (projectDir) setConfigFileName(readConfigFileName(projectDir));
  }, [projectDir]);

  // Persist transcript whenever it settles.
  useEffect(() => {
    if (!effectiveProject || !events.length) return;
    saveSession({
      projectDir: effectiveProject,
      events,
      status,
      goal,
      startedAt: startRef.current,
      finishedAt: status === 'running' ? 0 : Date.now(),
    });
  }, [events, status, goal, effectiveProject]);

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

  const subscribeStream = useCallback((dir: string) => {
    abortRef.current?.();
    abortRef.current = subscribeAgentStream(
      dir,
      (ev) => {
        if (ev.type === 'close') return;
        if (ev.type === 'status') {
          if (ev.status) setStatus(ev.status);
          if (ev.status && isTerminal(ev.status)) {
            setRunning(false);
            abortRef.current?.();
            abortRef.current = null;
          }
          return;
        }
        setEvents((prev) => [...prev, ev]);
        if (ev.type === 'finish' || ev.type === 'error' || ev.type === 'stopped') {
          setRunning(false);
        }
      },
      (err) => {
        setError(normalizeError(err, 'Agent 事件流中断'));
        setRunning(false);
      },
    );
  }, []);

  const handleStart = useCallback(async () => {
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
    setEvents([]);
    setStatus('running');
    setRunning(true);
    startRef.current = Date.now();
    try {
      await startAgent({
        project_dir: effectiveProject,
        config_file_name: configFileName || 'config.yaml',
        backend_profile_data: profile,
        goal,
      });
      subscribeStream(effectiveProject);
    } catch (err) {
      setError(normalizeError(err, '启动 Agent 失败'));
      setRunning(false);
      setStatus('failed');
    }
  }, [effectiveProject, backendProfileName, configFileName, goal, subscribeStream]);

  const handleStop = useCallback(async () => {
    if (!effectiveProject) return;
    try {
      await stopAgent(effectiveProject);
      setStatus('stopped');
      setRunning(false);
      abortRef.current?.();
      abortRef.current = null;
    } catch (err) {
      setError(normalizeError(err, '停止 Agent 失败'));
    }
  }, [effectiveProject]);

  const handleOpenProject = useCallback(async () => {
    try {
      const selected = await openDialog({ directory: true, multiple: false });
      if (typeof selected === 'string' && selected) {
        setProjectDir(selected);
        setConfigFileName(readConfigFileName(selected));
      }
    } catch {
      // User cancelled.
    }
  }, []);

  const handleClear = useCallback(() => {
    if (!effectiveProject) return;
    setEvents([]);
    setStatus('idle');
    setError(null);
    try {
      localStorage.removeItem(sessionsKey(effectiveProject));
    } catch {
      // ignore
    }
  }, [effectiveProject]);

  const timeline = useMemo(() => buildTimeline(events), [events]);
  const stepCount = useMemo(() => timeline.reduce((n, g) => n + (g.type === 'activity' ? g.items.length : 1), 0), [timeline]);
  const canStart = Boolean(projectDir) && Boolean(backendProfileName) && !running;

  return (
    <div className="agent-console">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar">🤖</span>
          <div className="agent-console__bar-copy">
            <div className="agent-console__bar-title">
              <span className="agent-console__bar-name">翻译 Agent</span>
              <StatusPill status={status} running={running} elapsed={elapsed} />
            </div>
            <button
              type="button"
              className="agent-console__project"
              onClick={handleOpenProject}
              disabled={running}
              title="点击切换项目"
            >
              {projectDir ? (
                <>
                  <span className="agent-console__project-name">{shortName(projectDir)}</span>
                  <span className="agent-console__project-path">{projectDir}</span>
                </>
              ) : (
                <span className="agent-console__project-empty">选择项目…</span>
              )}
              <span className="agent-console__project-caret">▾</span>
            </button>
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
            onClick={handleClear}
            disabled={running || !events.length}
            title="清空对话"
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
              onChange={(e) => setBackendProfileName(e.target.value)}
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
                它会自主了解项目、准备字典、启动翻译、跟进进度，并复核修复发现的问题。每一步的思考与操作都会实时显示在这里。
              </p>
              <div className="agent-hero__steps">
                <span>① 了解项目</span>
                <span>② 准备字典</span>
                <span>③ 启动翻译</span>
                <span>④ 复核修复</span>
              </div>
            </div>
          ) : (
            <>
              {projectDir ? (
                <div className="agent-row agent-row--user">
                  <div className="agent-bubble agent-bubble--user">
                    <div className="agent-bubble__label">目标</div>
                    <div className="agent-bubble__text">{goal}</div>
                  </div>
                </div>
              ) : null}

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
            placeholder="描述你希望 Agent 完成的目标，例如：按标准流程完成本项目的翻译。"
            rows={2}
            disabled={running}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && canStart) {
                e.preventDefault();
                void handleStart();
              }
            }}
          />
          <div className="agent-composer__toolbar">
            <div className="agent-composer__left">
              <button
                type="button"
                className="agent-composer__chip"
                onClick={handleOpenProject}
                disabled={running}
                title="切换项目"
              >
                <span className="agent-composer__chip-icon">📁</span>
                {projectDir ? shortName(projectDir) : '选择项目'}
              </button>
              <button
                type="button"
                className="agent-composer__chip"
                onClick={() => setSettingsOpen((v) => !v)}
                disabled={running}
                title="模型与配置"
              >
                <span className="agent-composer__chip-icon">⚙</span>
                {backendProfileName || '未配置后端'}
              </button>
            </div>
            <div className="agent-composer__right">
              {running ? (
                <button type="button" className="agent-composer__stop" onClick={handleStop} title="停止 Agent">
                  <span className="agent-composer__stop-icon" />
                </button>
              ) : (
                <button
                  type="button"
                  className="agent-composer__send"
                  onClick={handleStart}
                  disabled={!canStart}
                  title="启动 Agent（Ctrl/⌘ + Enter）"
                  aria-label="启动 Agent"
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
        ) : null}
      </div>
    </div>
  );
}

function isTerminal(s: string): boolean {
  return s === 'done' || s === 'stopped' || s === 'failed' || s === 'idle';
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
    : status === 'done'
      ? '已完成'
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
  if (group.type === 'finish') return <FinishNotice group={group} />;
  if (group.type === 'error') return <ErrorNotice group={group} />;
  if (group.type === 'stopped') return <StoppedNotice group={group} />;
  return <AgentActivityGroup group={group} isLive={isLive} />;
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

  const label = isLive
    ? hasThought && !toolCount
      ? '思考中'
      : '工作中'
    : hasThought && !toolCount
      ? '思考'
      : '工作';

  const parts: string[] = [];
  if (totalMs > 0) parts.push(formatDuration(totalMs));
  if (items.length > 1) parts.push(`${items.length} 步`);

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
  const [open, setOpen] = useState(false);
  const text = item.content || '';
  const long = text.length > 180 || text.includes('\n');
  return (
    <div className={`agent-thought${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="agent-thought__header"
        onClick={() => setOpen((v) => !v)}
        disabled={!long}
        aria-expanded={open}
      >
        <span className="agent-thought__icon">✳</span>
        <span className="agent-thought__label">思考</span>
        {!long ? <span className="agent-thought__inline">{collapse(text)}</span> : null}
        {long ? <span className="agent-thought__caret">›</span> : null}
      </button>
      {long ? (
        <div className="agent-thought__collapse">
          <div className="agent-thought__collapse-inner">
            <div className="agent-thought__text">{text}</div>
          </div>
        </div>
      ) : null}
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

function FinishNotice({ group }: { group: Extract<TimelineGroup, { type: 'finish' }> }) {
  return (
    <div className="agent-notice agent-notice--finish">
      <span className="agent-notice__icon">🏁</span>
      <div className="agent-notice__body">
        <div className="agent-notice__title">已结束 · 共 {group.totalSteps ?? group.step} 步</div>
        {group.summary ? <div className="agent-notice__text">{group.summary}</div> : null}
      </div>
    </div>
  );
}

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