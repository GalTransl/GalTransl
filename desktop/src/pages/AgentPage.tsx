import { followTail } from "./agent/followTail";
import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import {
  addOpenProject,
  AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  type AgentContextUsage,
  type AgentEvent,
  type AgentSession as AgentSessionMeta,
  answerAgentAsk,
  answerAgentPermission,
  createAgentSession,
  deleteAgentQueued,
  deleteAgentSession,
  fetchAgentStatus,
  fetchAgentTranscript,
  getAgentDefaultBackendProfile,
  getAgentTranslatorBackendContext,
  getBackendProfile,
  getBackendProfileNames,
  listAgentSessions,
  loadOpenProjects,
  OPEN_PROJECTS_CHANGE_EVENT,
  type QueuedMessage,
  readConfigFileName,
  resetAgent,
  saveOpenProjects,
  sendAgentMessage,
  sendAgentQueuedNow,
  setAgentPermissionMode,
  startAgent,
  stopAgent,
  updateAgentQueued,
} from '../lib/api';
import { normalizeError } from '../lib/errors';
import { notifyNeedsAttention } from '../lib/desktopNotify';
import {
  loadPermissionMode,
  PERMISSION_MODE_HINTS,
  PERMISSION_MODE_LABELS,
  PERMISSION_MODES,
  type PermissionDecision,
  type PermissionMode,
  savePermissionMode,
} from '../lib/permissionMode';
import { invalidateCacheFilesForToolResult } from '../components/AgentCacheRef';
import { Icon } from '../components/Icon';
import { formatProfileLabel } from '../lib/backendProfile';
import { removeProjectFromHistory } from './HomePage';
import { AgentSessionSidebar } from './agent/AgentSessionSidebar';
import { AGENT_PROMPT_SUGGESTIONS, ContextMeter, SendIcon, StatusPill, StopIcon } from './agent/composer';
import { AgentGroupView } from './agent/rows/AgentGroupView';
import { AskUserCard } from './agent/rows/AskUserCard';
import { PermissionCard } from './agent/rows/PermissionCard';
import {
  loadActiveSessionId,
  loadSession,
  loadSessionBackends,
  maxStep,
  mergeTranscriptEvents,
  persistedTranscriptEvents,
  readHistory,
  saveActiveSessionId,
  saveSession,
  saveSessionBackends,
  seedStreaming,
  type SessionBackendMap,
  sessionsKey,
  shortName,
} from './agent/storage';
import {
  isTerminal,
  lastActivityItem,
  workingLabel,
} from './agent/timeline';
import { createTimelineBuilder } from './agent/timelineCache';
import { askNotifyBody } from './agent/toolMeta';
import { useAgentStream } from './agent/useAgentStream';

/* ── Main page ── */

