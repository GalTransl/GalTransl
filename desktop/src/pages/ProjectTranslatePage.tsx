import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { invoke } from '@tauri-apps/api/core';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Button } from '../components/Button';
import { Icon } from '../components/Icon';
import { CustomSelect } from '../components/CustomSelect';
import { Panel } from '../components/Panel';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState, InlineFeedback } from '../components/page-state';
import { useConnection } from '../features/connection/ConnectionContext';
import { useNameDict } from '../lib/useNameDict';
import {
  type FileProgress,
  type Job,
  type ProjectRuntimeResponse,
  type SubmitJobPayload,
  BACKEND_PROFILES_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  fetchJobs,
  fetchProjectConfig,
  fetchProjectRuntime,
  getBackendProfile,
  getBackendProfileNames,
  getDefaultBackendProfile,
  getSelectedBackendProfileDisplay,
  getSelectedTranslatorTemplate,
  getSelectedBackendProfileJobPayload,
  setSelectedBackendProfile,
  setSelectedTranslatorTemplate,
  stopProjectTranslation,
  submitJob } from '../lib/api';
import { getProfileMeta } from '../lib/backendProfile';
import { summarizeBackendUsage } from '../lib/backendUsage';
import { normalizeError } from '../lib/errors';
import { basenamePath, joinPath } from '../lib/paths';
import { usePrefersReducedMotion, LAUNCH, STRIP_BOOT, BAR_SURGE, COMPLETE, FRESH_HIGHLIGHT_MS } from '../lib/motion';
import {
  RuntimeErrorRow,
  RuntimeSuccessRow,
  FileProgressRow,
  toRuntimeJob,
  getStatusLabel,
  formatDate,
  formatSpeed,
  formatEta,
  formatElapsedTime,
  formatPercentDisplay,
  clampPercent } from './translateRuntimeShared';

const JOB_POLL_INTERVAL_MS = 2000;
const RUNTIME_POLL_INTERVAL_MS = 1000;
// 「文件进度」里有请求在跑的行排在最上面；请求结束后再在上面待这么久（见 prioritizedRuntimeFiles）
const LIVE_ROW_STICKY_MS = 10_000;
const SUCCESS_STICK_BOTTOM_THRESHOLD_PX = 24;
// Backend keeps up to 100 success cards per translating file, but the UI only
// renders the newest 100 cards (after filtering) to keep scrolling performant.
const SUCCESS_RENDER_LIMIT = 100;
const INPUT_FOLDER_NAME = 'gt_input';
const OUTPUT_FOLDER_NAME = 'gt_output';
const CACHE_FOLDER_NAME = 'transl_cache';
const CONTINUOUS_RETRANSL_STORAGE_KEY = 'galtransl-continuous-retransl-by-project';

const HIDDEN_TRANSLATORS = new Set(['rebuilda', 'rebuildr', 'show-plugs', 'dump-name']);

// 不该占用工作台「翻译模板」下拉的流程：内部辅助流程（重建/导出人名/插件列表），
// 以及 GenDic（生成 GPT 字典，通常是从「项目字典」页启动的）。
// 以前 GenDic 会被选中并持久化，跑完一次后工作台就一直停在 GenDic 上，
// 下次要正式翻译还得手动把模板改回来——现在既不采纳、也不恢复被它污染的历史值。
const TEMPLATE_SELECTION_IGNORED = new Set([...HIDDEN_TRANSLATORS, 'GenDic']);

// Module-level cache shared across remounts of this page. Switching project tabs
// unmounts/remounts the component; without this cache the first render would see
// empty state and flash the "启动翻译" (blue) button before fetches complete,
// causing the button to flip blue→red on every tab switch.
let cachedJobs: Job[] = [];
const cachedRuntimeByProject = new Map<string, ProjectRuntimeResponse>();

type RetranslListItem = {
  key: string;
  count: number;
};


function readContinuousRetranslEnabled(projectDir: string): boolean {
  try {
    const raw = localStorage.getItem(CONTINUOUS_RETRANSL_STORAGE_KEY);
    if (!raw) return false;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return parsed[projectDir] === true;
  } catch {
    return false;
  }
}

function saveContinuousRetranslEnabled(projectDir: string, enabled: boolean) {
  try {
    const raw = localStorage.getItem(CONTINUOUS_RETRANSL_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) as Record<string, unknown> : {};
    parsed[projectDir] = enabled;
    localStorage.setItem(CONTINUOUS_RETRANSL_STORAGE_KEY, JSON.stringify(parsed));
  } catch {
    // ignore storage errors
  }
}