export function AgentPage() {
  const uiLanguage = useUiLanguage();
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
  const [messageDraft, setMessageDraft] = useState('');

  // 输入框本体：点推荐提示词后要把焦点还回去（用户接着改两个字就能直接回车发出）
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  /** 空态的推荐提示词：填进输入框并聚焦到末尾，**不直接发送**（用户还能改）。
   *  输入框里已经有字时追加成新的一行——点了没反应或者把写了一半的话冲掉都很难受。 */
  const applyPromptSuggestion = useCallback((text: string) => {
    setMessageDraft((prev) => (prev.trim() ? `${prev.trimEnd()}\n${text}` : text));
    // 等 React 把新的 value 提交到 DOM 再定位光标，否则量到的还是旧长度
    requestAnimationFrame(() => {
      const el = composerRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    });
  }, []);

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<string>('idle');
  const [running, setRunning] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
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
  // 本地乐观显示、还等后端确认的用户消息：文本 → 条数（同样的文字连发两条也要一一对上）
  const localMsgIdsRef = useRef<Map<string, number>>(new Map());
  // 发送流程自身的会话过渡：首条消息 create→start→subscribe 期间
  // activeSessionId 从空变到新会话，会触发会话切换 effect；它的恢复/对账
  // 逻辑会清掉乐观气泡并把 running 打回 false（fetch 先于 startAgent 完成，
  // 拿到 idle），界面就像没在跑一样。用 ref 标记该窗口让 effect 跳过；
  // 用户在此窗口内手动切到别的会话则不拦（id 不匹配，正常恢复）。
  const sendingRef = useRef(false);
  const sendTransitionRef = useRef<string | null>(null);

  // 权限模式：本地存一份（下次打开还是这个档），同时立刻推给后端——跑着也能改，下一次
  // 工具调用就按新档判。声明放在最前：handleSend 和答复回调都要把它一起带上（见 answerBackendContext）。
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(() => loadPermissionMode());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const threadContentRef = useRef<HTMLDivElement | null>(null);
  const tailFollowerRef = useRef<ReturnType<typeof followTail> | null>(null);
  // 跟随状态也驱动「回到最新」按钮。
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
    closeStream();
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
    setRunning(persisted?.status === 'running');
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
  // 只在可回放的事件变了时才写 localStorage：流式增量不落缓存，按 token 触发
  // 一次 filter + JSON.stringify + 同步写盘会把界面拖卡。
  const persistKey = useMemo(() => {
    const replayable = persistedTranscriptEvents(events);
    const last = replayable[replayable.length - 1];
    return `${replayable.length}:${last ? `${last.type}@${last.step}` : ''}`;
  }, [events]);
  const eventsRef = useRef(events);
  eventsRef.current = events;
  useEffect(() => {
    const events = eventsRef.current;
    if (!effectiveProject || !activeSessionId || !events.length) return;
    saveSession({
      projectDir: effectiveProject,
      sessionId: activeSessionId,
      events,
      status,
      first_prompt: events.find((event) => event.type === 'user_message')?.message ?? '',
      startedAt: startRef.current,
      finishedAt: status === 'running' ? 0 : Date.now(),
    });
  }, [persistKey, status, effectiveProject, activeSessionId]);

  // 折叠动画、后端快照恢复及窗口大小变化都可能改变内容高度。
  useLayoutEffect(() => {
    const viewport = scrollRef.current;
    const content = threadContentRef.current;
    if (!viewport || !content) return;
    const follower = followTail(viewport, content, setAtBottom);
    tailFollowerRef.current = follower;
    return () => {
      follower.dispose();
      tailFollowerRef.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    tailFollowerRef.current?.jump();
  }, [effectiveProject, activeSessionId]);

  /** 回到转录最底部（并恢复跟随新消息）。 */
  const handleJumpToBottom = useCallback(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    tailFollowerRef.current?.jump(reduced ? 'auto' : 'smooth');
  }, []);

  // 事件流：连接管理（重连、批量、作废旧流）在 useAgentStream 里，这里只管怎么解释事件
  const stream = useAgentStream(setEvents, {
    onEvent: (ev) => {
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
        return true;
      }
      // 队列快照：队列面板的权威数据（发送/被消费/删除/编辑都会推一份整表）。
      // 是控制事件，和 status 一样放在去重之前处理，保证不会漏。
      if (ev.type === 'queue') {
        setQueued(ev.queued || []);
        return true;
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
        if (ev.step <= lastStepRef.current) return true;
        lastStepRef.current = ev.step;
      }
      // 上下文用量：只更新指示器，不进对话转录（否则每次请求都会在
      // 界面上多出一条无意义记录，刷新后由 status 快照兜底）
      if (ev.type === 'context_usage') {
        if (ev.context) setContextUsage(ev.context);
        return true;
      }
      if (ev.type === 'user_message') {
        // 后端已收录这条消息：用真实事件替换本地乐观的占位（step=-1），
        // 保持顺序正确且刷新后可完整回放
        const localId = `local:${ev.message}`;
        const pendingCount = localMsgIdsRef.current.get(localId) || 0;
        if (pendingCount > 0) {
          if (pendingCount > 1) localMsgIdsRef.current.set(localId, pendingCount - 1);
          else localMsgIdsRef.current.delete(localId);
          const text = ev.message;
          stream.flush();
          setEvents((prev) => {
            const idx = prev.findIndex(
              (it) => it.type === 'user_message' && it.step === -1 && it.message === text,
            );
            if (idx === -1) return [...prev, ev];
            const next = [...prev];
            next[idx] = ev;
            return next;
          });
          return true;
        }
      }
      if (ev.type === 'finish' || ev.type === 'error' || ev.type === 'stopped') {
        // 后端若已安排好 followup 回合（点「立即」发送、或滞留插话转新回合），
        // 下一步马上又在跑：这里绝不能把运行态打回 false——否则停止按钮会消失、
        // 顶栏还会因为 running=false 显示成"空闲"，而后端其实在跑。
        if (!ev.followup) setRunning(false);
        // 不主动 abort：followup 回合的后续事件会走同一条流；
        // 流的关闭由后端 close 帧决定。
      }
      // 进转录（由 hook 批量写进 events）
      return false;
    },
    onClose: () => {
      // 后端明确关流才收尾（可能整段回放完才到）
      setRunning(false);
    },
    onReconnecting: (attempt, max) => {
      setError(attempt > 0 ? uiMessage("agent:agentPage.onReconnecting_setError_agentPending", { attempt: attempt, max: max }) : null);
    },
    onGiveUp: (err) => {
      setError(normalizeError(err, uiMessage("agent:agentPage.onGiveUp_normalizeError_agent")));
      setRunning(false);
    },
  });
  const subscribeStream = useCallback(
    // 续订时从本地已见的最大 step 之后开始拉，避免后端重放旧回合的事件
    (dir: string, sessionId?: string) => stream.subscribe(dir, sessionId, () => lastStepRef.current),
    [stream.subscribe],
  );
  const closeStream = stream.close;

  const handleSend = useCallback(async () => {
    const text = messageDraft.trim();
    if (!text) return;
    setError(null);
    // 发出去就等于会话开始：项目 chip 马上要变成不可点的纯标签，菜单顺手收掉
    setProjectMenuOpen(false);
    if (!effectiveProject) {
      setError(uiMessage("agent:agentPage.handleSend_setError_selectCountProject"));
      return;
    }
    const profile = getBackendProfile(backendProfileName);
    if (!profile) {
      setError(uiMessage("agent:agentPage.handleSend_setError_selectCountTranslationBackendConfigModelSettings"));
      setProfileMenuOpen(true);
      return;
    }

    // 运行中发的消息不进聊天框：后端把它放进队列，显示在 composer 上方的队列
    // 面板里（想马上发就点「立即」）。空闲时才是普通对话气泡——本地立刻显示，
    // 不等后端确认。
    if (!running) {
      const localId = `local:${text}`;
      localMsgIdsRef.current.set(localId, (localMsgIdsRef.current.get(localId) || 0) + 1);
      stream.flush();
      setEvents((prev) => [
        ...prev,
        { type: 'user_message', step: -1, message: text },
      ]);
    }
    setMessageDraft('');
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
            first_prompt: text,
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
      setError(normalizeError(err, uiMessage("agent:agentPage.handleSend_normalizeError_sendFailed")));
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
    messageDraft,
    running,
    permissionMode,
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
        if (!stream.isOpen() && snap.status === 'running') {
          subscribeStream(effectiveProject, activeSessionRef.current || undefined);
        }
      } catch (err) {
        setError(normalizeError(err, uiMessage("agent:agentPage.handleQueuedSendNow_normalizeError_sendFailed")));
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
        setError(normalizeError(err, uiMessage("agent:agentPage.handleQueuedDelete_normalizeError_deleteQueueFailed")));
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
      setError(normalizeError(err, uiMessage("agent:agentPage.handleQueuedSaveEdit_normalizeError_changeQueueFailed")));
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
      setError(normalizeError(err, uiMessage("agent:agentPage.handleStop_normalizeError_stopAgentFailed")));
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
        setMessageDraft('');
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
    setMessageDraft('');
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
    closeStream();
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
    setMessageDraft('');
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
          closeStream();
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
        setError(normalizeError(err, uiMessage("agent:agentPage.handleCreateSessionInProject_normalizeError_newSessionFailed")));
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

  /** 项目分组 ✕：把这个项目从 Agent 的会话列里收起（**不删任何会话与文件**）。
   *
   *  会话列的项目来自两处（打开的项目 ∪ 首页最近项目），所以"关闭"也分两种，
   *  各自沿用已有的同款入口，不另造第三种语义：
   *  - 打开中的项目 → 从"已打开项目"里移除（与左侧栏项目行的 ✕ 同一件事；
   *    写盘 + 广播 OPEN_PROJECTS_CHANGE_EVENT，App 的侧栏会同步收起）；
   *  - 只在首页最近项目里的 → 从最近项目里移除（与首页历史行的 ✕ 同一件事）。
   *  该项目下还有会话在跑时不允许关：收起来就看不到那个蓝灯了，容易忘了它还在跑；
   *  关掉的是当前正在看的项目时，主区回空态（与顶部 ＋ 同款收尾）。 */
  const handleCloseProjectGroup = useCallback(
    (dir: string) => {
      if (!dir) return;
      const shortDir = shortName(dir);
      const list = sessionsByProject[dir] || [];
      const hasRunning =
        list.some((s) => s.status === 'running') || (dir === effectiveProjectRef.current && running);
      if (hasRunning) {
        setError(uiMessage("agent:agentPage.handleCloseProjectGroup_setError_sessionPendingRunningStopDisable", { shortDir: shortDir }));
        return;
      }
      const isOpen = loadOpenProjects().includes(dir);
      const ok = window.confirm(
        isOpen
          ? translate("agent:agentPage.ok_confirm_disableProjectProjectSessionHistoryAllKeep", { shortDir: shortDir })
          : translate("agent:agentPage.ok_confirm_projectRemoveRemoveEntryHistorySessionProject", { shortDir: shortDir }),
      );
      if (!ok) return;
      if (isOpen) {
        saveOpenProjects(loadOpenProjects().filter((d) => d !== dir));
      } else {
        removeProjectFromHistory(dir);
      }
      if (dir === effectiveProjectRef.current) {
        void handleCreateBlankSession();
      }
      // 分组从列表里消失；会话缓存一并清掉（下次打开该项目时会重新拉）
      setSessionsByProject((prev) => {
        if (!(dir in prev)) return prev;
        const next = { ...prev };
        delete next[dir];
        return next;
      });
      // 打开列表那条路径靠 OPEN_PROJECTS_CHANGE_EVENT 已同步；历史那条没有事件，这里补一次
      setProjectOptions(mergeProjects());
    },
    [sessionsByProject, running, handleCreateBlankSession, mergeProjects],
  );

  /** 选中某项目下的某会话：停 SSE、清视图，切项目+会话；转录由 session effect 加载。 */
  const handleSelectSession = useCallback(
    (dir: string, sessionId: string) => {
      if (sessionId === activeSessionRef.current && dir === effectiveProjectRef.current) return;
      closeStream();
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
      if (!window.confirm(translate("agent:agentPage.handleDeleteSession_confirm_deleteSessionSessionHistoryDelete", { title: session.title }))) return;
      try {
        await deleteAgentSession(dir, session.session_id);
      } catch (err) {
        setError(normalizeError(err, uiMessage("agent:agentPage.handleDeleteSession_normalizeError_deleteSessionFailed")));
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
        closeStream();
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
    if (!window.confirm(translate("agent:agentPage.handleClear_confirm_clearCurrentSessionRunningJobStopHistory"))) return;
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

  const timelineBuilder = useMemo(() => createTimelineBuilder(), [uiLanguage, effectiveProject, activeSessionId]);
  const timeline = useMemo(() => timelineBuilder(events), [timelineBuilder, events]);
  const hasSession = events.length > 0;
  const canSend = Boolean(projectDir) && Boolean(backendProfileName) && messageDraft.trim().length > 0 && !sending;
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
  const [askError, setAskError] = useMessageState<string | null>(null);
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
    if (askId) {
      handleJumpToBottom();
      // Agent 卡在提问上，用户多半已经切到别的窗口了：弹条系统通知把他叫回来
      // （窗口在前台就不弹，见 desktopNotify）
      void notifyNeedsAttention(`ask:${askId}`, translate("agent:agentPage.agentPage_notifyNeedsAttention_agentAnswer"), askNotifyBody(pendingAsk));
    }
  }, [askId, handleJumpToBottom]);
  /** 答复 ask_user / 审批卡时捎带的前端上下文（与 handleSend 同一份）。
   *  后端那边如果已经没在等这道题（卡片是重启后从落盘事件重建出来的），会把这次答复当成
   *  一条用户消息、另起一个回合；而 token 只在 localStorage、不落盘，必须随请求带上——
   *  否则新回合起不来（报 "backend profile ... tokens is empty"）。 */
  const answerBackendContext = useCallback(() => {
    const profile = getBackendProfile(backendProfileName);
    if (!profile) return undefined;
    return {
      ...(backendProfileName ? { backend_profile_name: backendProfileName } : {}),
      backend_profile_data: profile,
      ...getAgentTranslatorBackendContext(effectiveProject),
      permission_mode: permissionMode,
    };
  }, [backendProfileName, effectiveProject, permissionMode]);
  const handleAskSubmit = useCallback(
    async (answers: Array<string[] | null>) => {
      const target = pendingAskIdRef.current;
      if (!target) return;
      setAskSubmitting(true);
      setAskError(null);
      try {
        await answerAgentAsk(
          effectiveProject,
          answers,
          activeSessionRef.current || undefined,
          answerBackendContext(),
        );
        setAnsweredAskId(target);
        // 后端可能把这次答复当成用户消息另起了一个回合：空闲会话的 SSE 流早在上轮结束时
        // 就关了，补一次订阅才看得到新回合的事件（否则界面永远停在"等待指令"）。
        if (!running) {
          setStatus('running');
          setRunning(true);
          startRef.current = Date.now();
        }
        subscribeStream(effectiveProject, activeSessionRef.current || undefined);
      } catch (err) {
        setAskError(normalizeError(err, uiMessage("agent:agentPage.handleAskSubmit_normalizeError_answerSubmitFailed")));
      } finally {
        setAskSubmitting(false);
      }
    },
    [effectiveProject, running, answerBackendContext, subscribeStream],
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
  const [permissionError, setPermissionError] = useMessageState<string | null>(null);
  // 同上：提交后先把卡片收起来，工具结果一到就自然消失
  const [answeredPermissionId, setAnsweredPermissionId] = useState('');
  const pendingPermissionIdRef = useRef('');
  const permissionId = pendingPermission?.permission?.id || '';
  useEffect(() => {
    pendingPermissionIdRef.current = permissionId;
    setPermissionError(null);
    setPermissionSubmitting(false);
    // 审批卡同样摆在转录里：Agent 正卡在这儿等一个点击，带到最底部
    if (permissionId) {
      handleJumpToBottom();
      // 同 ask_user：被权限拦下来时也在后台等着，弹系统通知（前台不弹，见 desktopNotify）
      const label = pendingPermission?.permission?.label || pendingPermission?.name || translate("agent:agentPage.label_message_text");
      const name = pendingPermission?.name || '';
      void notifyNeedsAttention(
        `permission:${permissionId}`,
        translate("agent:agentPage.agentPage_notifyNeedsAttention_agentWaitApprove"),
        name && name !== label ? translate("agent:agentPage.agentPage_notifyNeedsAttention_confirm", { label: label, name: name }) : translate("agent:agentPage.agentPage_notifyNeedsAttention_confirmVariant2", { label: label }),
      );
    }
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
          answerBackendContext(),
        );
        setAnsweredPermissionId(target);
        // 同 ask_user：没在等的审批会被兜底成一条用户消息、另起回合，补订阅才看得到
        if (!running) {
          setStatus('running');
          setRunning(true);
          startRef.current = Date.now();
        }
        subscribeStream(effectiveProject, activeSessionRef.current || undefined);
      } catch (err) {
        setPermissionError(normalizeError(err, uiMessage("agent:agentPage.handlePermissionDecide_normalizeError_submitFailed")));
      } finally {
        setPermissionSubmitting(false);
      }
    },
    [effectiveProject, running, answerBackendContext, subscribeStream],
  );

  // 选择器参考「后端配置」那个 chip：点开是菜单。
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
      ).catch((err) => setError(normalizeError(err, uiMessage("agent:agentPage.handlePickPermissionMode_normalizeError_permissionChangeFailed"))));
    },
    [effectiveProject],
  );
  // 展示「后端配置文件名/模型名」：模型名从当前配置里取，与「模型设置」页同一口径
  const backendProfileLabel = useMemo(
    () => (backendProfileName ? formatProfileLabel(backendProfileName, getBackendProfile(backendProfileName)) : ''),
    [uiLanguage, backendProfileName],
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
        onCloseProjectGroup={handleCloseProjectGroup}
        onSelectSession={handleSelectSession}
        onDeleteSession={(dir, s) => void handleDeleteSession(dir, s)}
      />
      <div className="agent-console__main agent-cockpit">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar" aria-hidden><Icon name="bot" /></span>
          <div className="agent-console__bar-copy">
            <div className="agent-console__bar-title">
              <span className="agent-console__bar-name">{translate("agent:agentPage.agentConsoleBarTitle_message_galTranslAgent")}</span>
            </div>
            <div className="agent-console__project-static">
              {projectDir ? (
                <>
                  <span className="agent-console__project-name">{shortName(projectDir)}</span>
                  <span className="agent-console__project-path">{projectDir}</span>
                </>
              ) : (
                <span className="agent-console__project-empty">{translate("agent:agentPage.agentConsoleProjectStatic_message_notSelectedProject")}</span>
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
            title={projectDir || translate("agent:agentPage.agentConsoleIconBtn_title_openProjectFile")}
            aria-label={translate("agent:agentPage.agentConsoleIconBtn_ariaLabel_openProjectFile")}
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
            title={translate("agent:agentPage.agentConsoleIconBtn_title_resetSessionClearAllBackend")}
          >
            <Icon name="trash" />
          </button>
        </div>
      </header>

      <div className="agent-console__thread" ref={scrollRef}>
        <div className="agent-thread" ref={threadContentRef}>
          {timeline.length === 0 ? (
            <div className="agent-hero">
              <div className="agent-hero__mark"><Icon name="bot" /></div>
              <h2 className="agent-hero__title">{translate("agent:agentPage.agentHero_message_agentCountTranslation")}</h2>
              <p className="agent-hero__subtitle">{translate("agent:agentPage.agentHero_message_sendEntrySessionProjectDictionaryTranslationProgress")}</p>
              <div className="agent-hero__steps">
                {AGENT_PROMPT_SUGGESTIONS.map((text) => (
                  <button
                    key={text}
                    type="button"
                    className="agent-hero__step"
                    onClick={() => applyPromptSuggestion(text)}
                    title={translate("agent:agentPage.agentHeroStep_title_text")}
                  >
                    <span className="agent-hero__step-icon"><Icon name="sparkle" /></span>
                    {text}
                  </button>
                ))}
              </div>
              <div className="agent-hero__project-panel">
                {projectOptions.length > 0 ? (
                  <>
                    <span className="agent-hero__open-projects-label">{translate("agent:agentPage.agentHeroProjectPanel_message_selectCountDoneOpenProjectStart")}</span>
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
                  <span className="agent-hero__open-projects-label">{translate("agent:agentPage.agentHeroProjectPanel_message_emptyOpenProjectNewOpenCount")}</span>
                )}
                <div className="agent-hero__actions">
                  <button
                    type="button"
                    className="agent-hero__action"
                    onClick={() => void handleOpenProject()}
                    title={translate("agent:agentPage.agentHeroAction_title_fileOpenCountDoneProject")}
                  >
                    <Icon name="folder-open" />{translate("agent:agentPage.agentHeroAction_button_openProject")}</button>
                  <button
                    type="button"
                    className="agent-hero__action agent-hero__action--secondary"
                    onClick={() => navigate('/new-project')}
                    title={translate("agent:agentPage.agentHeroActionAgentHeroActionSecondary_title_newProject")}
                  >
                    <Icon name="sparkle" />{translate("agent:agentPage.agentHeroActionAgentHeroActionSecondary_button_newProject")}</button>
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
              title={translate("agent:agentPage.agentJumpBottom_title_text")}
              aria-label={translate("agent:agentPage.agentJumpBottom_ariaLabel_text")}
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
                        >{translate("common:actions.save")}</button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          onClick={() => setEditingQueued(null)}
                        >{translate("common:actions.cancel")}</button>
                      </>
                    ) : (
                      <>
                        <span className="agent-queue__text" title={item.text}>
                          {item.text}
                        </span>
                        <button
                          type="button"
                          className="agent-queue__act is-primary"
                          title={translate("agent:agentPage.agentQueueActIsPrimary_title_agentSendEntry")}
                          onClick={() => void handleQueuedSendNow(item.id)}
                        >
                          <span className="agent-queue__act-icon"><Icon name="send-now" /></span>{translate("agent:agentPage.agentQueueActIsPrimary_button_text")}</button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          title={translate("agent:agentPage.agentQueueAct_title_editEntry")}
                          aria-label={translate("agent:agentPage.agentQueueAct_ariaLabel_editEntry")}
                          onClick={() => setEditingQueued({ id: item.id, text: item.text })}
                        >
                          <Icon name="pencil" />
                        </button>
                        <button
                          type="button"
                          className="agent-queue__act"
                          title={translate("agent:agentPage.agentQueueAct_title_deleteEntry")}
                          aria-label={translate("agent:agentPage.agentQueueAct_ariaLabel_deleteEntry")}
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
            ref={composerRef}
            className="agent-composer__input"
            value={messageDraft}
            onChange={(e) => setMessageDraft(e.target.value)}
            placeholder={
              running
                ? translate("agent:agentPage.agentConsoleComposer_placeholder_queueAuto")
                : hasSession
                  ? translate("agent:agentPage.agentConsoleComposer_placeholder_agentNextCurrentProgress")
                  : translate("agent:agentPage.agentConsoleComposer_placeholder_agentCompleteJob")
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
                  title={translate("agent:agentPage.agentComposerChipAgentComposerChipStatic_title_sessionDoneStartProjectNewSession", { value: projectDir || translate("agent:agentPage.interpolation_fallback_notSelectedProject") })}
                >
                  <span className="agent-composer__chip-icon"><Icon name="folder" /></span>
                  <span className="agent-composer__chip-label">{projectDir ? shortName(projectDir) : translate("agent:agentPage.agentComposerChipLabel_message_notSelectedProject")}</span>
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
                    title={translate("agent:agentPage.agentProfilePicker_title_countProjectSessionStart", { value: projectDir || translate("agent:agentPage.interpolation_fallback_notSelectedProject") })}
                  >
                    <span className="agent-composer__chip-icon"><Icon name="folder" /></span>
                    <span className="agent-composer__chip-label">{projectDir ? shortName(projectDir) : translate("agent:agentPage.agentComposerChipLabel_message_notSelectedProject")}</span>
                  </button>
                  {projectMenuOpen ? (
                    <div className="agent-profile-menu" role="menu">
                      {projectOptions.length === 0 ? (
                        <div className="agent-profile-menu__empty">{translate("agent:agentPage.agentProfileMenu_message_emptyOpenProject")}</div>
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
                        <span className="agent-profile-menu__label">{translate("agent:agentPage.agentProfileMenuItemAgentProfileMenuItemAction_message_openProject")}</span>
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
                      ? translate("agent:agentPage.agentProfilePicker_title_agentRunningCannotBackendConfig")
                      : translate("agent:agentPage.agentProfilePicker_title_currentSession", { value: backendProfileLabel || translate("agent:agentPage.interpolation_fallback_notConfiguredBackend"), value2: boundBackendProfile ? translate("agent:agentPage.interpolation_fallback_doneSession") : translate("agent:agentPage.interpolation_fallback_agentDefault") })
                  }
                >
                  <span className="agent-composer__chip-icon"><Icon name="settings" /></span>
                  <span className="agent-composer__chip-label">{backendProfileLabel || translate("agent:agentPage.agentComposerChipLabel_message_notConfiguredBackend")}</span>
                </button>
                {profileMenuOpen ? (
                  <div className="agent-profile-menu" role="menu">
                    {backendProfileNames.length === 0 ? (
                      <div className="agent-profile-menu__empty">{translate("agent:agentPage.agentProfileMenu_message_emptyBackendConfig")}</div>
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
                            <span className="agent-profile-menu__label">{translate("agent:agentPage.agentProfileMenuItemAgentProfileMenuItemAction_message_agentDefault", { value: formatProfileLabel(defaultProfileName, getBackendProfile(defaultProfileName)) })}</span>
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
                      <span className="agent-profile-menu__label">{translate("agent:agentPage.agentProfileMenuItemAgentProfileMenuItemAction_message_backendConfig")}</span>
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
                  title={translate("agent:agentPage.agentProfilePicker_title_permission", { value: PERMISSION_MODE_LABELS[permissionMode], value2: PERMISSION_MODE_HINTS[permissionMode], value3: running ? translate("agent:agentPage.interpolation_fallback_runningEffective") : '' })}
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
                    <div className="agent-profile-menu__note">{translate("agent:agentPage.agentProfileMenu_message_clearSessionAllow")}</div>
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
                    title={translate("agent:agentPage.agentComposerSend_title_sendAgentNextEnterSendShiftEnter")}
                    aria-label={translate("agent:agentPage.agentComposerSend_ariaLabel_send")}
                  >
                    <SendIcon />
                  </button>
                  <button type="button" className="agent-composer__stop" onClick={handleStop} title={translate("agent:agentPage.agentComposerStop_title_stopAgent")}>
                    <StopIcon />
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="agent-composer__send"
                  onClick={() => void handleSend()}
                  disabled={!canSend}
                  title={hasSession ? translate("agent:agentPage.agentComposerSend_title_sendEnterSendShiftEnter") : translate("agent:agentPage.agentComposerSend_title_sendAgentEnterSendShiftEnter")}
                  aria-label={translate("agent:agentPage.agentComposerSend_ariaLabel_sendVariant2")}
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