export function ProjectTranslatePage({ ctx }: { ctx: ProjectPageContext }) {
  const uiLanguage = useUiLanguage();
  const { projectDir, projectId, configFileName } = ctx;
  const navigate = useNavigate();
  const { connectionPhase, translators, loadJobs } = useConnection();
  const reducedMotion = usePrefersReducedMotion();
  const { nameDict } = useNameDict(projectId);

  const [jobs, setJobs] = useState<Job[]>(() => cachedJobs);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [submitError, setSubmitError] = useMessageState<string | null>(null);
  const [runtimeError, setRuntimeError] = useMessageState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [selectedTranslator, setSelectedTranslator] = useState('');
  const [runtime, setRuntime] = useState<ProjectRuntimeResponse | null>(
    () => (projectId ? cachedRuntimeByProject.get(projectId) ?? null : null),
  );
  const [projectBackendConfig, setProjectBackendConfig] = useState<Record<string, unknown> | null>(null);
  // 开始翻译页的后端切换：纯文字按钮 + 列表弹窗，不用下拉框。
  // 值与「配置编辑-翻译后端」同一口径：'__default__' 跟随全局默认，'' 用项目自身配置，其余为配置名。
  const [backendSelection, setBackendSelection] = useState(() => (projectDir ? getSelectedBackendProfileDisplay(projectDir) : '__default__'));
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>(() => getBackendProfileNames());
  const [defaultBackendName, setDefaultBackendName] = useState(() => getDefaultBackendProfile());
  const [showBackendSwitcher, setShowBackendSwitcher] = useState(false);
  const [selectedSuccessFiles, setSelectedSuccessFiles] = useState<string[]>([]);
  const [freshSuccessIds, setFreshSuccessIds] = useState<string[]>([]);
  const seenSuccessIdsRef = useRef<Set<string>>(new Set());
  const successListRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  // 默认落在「文件进度」：任务在跑的时候最常看的是它，错误只在出问题时才关心
  const [rightTab, setRightTab] = useState<'errors' | 'files' | 'retransl'>('files');
  const [retranslKeys, setRetranslKeys] = useState<RetranslListItem[]>([]);
  const [continuousRetranslEnabled, setContinuousRetranslEnabled] = useState(false);
  const [launchPhase, setLaunchPhase] = useState<'idle' | 'charging' | 'blasting'>('idle');
  const [stripBooting, setStripBooting] = useState(false);
  const [barSurging, setBarSurging] = useState(false);
  const [justCompleted, setJustCompleted] = useState(false);
  const [particles, setParticles] = useState<Array<{ id: number; x: number; y: number; dx: number; dy: number; color: string }>>([]);
  const [ripples, setRipples] = useState<Array<{ id: number; x: number; y: number; size: number }>>([]);
  const launchButtonRef = useRef<HTMLDivElement | null>(null);
  const prevShouldPollRuntimeRef = useRef(false);
  const autoRetranslPrevPendingRef = useRef<number | null>(null);
  const autoRetranslStagnationRoundsRef = useRef(0);
  const autoRetranslPrevJobIdRef = useRef<string | null>(null);
  const autoRetranslPrevJobStatusRef = useRef<Job['status'] | null>(null);

  useEffect(() => {
    if (!projectDir || translators.length === 0) {
      setSelectedTranslator('');
      return;
    }
    const persisted = getSelectedTranslatorTemplate(projectDir);
    // 被 GenDic 这类流程写进去的历史值不算有效选择，回落到默认模板（列表第一个，通常是 ForGal-json）
    const hasPersisted = translators.some((item) => item.name === persisted)
      && !TEMPLATE_SELECTION_IGNORED.has(persisted);
    const nextTranslator = hasPersisted ? persisted : translators[0].name;
    setSelectedTranslator((current) => (current === nextTranslator ? current : nextTranslator));
    if (!hasPersisted) {
      setSelectedTranslatorTemplate(projectDir, nextTranslator);
    }
  }, [projectDir, translators]);

  useEffect(() => {
    if (!projectDir) {
      setContinuousRetranslEnabled(false);
      return;
    }
    setContinuousRetranslEnabled(readContinuousRetranslEnabled(projectDir));
  }, [projectDir]);

  useEffect(() => {
    if (!projectDir) return;
    saveContinuousRetranslEnabled(projectDir, continuousRetranslEnabled);
  }, [projectDir, continuousRetranslEnabled]);

  const refreshJobs = useCallback(async (_silent = false) => {
    try {
      const nextJobs = await fetchJobs();
      cachedJobs = nextJobs;
      setJobs(nextJobs);
    } catch {
      // keep UI silent on background refresh errors
    }
  }, []);

  const refreshRuntime = useCallback(async (silent = false) => {
    if (!projectId) {
      setRuntime(null);
      return;
    }
    try {
      const data = await fetchProjectRuntime(projectId);
      cachedRuntimeByProject.set(projectId, data);
      setRuntime(data);
      setRuntimeError(null);
    } catch (error) {
      if (!silent) {
        setRuntimeError(normalizeError(error, uiMessage("projects:projectTranslatePage.refreshRuntime_normalizeError_readRunningFailed")));
      }
    }
  }, [projectId]);

  useEffect(() => {
    // Do NOT clear runtime here: on tab remount we already hydrated from
    // cachedRuntimeByProject so the stop/start button keeps the correct
    // color until the fresh snapshot arrives.
    setRuntimeError(null);
    void refreshJobs();
    void refreshRuntime(true);
  }, [refreshJobs, refreshRuntime]);

  useEffect(() => {
    if (!projectId) {
      setProjectBackendConfig(null);
      return;
    }
    let cancelled = false;
    fetchProjectConfig(projectId, configFileName || 'config.yaml')
      .then((res) => {
        if (cancelled) return;
        const backendSpecific = res.config?.backendSpecific;
        setProjectBackendConfig(
          backendSpecific && typeof backendSpecific === 'object'
            ? backendSpecific as Record<string, unknown>
            : null,
        );
      })
      .catch(() => {
        if (!cancelled) setProjectBackendConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, configFileName]);

  // 后端选择与「配置编辑」页共享同一份 localStorage：切项目时重读，别处改了就跟随。
  useEffect(() => {
    if (!projectDir) return;
    setBackendSelection(getSelectedBackendProfileDisplay(projectDir));
    setBackendProfileNames(getBackendProfileNames());
    setDefaultBackendName(getDefaultBackendProfile());
    setShowBackendSwitcher(false);
  }, [projectDir]);

  useEffect(() => {
    const sync = () => {
      if (projectDir) setBackendSelection(getSelectedBackendProfileDisplay(projectDir));
      setBackendProfileNames(getBackendProfileNames());
      setDefaultBackendName(getDefaultBackendProfile());
    };
    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
      window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    };
  }, [projectDir]);

  const refreshRetranslKeys = useCallback(async () => {
    if (!projectId) {
      setRetranslKeys([]);
      return;
    }
    try {
      const res = await fetchProjectConfig(projectId, configFileName || 'config.yaml');
      const common = (res.config?.common as Record<string, unknown>) || {};
      const raw = common.retranslKey;
      let keys: string[] = [];
      if (Array.isArray(raw)) {
        keys = raw.map((k) => String(k ?? '').trim()).filter(Boolean);
      } else if (typeof raw === 'string') {
        keys = raw.split(/\r?\n/).map((k) => k.trim()).filter(Boolean);
      }
      const runtimeSnapshot = runtime?.project_dir === projectDir
        ? runtime
        : cachedRuntimeByProject.get(projectId);
      const runtimeStats = new Map(
        (runtimeSnapshot?.retransl_stats || []).map((item) => [item.key, item.count]),
      );
      setRetranslKeys(keys.map((key) => ({ key, count: runtimeStats.get(key) ?? 0 })));
    } catch {
      // silent; keep prior list
    }
  }, [projectId, projectDir, configFileName, runtime]);

  useEffect(() => {
    void refreshRetranslKeys();
  }, [refreshRetranslKeys]);

  useEffect(() => {
    if (rightTab !== 'retransl') return;
    void refreshRetranslKeys();
  }, [rightTab, refreshRetranslKeys]);

  useEffect(() => {
    if (rightTab !== 'retransl') return;
    if (!projectId) return;
    void refreshRetranslKeys();
  }, [projectId, refreshRetranslKeys, rightTab, runtime?.retransl_stats]);

  useEffect(() => {
    const poller = window.setInterval(() => {
      void loadJobs(true);
      void refreshJobs(true);
    }, JOB_POLL_INTERVAL_MS);
    return () => window.clearInterval(poller);
  }, [loadJobs, refreshJobs]);

  const runningJobs = useMemo(
    () => jobs.filter((job) => (job.status === 'pending' || job.status === 'running') && !HIDDEN_TRANSLATORS.has(job.translator)),
    [jobs],
  );
  const currentProjectJobFallback = useMemo(
    () => runningJobs.find((job) => job.project_dir === projectDir) ?? null,
    [projectDir, runningJobs],
  );
  const runtimeMatchesProject = runtime?.project_dir === projectDir;
  const jobCandidate = runtimeMatchesProject
    ? (runtime?.job ?? (currentProjectJobFallback ? toRuntimeJob(currentProjectJobFallback) : null))
    : (currentProjectJobFallback ? toRuntimeJob(currentProjectJobFallback) : null);
  // 跑完的辅助流程（提取人名 / 构建输出 / 插件列表）不算「这个项目的任务」：新项目刚建好就显示
  // 「已完成」很莫名其妙，打开项目应当是「空闲」。运行期间照旧认它，否则进度条和「停止翻译」会失灵。
  const auxiliaryFlowFinished = Boolean(
    jobCandidate
    && HIDDEN_TRANSLATORS.has(jobCandidate.translator)
    && jobCandidate.status !== 'pending'
    && jobCandidate.status !== 'running',
  );
  const currentJob = auxiliaryFlowFinished ? null : jobCandidate;
  const shouldPollRuntime = currentJob?.status === 'pending' || currentJob?.status === 'running';
  const isSelectedTranslatorValid = translators.some((item) => item.name === selectedTranslator);

  useEffect(() => {
    const justStarted = shouldPollRuntime && !prevShouldPollRuntimeRef.current;
    prevShouldPollRuntimeRef.current = shouldPollRuntime;
    if (!justStarted) return;
    if (reducedMotion) return;
    setStripBooting(true);
    setBarSurging(true);
    const stripTimer = window.setTimeout(() => setStripBooting(false), STRIP_BOOT.totalMs);
    const barTimer = window.setTimeout(() => setBarSurging(false), BAR_SURGE.ms);
    return () => {
      window.clearTimeout(stripTimer);
      window.clearTimeout(barTimer);
    };
  }, [shouldPollRuntime, reducedMotion]);

  const prevJobCompletedRef = useRef<boolean | null>(null);
  const prevJobIdRef = useRef<string | null>(null);
  const celebratedJobIdRef = useRef<string | null>(null);
  const prevJobStatusForCancelRef = useRef<Job['status'] | null>(null);
  const prevJobIdForCancelRef = useRef<string | null>(null);
  const [cancelledAlertJobId, setCancelledAlertJobId] = useState<string | null>(null);

  useEffect(() => {
    const isCompleted = currentJob?.status === 'completed';
    const jobId = currentJob?.job_id ?? null;
    const wasPreviously = prevJobCompletedRef.current;
    const prevJobId = prevJobIdRef.current;
    prevJobCompletedRef.current = !!isCompleted;
    prevJobIdRef.current = jobId;
    if (!isCompleted || wasPreviously !== false || prevJobId !== jobId) return;
    if (celebratedJobIdRef.current === jobId) return;
    celebratedJobIdRef.current = jobId;
    setJustCompleted(true);
    const timer = window.setTimeout(() => setJustCompleted(false), COMPLETE.celebrateMs);
    return () => window.clearTimeout(timer);
  }, [currentJob?.status, currentJob?.job_id]);

  useEffect(() => {
    if (!projectDir || !runtimeMatchesProject || !currentJob?.translator) return;
    // Auxiliary flows like 构建输出 (rebuilda/rebuildr)、提取人名表 (dump-name) 和
    // 生成字典 (GenDic) reuse the job pipeline but must not hijack the user's translator
    // template selection in the cockpit dropdown.
    if (TEMPLATE_SELECTION_IGNORED.has(currentJob.translator)) return;
    setSelectedTranslator((current) => (current === currentJob.translator ? current : currentJob.translator));
    setSelectedTranslatorTemplate(projectDir, currentJob.translator);
  }, [currentJob?.translator, projectDir, runtimeMatchesProject]);

  useEffect(() => {
    const jobId = currentJob?.job_id ?? null;
    const status = currentJob?.status ?? null;
    const prevJobId = prevJobIdForCancelRef.current;
    const prevStatus = prevJobStatusForCancelRef.current;
    if (jobId !== prevJobId && cancelledAlertJobId !== null) {
      setCancelledAlertJobId(null);
    }
    if (
      jobId
      && status === 'cancelled'
      && prevJobId === jobId
      && (prevStatus === 'pending' || prevStatus === 'running')
      && cancelledAlertJobId !== jobId
    ) {
      setCancelledAlertJobId(jobId);
    }
    if (status !== 'cancelled' && cancelledAlertJobId !== null && cancelledAlertJobId === jobId) {
      setCancelledAlertJobId(null);
    }
    prevJobIdForCancelRef.current = jobId;
    prevJobStatusForCancelRef.current = status;
  }, [currentJob?.job_id, currentJob?.status, cancelledAlertJobId]);

  // Auto-dismiss the cancellation toast after a few seconds (phone-notification style).
  useEffect(() => {
    if (!cancelledAlertJobId) return;
    const timer = window.setTimeout(() => setCancelledAlertJobId(null), 5200);
    return () => window.clearTimeout(timer);
  }, [cancelledAlertJobId]);

  useEffect(() => {
    if (!shouldPollRuntime) return;
    const poller = window.setInterval(() => {
      void refreshRuntime(true);
    }, RUNTIME_POLL_INTERVAL_MS);
    return () => window.clearInterval(poller);
  }, [refreshRuntime, shouldPollRuntime]);

  useEffect(() => {
    const successEntries = runtime?.recent_successes ?? [];
    if (successEntries.length === 0) return;
    const seen = seenSuccessIdsRef.current;
    const nextFresh = successEntries.filter((entry) => !seen.has(entry.id)).map((entry) => entry.id);
    for (const entry of successEntries) seen.add(entry.id);
    if (nextFresh.length === 0) return;
    setFreshSuccessIds((current) => Array.from(new Set([...current, ...nextFresh])));
    const timeout = window.setTimeout(() => {
      setFreshSuccessIds((current) => current.filter((id) => !nextFresh.includes(id)));
    }, FRESH_HIGHLIGHT_MS);
    return () => window.clearTimeout(timeout);
  }, [runtime?.recent_successes]);

  useEffect(() => {
    const successEntries = runtime?.recent_successes ?? [];
    if (successEntries.length === 0) return;
    if (!shouldStickToBottomRef.current) return;
    const container = successListRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [runtime?.recent_successes]);

  const handleSubmit = useCallback(
    async (payload: SubmitJobPayload) => {
      setSubmitting(true);
      setSubmitError(null);
      try {
        const createdJob = await submitJob(payload);
        setJobs((current) => [createdJob, ...current.filter((job) => job.job_id !== createdJob.job_id)]);
        await refreshRuntime(true);
      } catch (error) {
        const message = normalizeError(error, uiMessage("projects:projectTranslatePage.message_normalizeError_submitJobFailed"));
        setSubmitError(message);
        throw error;
      } finally {
        setSubmitting(false);
      }
    },
    [refreshRuntime],
  );

  const handleStartTranslation = useCallback(() => {
    if (!projectDir || !selectedTranslator || !isSelectedTranslatorValid) {
      setSubmitError(uiMessage("projects:projectTranslatePage.handleStartTranslation_setSubmitError_chooseTranslation"));
      return;
    }
    setSubmitError(null);
    setSelectedTranslatorTemplate(projectDir, selectedTranslator);
    const backendProfilePayload = getSelectedBackendProfileJobPayload(projectDir);

    if (!reducedMotion) {
      const btnEl = launchButtonRef.current;
      if (btnEl) {
        const rect = btnEl.getBoundingClientRect();
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        setRipples([{ id: Date.now(), x: cx, y: cy, size: Math.max(rect.width, rect.height) }]);
        window.setTimeout(() => setRipples([]), LAUNCH.rippleMs);
      }
    }

    if (reducedMotion) {
      void handleSubmit({
        config_file_name: configFileName || 'config.yaml',
        project_dir: projectDir,
        translator: selectedTranslator,
        ...backendProfilePayload });
      void refreshRuntime();
      return;
    }

    setLaunchPhase('charging');
    void refreshRuntime();
    window.setTimeout(() => {
      setLaunchPhase('blasting');
      const newParticles = Array.from({ length: LAUNCH.particleCount }, (_, i) => {
        const angle = (Math.PI * 2 * i) / LAUNCH.particleCount + (Math.random() - 0.5) * 0.4;
        const dist = LAUNCH.particleDistanceMin + Math.random() * (LAUNCH.particleDistanceMax - LAUNCH.particleDistanceMin);
        const colors = ['#3b82f6', '#22d3ee', '#34d399', '#a78bfa', '#fbbf24'];
        return {
          id: Date.now() + i,
          x: 50,
          y: 50,
          dx: Math.cos(angle) * dist,
          dy: Math.sin(angle) * dist,
          color: colors[i % colors.length] };
      });
      setParticles(newParticles);
      window.setTimeout(() => setParticles([]), LAUNCH.particleMs);
      void handleSubmit({
        config_file_name: configFileName || 'config.yaml',
        project_dir: projectDir,
        translator: selectedTranslator,
        ...backendProfilePayload })
        .then(() => { void refreshRuntime(); });
      window.setTimeout(() => setLaunchPhase('idle'), LAUNCH.blastMs);
    }, LAUNCH.chargeMs);
  }, [configFileName, handleSubmit, isSelectedTranslatorValid, projectDir, selectedTranslator, reducedMotion, refreshRuntime]);

  const handleStopTranslation = useCallback(async () => {
    if (!projectId) return;
    setStopping(true);
    setSubmitError(null);
    void refreshRuntime();
    try {
      const stoppedJob = await stopProjectTranslation(projectId);
      setJobs((current) =>
        current.map((job) =>
          job.job_id === stoppedJob.job_id
            ? { ...job, status: stoppedJob.status, success: stoppedJob.success }
            : job,
        ),
      );
      await refreshRuntime();
      await refreshJobs();
    } catch (error) {
      const message = normalizeError(error, uiMessage("projects:projectTranslatePage.message_normalizeError_stopJobFailed"));
      setSubmitError(message);
      void refreshRuntime();
      void refreshJobs();
    } finally {
      setStopping(false);
    }
  }, [projectId, refreshJobs, refreshRuntime]);

  const summary = runtimeMatchesProject ? (runtime?.summary ?? null) : null;
  const runtimeFiles = runtimeMatchesProject ? (runtime?.files ?? []) : [];
  // 刚才还有请求在跑的文件（文件名 → 最近一次看到的时刻）。排序把它们顶到最上面，小灯才看得见——
  // 不然刚开始翻的文件（0%）要等第一批写进缓存才挪上来，最热闹的那段一直埋在列表下面。
  // 留 LIVE_ROW_STICKY_MS 的余量：批与批之间短暂没有请求，行也不会上下跳
  const liveSeenAtRef = useRef<Map<string, number>>(new Map());
  const prioritizedRuntimeFiles = useMemo(() => {
    const now = Date.now();
    const liveSeenAt = liveSeenAtRef.current;
    if (!shouldPollRuntime) liveSeenAt.clear();
    const rank = (file: FileProgress) => {
      const isComplete = file.total > 0 && file.translated >= file.total;
      if (shouldPollRuntime && !isComplete) {
        if (file.activity) {
          liveSeenAt.set(file.filename, now);
          return 0;
        }
        const seenAt = liveSeenAt.get(file.filename);
        if (seenAt !== undefined && now - seenAt <= LIVE_ROW_STICKY_MS) return 0;
      }
      return file.translated > 0 && file.translated < file.total ? 1 : 2;
    };
    return runtimeFiles
      .map((file, index) => ({ file, index, rank: rank(file) }))
      .sort((a, b) => a.rank - b.rank || a.index - b.index)
      .map((item) => item.file);
  }, [runtimeFiles, shouldPollRuntime]);

  const unfinishedRuntimeFilesCount = useMemo(
    () => runtimeFiles.filter((file) => file.translated < file.total).length,
    [runtimeFiles],
  );

  const projectName = projectDir ? basenamePath(projectDir) : '';
  const backendUsageSummary = useMemo(
    () => projectDir
      ? summarizeBackendUsage(projectDir, projectBackendConfig)
      : { backend: translate("projects:projectTranslatePage.backend_backend_notSelectedProject"), model: translate("projects:projectTranslatePage.model_model_notSelectedProject"), profile: '' },
    // backendSelection 变化即 localStorage 里的项目后端配置变化，强制重算展示文案
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [uiLanguage, projectDir, projectBackendConfig, backendSelection],
  );
  const backendDisplayText = backendUsageSummary.model
    ? `${backendUsageSummary.backend}:${backendUsageSummary.model}`
    : backendUsageSummary.backend;
  const runtimeStage = (runtimeMatchesProject ? (runtime?.stage ?? '') : '').trim();
  const runtimeStageDetail = (runtimeMatchesProject ? (runtime?.current_file ?? '') : '').trim();
  const runtimeRetranslPendingCount = useMemo(
    () => (runtimeMatchesProject
      ? (runtime?.retransl_stats ?? []).reduce(
        (sum, item) => sum + Math.max(Number(item.count) || 0, 0),
        0,
      )
      : 0),
    [runtimeMatchesProject, runtime?.retransl_stats],
  );
  const statusTone = runtimeStage === '检查模型可用性' ? 'checking-availability' : (currentJob?.status ?? 'pending');
  // 后端报了阶段就直接显示阶段：只写「翻译中」看不出任务跑到哪一步（GenDic 的分词/人名/提取/审校尤其明显）
  const statusLabel = runtimeStage
    ? (runtimeStage === '检查模型可用性' ? translate("projects:projectTranslatePage.statusLabel_message_model") : runtimeStage)
    : getStatusLabel(currentJob?.status);
  const currentJobError = currentJob?.error?.trim() ?? '';
  const cancelledToastTitle = currentJob?.translator === 'GenDic' ? translate("projects:projectTranslatePage.cancelledToastTitle_message_genDicDoneStop") : translate("projects:projectTranslatePage.cancelledToastTitle_message_jobDoneCancel");
  const cancelledToastDescription = useMemo(() => {
    if (!currentJob || currentJob.status !== 'cancelled') return currentJobError;
    if (currentJob.translator !== 'GenDic') return currentJobError;
    const addedEntries = Number(currentJob.gendic_added_entries ?? 0);
    const dupEntries = Number(currentJob.gendic_duplicated_entries ?? 0);
    if (Number.isFinite(addedEntries) && addedEntries >= 0 && Number.isFinite(dupEntries) && dupEntries >= 0) {
      return translate("projects:projectTranslatePage.cancelledToastDescription_message_doneCurrentDictionaryEntryEntry", { addedEntries: addedEntries, dupEntries: dupEntries });
    }
    return currentJobError;
  }, [uiLanguage, currentJob, currentJobError]);
  // GenDic 跑的是分片/批次而不是句子：进度单位不能跟着普通翻译叫「句」
  const isGendicJob = currentJob?.translator === 'GenDic' || runtimeStage.startsWith('GenDic');
  const progressUnit = isGendicJob ? translate("projects:projectTranslatePage.progressUnit_message_item") : translate("projects:projectTranslatePage.progressUnit_message_sentence");
  const progressPercent = clampPercent(summary?.percent ?? 0);
  const progressPercentText = formatPercentDisplay(summary?.percent ?? 0);
  const translatedCount = summary?.translated ?? 0;
  const totalCount = summary?.total ?? 0;
  const remainingCount = Math.max(totalCount - translatedCount, 0);
  const workersActive = summary?.workers_active ?? 0;
  const workersConfigured = summary?.workers_configured ?? 0;
  const speedText = formatSpeed(summary?.translation_speed_lpm ?? 0, progressUnit);
  const etaText = formatEta(summary?.eta_seconds ?? 0);
  const elapsedText = formatElapsedTime(currentJob, nowMs);
  const updatedAtText = summary?.updated_at ? formatDate(summary.updated_at) : translate("projects:projectTranslatePage.updatedAtText_message_wait");

  useEffect(() => {
    if (!currentJob?.started_at) return;
    if (currentJob.status !== 'pending' && currentJob.status !== 'running') return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [currentJob?.started_at, currentJob?.status]);

  useEffect(() => {
    if (currentJob?.finished_at) setNowMs(Date.now());
  }, [currentJob?.finished_at]);

  useEffect(() => {
    const availableFiles = new Set(runtimeFiles.map((file) => file.filename));
    setSelectedSuccessFiles((current) => current.filter((filename) => availableFiles.has(filename)));
  }, [runtimeFiles]);

  const handleToggleSuccessFileFilter = useCallback((filename: string) => {
    setSelectedSuccessFiles((current) =>
      current.includes(filename) ? current.filter((name) => name !== filename) : [...current, filename],
    );
  }, []);
  const handleClearSuccessFileFilters = useCallback(() => {
    setSelectedSuccessFiles([]);
    shouldStickToBottomRef.current = true;
    window.requestAnimationFrame(() => {
      const container = successListRef.current;
      if (!container) return;
      container.scrollTop = container.scrollHeight;
    });
  }, []);

  const selectedSuccessFileSet = useMemo(() => new Set(selectedSuccessFiles), [selectedSuccessFiles]);
  const hasSelectedSuccessFileFilter = selectedSuccessFiles.length > 0;
  const selectedSuccessFileFilterSummary = useMemo(() => {
    if (!hasSelectedSuccessFileFilter) return '';
    const preview = selectedSuccessFiles.slice(0, 2);
    const extraCount = selectedSuccessFiles.length - preview.length;
    return extraCount > 0 ? translate("projects:projectTranslatePage.selectedSuccessFileFilterSummary_message_countFile", { value: preview.join('、'), count: selectedSuccessFiles.length }) : preview.join('、');
  }, [uiLanguage, hasSelectedSuccessFileFilter, selectedSuccessFiles]);

  const successEntries = useMemo(
    () => {
      const entries = runtimeMatchesProject ? runtime?.recent_successes ?? [] : [];
      const shouldFilterByFiles = selectedSuccessFileSet.size > 0;
      const filteredEntries = shouldFilterByFiles
        ? entries.filter((entry) => selectedSuccessFileSet.has(entry.filename || ''))
        : entries;
      // Backend returns newest-first; take the newest SUCCESS_RENDER_LIMIT and
      // reverse so the list renders oldest→newest (newest at the bottom).
      const trimmed = filteredEntries.slice(0, SUCCESS_RENDER_LIMIT);
      return [...trimmed].reverse();
    },
    [runtime?.recent_successes, runtimeMatchesProject, selectedSuccessFileSet],
  );

  const isCurrentProjectActive = currentJob?.status === 'pending' || currentJob?.status === 'running';
  // 翻译中途锁定后端切换：pending / running 时点开也不允许换，避免任务前后用了两个模型
  const isBackendSwitchLocked = isCurrentProjectActive || submitting || stopping;
  const backendSwitcherTitle = isBackendSwitchLocked
    ? translate("projects:projectTranslatePage.backendSwitchLocked")
    : translate("projects:projectTranslatePage.switchBackend");

  const handleSelectBackendProfile = useCallback((profile: string) => {
    if (!projectDir || isBackendSwitchLocked) return;
    setSelectedBackendProfile(projectDir, profile);
    setBackendSelection(profile);
    setShowBackendSwitcher(false);
  }, [projectDir, isBackendSwitchLocked]);

  useEffect(() => {
    if (!showBackendSwitcher || isBackendSwitchLocked) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setShowBackendSwitcher(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [showBackendSwitcher, isBackendSwitchLocked]);

  const backendProfileOptions = useMemo(() => backendProfileNames.map((name) => {
    const { modelName } = getProfileMeta(getBackendProfile(name));
    return { name, modelName };
  }), [backendProfileNames]);
  const primaryActionDisabled =
    connectionPhase !== 'online'
    || submitting
    || stopping
    || (!isCurrentProjectActive && !isSelectedTranslatorValid);
  const primaryActionLabel = isCurrentProjectActive ? (stopping ? translate("projects:projectTranslatePage.primaryActionLabel_message_stop") : translate("projects:projectTranslatePage.primaryActionLabel_message_stopTranslation")) : (submitting ? translate("projects:projectTranslatePage.primaryActionLabel_message_submit") : translate("projects:projectTranslatePage.primaryActionLabel_message_translation"));
  const handlePrimaryAction = isCurrentProjectActive ? handleStopTranslation : handleStartTranslation;
  const primaryActionClassName = isCurrentProjectActive ? 'project-translate-page__stop-button' : '';

  useEffect(() => {
    if (!continuousRetranslEnabled) {
      autoRetranslPrevPendingRef.current = null;
      autoRetranslStagnationRoundsRef.current = 0;
    }
  }, [continuousRetranslEnabled, projectId]);

  useEffect(() => {
    const jobId = currentJob?.job_id ?? null;
    const status = currentJob?.status ?? null;
    const prevJobId = autoRetranslPrevJobIdRef.current;
    const prevStatus = autoRetranslPrevJobStatusRef.current;
    autoRetranslPrevJobIdRef.current = jobId;
    autoRetranslPrevJobStatusRef.current = status;

    const justCompleted = Boolean(
      jobId
      && status === 'completed'
      && prevJobId === jobId
      && prevStatus !== 'completed',
    );
    if (!justCompleted) return;
    if (!continuousRetranslEnabled) return;

    if (runtimeRetranslPendingCount <= 0) {
      autoRetranslPrevPendingRef.current = 0;
      autoRetranslStagnationRoundsRef.current = 0;
      return;
    }

    const prevPending = autoRetranslPrevPendingRef.current;
    if (prevPending !== null && runtimeRetranslPendingCount >= prevPending) {
      autoRetranslStagnationRoundsRef.current += 1;
    } else {
      autoRetranslStagnationRoundsRef.current = 0;
    }
    autoRetranslPrevPendingRef.current = runtimeRetranslPendingCount;

    if (autoRetranslStagnationRoundsRef.current >= 3) return;

    const timer = window.setTimeout(() => {
      if (!isCurrentProjectActive) {
        handleStartTranslation();
      }
    }, 450);
    return () => window.clearTimeout(timer);
  }, [
    continuousRetranslEnabled,
    currentJob?.job_id,
    currentJob?.status,
    handleStartTranslation,
    isCurrentProjectActive,
    runtimeRetranslPendingCount,
  ]);

  const handleSuccessListScroll = useCallback((event: React.UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    const distanceToBottom = element.scrollHeight - element.clientHeight - element.scrollTop;
    shouldStickToBottomRef.current = distanceToBottom <= SUCCESS_STICK_BOTTOM_THRESHOLD_PX;
  }, []);
  const handleOpenFolder = useCallback((path: string) => {
    if (!path) return;
    void invoke('open_folder', { path });
  }, []);
  const inputFolderPath = projectDir ? joinPath(projectDir, INPUT_FOLDER_NAME) : '';
  const outputFolderPath = projectDir ? joinPath(projectDir, OUTPUT_FOLDER_NAME) : '';
  const cacheFolderPath = projectDir ? joinPath(projectDir, CACHE_FOLDER_NAME) : '';

  const recentErrors = runtimeMatchesProject ? (runtime?.recent_errors ?? []) : [];

  const isJobDone = currentJob?.status === 'completed';

  return (
    <div className="ptv2-page project-translate-page">
      {/* Cockpit: unified hero surface */}
      <section
        className={`ptv2-cockpit${shouldPollRuntime ? ' ptv2-cockpit--live' : ''}${isJobDone ? ' ptv2-cockpit--done' : ''}${stripBooting || barSurging ? ' ptv2-cockpit--arming' : ''}`}
      >
        <div className="ptv2-cockpit__deco" aria-hidden="true">
          <span className="ptv2-cockpit__orb ptv2-cockpit__orb--a" />
          <span className="ptv2-cockpit__orb ptv2-cockpit__orb--b" />
          <span className="ptv2-cockpit__grid" />
        </div>

        <div className="ptv2-cockpit__topline">
          <div className="ptv2-cockpit__brand">
            <span className="ptv2-cockpit__eyebrow">{translate("projects:projectTranslatePage.ptv2CockpitBrand_message_translationCockpit")}</span>
            <div className="ptv2-cockpit__title-row">
              <h1 className="ptv2-cockpit__title">{translate("projects:projectTranslatePage.ptv2CockpitTitle_h1_startTranslation")}{projectName ? (
                  <>
                    <span className="ptv2-cockpit__title-sep">·</span>
                    <span className="ptv2-cockpit__title-project">{projectName}</span>
                  </>
                ) : null}
              </h1>
              {projectName ? (
                <div className="project-translate-page__folder-menu ptv2-cockpit__folder-inline">
                  <button
                    type="button"
                    className="ptv2-folder-iconbtn"
                    disabled={!projectDir}
                    onClick={() => handleOpenFolder(projectDir)}
                    title={projectDir || translate("projects:projectTranslatePage.ptv2FolderIconbtn_title_openProjectFile")}
                    aria-label={translate("projects:projectTranslatePage.ptv2FolderIconbtn_ariaLabel_openProjectFile")}
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
                  <div className="project-translate-page__folder-menu-dropdown" role="menu">
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(projectDir)} title={projectDir} variant="secondary"><Icon name="folder-open" />{translate("projects:projectTranslatePage.projectTranslatePageFolderMenuItem_button_projectFile")}</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(inputFolderPath)} title={inputFolderPath} variant="secondary"><Icon name="inbox" />{translate("projects:projectTranslatePage.projectTranslatePageFolderMenuItem_button_file")}</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(outputFolderPath)} title={outputFolderPath} variant="secondary"><Icon name="upload" />{translate("projects:projectTranslatePage.projectTranslatePageFolderMenuItem_button_fileVariant2")}</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(cacheFolderPath)} title={cacheFolderPath} variant="secondary"><Icon name="database" />{translate("projects:projectTranslatePage.projectTranslatePageFolderMenuItem_button_cacheFile")}</Button>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
          <div className="ptv2-cockpit__statusline">
            <StatusBadge label={statusLabel} tone={statusTone} celebrate={justCompleted} />
            {/* 阶段内进度只给 GenDic（「人名 3/10」「审校 0/8」）：普通翻译的 current_file 是
                「正在翻哪个文件」，最近译文里已经带了文件名，再挂一颗胶囊只是噪音 */}
            {isGendicJob && runtimeStageDetail ? (
              <span className="ptv2-cockpit__stage-detail" title={`${runtimeStage}${runtimeStage ? ' · ' : ''}${runtimeStageDetail}`}>
                {runtimeStageDetail}
              </span>
            ) : null}
            <span className="ptv2-cockpit__tick" title={updatedAtText}>
              <span className="ptv2-cockpit__tick-dot" aria-hidden="true" />
              {updatedAtText}
            </span>
          </div>
        </div>

        <div className="ptv2-cockpit__gauge">
          <div className="ptv2-gauge__numbers">
            <div className="ptv2-gauge__percent-row">
              <span className="ptv2-gauge__percent">{progressPercentText}</span>
              <span className="ptv2-gauge__percent-sign">%</span>
            </div>
            <div className="ptv2-gauge__fraction">
              <span className="ptv2-gauge__fraction-done">{translatedCount}</span>
              <span className="ptv2-gauge__fraction-sep">/</span>
              <span className="ptv2-gauge__fraction-total">{totalCount}</span>
              <span className="ptv2-gauge__fraction-unit">{progressUnit}</span>
              <span className="ptv2-gauge__fraction-divider" aria-hidden="true" />
              <span className="ptv2-gauge__fraction-remain">{translate("projects:projectTranslatePage.ptv2GaugeFraction_message_text", { remainingCount: remainingCount })}</span>
            </div>
          </div>

          <div className="ptv2-gauge__bar-wrap">
            <div className="ptv2-gauge__bar-track">
              <div
                className={`ptv2-gauge__bar-fill${isJobDone ? ' ptv2-gauge__bar-fill--done' : ''}${justCompleted ? ' ptv2-gauge__bar-fill--complete' : ''}`}
                style={{ width: `${progressPercent}%` }}
              >
                <span className="ptv2-gauge__bar-shine" aria-hidden="true" />
              </div>
              <div className="ptv2-gauge__bar-ticks" aria-hidden="true">
                {[25, 50, 75].map((tick) => (
                  <span key={tick} className="ptv2-gauge__bar-tick" style={{ left: `${tick}%` }} />
                ))}
              </div>
            </div>
          </div>

          <div className="ptv2-cockpit__action">
            <label className="ptv2-cockpit__field">
              <span className="ptv2-cockpit__field-label">{translate("projects:projectTranslatePage.ptv2CockpitField_message_translation")}</span>
              <CustomSelect
                disabled={submitting || stopping || isCurrentProjectActive || translators.length === 0}
                onChange={(event) => {
                  const nextTranslator = event.target.value;
                  setSelectedTranslator(nextTranslator);
                  if (projectDir) setSelectedTranslatorTemplate(projectDir, nextTranslator);
                }}
                value={selectedTranslator}
              >
                {translators.length === 0 ? <option value="">{translate("projects:projectTranslatePage.ptv2CockpitField_message_empty")}</option> : null}
                {translators.map((item) => (
                  <option key={item.name} value={item.name}>{item.name} · {item.description}</option>
                ))}
              </CustomSelect>
            </label>

            <div className={`project-translate-page__launch-wrapper ptv2-launch-wrapper${launchPhase !== 'idle' ? ` project-translate-page__launch-${launchPhase}` : ''}`} ref={launchButtonRef}>
              {ripples.map((r) => (
                <span key={r.id} className="project-translate-page__launch-ripple" style={{ left: r.x - r.size / 2, top: r.y - r.size / 2, width: r.size, height: r.size }} />
              ))}
              {particles.map((p) => (
                <span key={p.id} className="project-translate-page__launch-particle" style={{ left: `${p.x}%`, top: `${p.y}%`, background: p.color, '--launch-particle-x': `${p.dx}px`, '--launch-particle-y': `${p.dy}px` } as React.CSSProperties} />
              ))}
              <Button
                className={`ptv2-launch-btn${isCurrentProjectActive ? ' ptv2-launch-btn--stop' : ''}${primaryActionClassName ? ` ${primaryActionClassName}` : ''}`}
                disabled={primaryActionDisabled}
                onClick={handlePrimaryAction}
              >
                <span className="ptv2-launch-btn__glyph" aria-hidden="true">
                  <Icon name={isCurrentProjectActive ? 'stop' : 'play'} />
                </span>
                <span className="ptv2-launch-btn__label">{primaryActionLabel}</span>
              </Button>
            </div>
          </div>
        </div>

        <div className="ptv2-cockpit__ribbon">
          <div className="ptv2-stat ptv2-stat--primary">
            <span className="ptv2-stat__value">{speedText}</span>
            <span className="ptv2-stat__label">{translate("projects:projectTranslatePage.ptv2StatPtv2StatPrimary_message_text")}</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{etaText}</span>
            <span className="ptv2-stat__label">{translate("projects:projectTranslatePage.ptv2Stat_message_text")}</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{workersActive}<span className="ptv2-stat__value-sep">/</span>{workersConfigured}</span>
            <span className="ptv2-stat__label">{translate("projects:projectTranslatePage.ptv2Stat_message_textVariant2")}</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{elapsedText}</span>
            <span className="ptv2-stat__label">{translate("projects:projectTranslatePage.ptv2Stat_message_done")}</span>
          </div>
          <div
            className={`ptv2-stat ptv2-stat--backend${isBackendSwitchLocked ? ' ptv2-stat--backend-locked' : ' ptv2-stat--backend-clickable project-translate-page__folder-menu ptv2-backend-menu'}${showBackendSwitcher && !isBackendSwitchLocked ? ' ptv2-backend-menu--open' : ''}`}
            title={translate(isBackendSwitchLocked ? "projects:projectTranslatePage.currentBackendLocked" : "projects:projectTranslatePage.currentBackendSwitch", { backendDisplayText: backendDisplayText })}
          >
            <button
              type="button"
              className="ptv2-stat__backend-btn"
              disabled={isBackendSwitchLocked || !projectDir}
              onClick={() => setShowBackendSwitcher((prev) => !prev)}
              onBlur={(event) => {
                // 焦点彻底离开整个菜单才收起，Tab 在菜单内移动时保持展开
                if (!event.currentTarget.parentElement?.contains(event.relatedTarget as Node)) {
                  setShowBackendSwitcher(false);
                }
              }}
              title={backendSwitcherTitle}
              aria-label={translate("projects:projectTranslatePage.currentBackendAriaLabel", { backendDisplayText: backendDisplayText })}
              aria-haspopup="menu"
              aria-expanded={showBackendSwitcher && !isBackendSwitchLocked}
            >
              <span className="ptv2-stat__backend-text">{backendDisplayText}</span>
            </button>
            <span className="ptv2-stat__label">{translate("projects:projectTranslatePage.ptv2StatPtv2StatBackend_message_currentBackend")}</span>
            {!isBackendSwitchLocked ? (
              <div className="project-translate-page__folder-menu-dropdown ptv2-backend-menu__dropdown" role="menu" aria-label={translate("projects:projectTranslatePage.switchBackendMenu")}>
                <div className="ptv2-backend-menu__list" role="group" aria-label={translate("projects:projectTranslatePage.backendConfigList")}>
                  <button
                    type="button"
                    role="menuitem"
                    className={`project-translate-page__folder-menu-item ptv2-backend-menu__item${backendSelection === '__default__' ? ' ptv2-backend-menu__item--active' : ''}`}
                    onClick={() => handleSelectBackendProfile('__default__')}
                    title={translate("projects:projectTranslatePage.switchBackendHint")}
                  >
                    <Icon name={backendSelection === '__default__' ? 'check' : 'globe'} />
                    <span className="ptv2-backend-menu__item-text">{translate(defaultBackendName ? "projects:projectTranslatePage.followDefaultBackend" : "projects:projectTranslatePage.defaultBackendNotSet", { name: defaultBackendName })}</span>
                  </button>
                  {backendProfileOptions.map(({ name, modelName }) => (
                    <button
                      key={name}
                      type="button"
                      role="menuitem"
                      className={`project-translate-page__folder-menu-item ptv2-backend-menu__item${backendSelection === name ? ' ptv2-backend-menu__item--active' : ''}`}
                      onClick={() => handleSelectBackendProfile(name)}
                      title={modelName && modelName !== '—' ? `${name} / ${modelName}` : name}
                    >
                      <Icon name={backendSelection === name ? 'check' : 'bot'} />
                      <span className="ptv2-backend-menu__item-text">
                        {name}
                        {modelName && modelName !== '—' ? <span className="ptv2-backend-menu__item-model"> / {modelName}</span> : null}
                      </span>
                    </button>
                  ))}
                </div>
                <div className="ptv2-backend-menu__divider" aria-hidden="true" />
                <button
                  type="button"
                  role="menuitem"
                  className="project-translate-page__folder-menu-item ptv2-backend-menu__item"
                  onClick={() => { setShowBackendSwitcher(false); navigate('/backend-profiles'); }}
                >
                  <Icon name="arrow-right" />
                  <span className="ptv2-backend-menu__item-text">{translate("projects:projectTranslatePage.openModelSettings")}</span>
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </section>

      {backendUsageSummary.missing === true && !isCurrentProjectActive ? (
        <InlineFeedback
          className="ptv2-alert"
          tone="warning"
          title={translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_title_emptyTranslationModel")}
          description={translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_description_projectDefaultModelNotSettingsDefaultModel")}
          autoDismiss={0}
          dedupeKey={null}
          action={<Button variant="secondary" onClick={() => navigate('/backend-profiles')}>{translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_action_modelSettings")}</Button>}
        />
      ) : null}
      {submitError ? <InlineFeedback tone="error" title={translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_title_translationFailed")} description={submitError} className="ptv2-alert inline-alert--floating" /> : null}
      {runtimeError ? <InlineFeedback tone="error" title={translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_title_runningStatus")} description={runtimeError} className="ptv2-alert inline-alert--floating" /> : null}
      {currentJob?.status === 'failed' && currentJobError ? (
        <InlineFeedback className="ptv2-alert inline-alert--floating" tone="error" title={translate("projects:projectTranslatePage.ptv2PageProjectTranslatePage_title_jobFailed")} description={currentJobError} />
      ) : null}
      {currentJob?.status === 'cancelled' && currentJobError && cancelledAlertJobId === currentJob.job_id ? (
        <InlineFeedback
          className="ptv2-alert inline-alert--floating"
          tone="info"
          title={cancelledToastTitle}
          description={cancelledToastDescription}
          autoDismiss={2800}
          onDismiss={() => setCancelledAlertJobId(null)}
        />
      ) : null}

      {/* Main area: success stream (wide) + recent errors (narrower) */}
      <div className="ptv2-main">
        <div className="ptv2-main__success">
          <Panel title={translate("projects:projectTranslatePage.ptv2MainSuccess_title_translationText")}>
            {hasSelectedSuccessFileFilter ? (
              <div className="runtime-success-filter-hint" role="status">
                <span className="runtime-success-filter-hint__text" title={selectedSuccessFiles.join('\n')}>{translate("projects:projectTranslatePage.runtimeSuccessFilterHint_message_doneFilterFile", { selectedSuccessFileFilterSummary: selectedSuccessFileFilterSummary })}</span>
                <button className="runtime-success-filter-hint__clear" onClick={handleClearSuccessFileFilters} type="button">{translate("projects:projectTranslatePage.runtimeSuccessFilterHint_message_cancelFilter")}</button>
              </div>
            ) : null}
            {successEntries.length ? (
              <div
                className="runtime-event-list runtime-event-list--success ptv2-eventlist"
                onScroll={handleSuccessListScroll}
                ref={successListRef}
              >
                {successEntries.map((entry) => (
                  <RuntimeSuccessRow
                    entry={entry}
                    isFresh={freshSuccessIds.includes(entry.id)}
                    isSuccessFileFilterActive={selectedSuccessFileSet.has(entry.filename || '')}
                    onToggleSuccessFileFilter={handleToggleSuccessFileFilter}
                    nameDict={nameDict}
                    key={entry.id}
                  />
                ))}
              </div>
            ) : (
              <EmptyState title={translate("projects:projectTranslatePage.ptv2MainSuccess_title_emptyTranslationText")} description={translate("projects:projectTranslatePage.ptv2MainSuccess_description_jobStartSentence")} />
            )}
          </Panel>
        </div>

        <div className="ptv2-main__side">
          <section className="panel ptv2-tabpanel">
            <header className="panel__header ptv2-tabpanel__header">
              <div role="tablist" aria-label={translate("projects:projectTranslatePage.ptv2Tabs_ariaLabel_text")} className="ptv2-tabs">
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'files'}
                  className={`ptv2-tab${rightTab === 'files' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('files')}
                >
                  <span className="ptv2-tab__label">{translate("projects:projectTranslatePage.ptv2Tabs_message_fileProgress")}</span>
                  {unfinishedRuntimeFilesCount > 0 ? (
                    <span className="ptv2-tab__badge" title={translate("projects:projectTranslatePage.ptv2TabBadge_title_notCompleteFileCount")}>{unfinishedRuntimeFilesCount}</span>
                  ) : null}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'errors'}
                  className={`ptv2-tab${rightTab === 'errors' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('errors')}
                >
                  <span className="ptv2-tab__label">{translate("projects:projectTranslatePage.ptv2Tabs_message_error")}</span>
                  {recentErrors.length > 0 ? (
                    <span className="ptv2-tab__badge ptv2-tab__badge--danger">{recentErrors.length}</span>
                  ) : null}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'retransl'}
                  className={`ptv2-tab${rightTab === 'retransl' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('retransl')}
                >
                  <span className="ptv2-tab__label">{translate("projects:projectTranslatePage.ptv2Tabs_message_retranslateEntry")}</span>
                  {retranslKeys.length > 0 ? (
                    <span className="ptv2-tab__badge">{retranslKeys.length}</span>
                  ) : null}
                </button>
              </div>
            </header>
            <div className="panel__body ptv2-tabpanel__body">
              <div className="ptv2-tabpanel__pane" key={rightTab}>
              {rightTab === 'errors' ? (
                recentErrors.length ? (
                  <div className="runtime-event-list runtime-event-list--error ptv2-eventlist">
                    {recentErrors.map((entry) => (
                      <RuntimeErrorRow entry={entry} key={entry.id} />
                    ))}
                  </div>
                ) : (
                  <EmptyState title={translate("projects:projectTranslatePage.ptv2TabpanelPane_title_emptyError")} description={translate("projects:projectTranslatePage.ptv2TabpanelPane_description_errorError")} />
                )
              ) : rightTab === 'files' ? (
                prioritizedRuntimeFiles.length > 0 ? (
                  <div className="ptv2-filelist">
                    {prioritizedRuntimeFiles.map((file) => (
                      <FileProgressRow
                        key={file.filename}
                        file={file}
                        isRunning={shouldPollRuntime}
                        isSuccessFileFilterActive={selectedSuccessFileSet.has(file.filename)}
                        onToggleSuccessFileFilter={handleToggleSuccessFileFilter}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState title={translate("projects:projectTranslatePage.ptv2TabpanelPane_title_emptyFileProgress")} description={translate("projects:projectTranslatePage.ptv2TabpanelPane_description_translationFileProgress")} />
                )
              ) : (
                <div className="ptv2-retransl-pane">
                  <div className="ptv2-retransl-auto">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={continuousRetranslEnabled}
                      className={`ptv2-retransl-auto__toggle${continuousRetranslEnabled ? ' ptv2-retransl-auto__toggle--on' : ''}`}
                      onClick={() => setContinuousRetranslEnabled((prev) => !prev)}
                    >
                      <span className="ptv2-retransl-auto__toggle-track" aria-hidden="true">
                        <span className="ptv2-retransl-auto__toggle-thumb" />
                      </span>
                      <span className="ptv2-retransl-auto__toggle-label">{translate("projects:projectTranslatePage.ptv2RetranslAuto_message_autoRetranslate")}</span>
                    </button>
                    <p className="ptv2-retransl-auto__hint">{translate("projects:projectTranslatePage.ptv2RetranslAuto_message_translationAutoTranslation3RetranslateSentence")}</p>
                  </div>
                  {retranslKeys.length > 0 ? (
                    <ul className="ptv2-retransl-list">
                      {retranslKeys.map((item, idx) => (
                        <li
                          className="ptv2-retransl-list__item ptv2-retransl-list__item--link"
                          key={`${idx}-${item.key}`}
                          role="button"
                          tabIndex={0}
                          title={translate("projects:projectTranslatePage.ptv2RetranslListItemPtv2RetranslListItemLink_title_configEditRetranslate")}
                          onClick={() => navigate(`/project/${projectId}/config?section=retranslKey`)}
                          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(`/project/${projectId}/config?section=retranslKey`); } }}
                        >
                          <span className="ptv2-retransl-list__index">{idx + 1}</span>
                          <span className="ptv2-retransl-list__text">{item.key}</span>
                          <span className="ptv2-retransl-list__count">{translate("projects:projectTranslatePage.ptv2RetranslListItemPtv2RetranslListItemLink_message_sentence", { count: item.count })}</span>
                          <span className="ptv2-retransl-list__arrow">›</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState title={translate("projects:projectTranslatePage.ptv2RetranslPane_title_emptyRetranslateEntry")} description={translate("projects:projectTranslatePage.ptv2RetranslPane_description_projectConfigRetranslateAddTranslationSentenceTranslation")} />
                  )}
                </div>
              )}
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
