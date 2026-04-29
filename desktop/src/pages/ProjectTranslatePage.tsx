import { invoke } from '@tauri-apps/api/core';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { Panel } from '../components/Panel';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState, InlineFeedback } from '../components/page-state';
import { useConnection } from '../features/connection/ConnectionContext';
import { useNameDict } from '../lib/useNameDict';
import {
  type Job,
  type ProjectRuntimeResponse,
  type SubmitJobPayload,
  fetchJobs,
  fetchProjectConfig,
  fetchProjectRuntime,
  getSelectedTranslatorTemplate,
  getSelectedBackendProfileJobPayload,
  setSelectedTranslatorTemplate,
  stopProjectTranslation,
  submitJob } from '../lib/api';
import { normalizeError } from '../lib/errors';
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
const SUCCESS_STICK_BOTTOM_THRESHOLD_PX = 24;
// Backend keeps up to 100 success cards per translating file, but the UI only
// renders the newest 100 cards (after filtering) to keep scrolling performant.
const SUCCESS_RENDER_LIMIT = 100;
const INPUT_FOLDER_NAME = 'gt_input';
const OUTPUT_FOLDER_NAME = 'gt_output';
const CACHE_FOLDER_NAME = 'transl_cache';
const CONTINUOUS_RETRANSL_STORAGE_KEY = 'galtransl-continuous-retransl-by-project';

const HIDDEN_TRANSLATORS = new Set(['rebuilda', 'rebuildr', 'show-plugs', 'dump-name']);

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
  const { projectDir, projectId, configFileName } = ctx;
  const navigate = useNavigate();
  const { connectionPhase, translators, loadJobs } = useConnection();
  const reducedMotion = usePrefersReducedMotion();
  const { nameDict } = useNameDict(projectId);

  const [jobs, setJobs] = useState<Job[]>(() => cachedJobs);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [selectedTranslator, setSelectedTranslator] = useState('');
  const [runtime, setRuntime] = useState<ProjectRuntimeResponse | null>(
    () => (projectId ? cachedRuntimeByProject.get(projectId) ?? null : null),
  );
  const [selectedSuccessFiles, setSelectedSuccessFiles] = useState<string[]>([]);
  const [freshSuccessIds, setFreshSuccessIds] = useState<string[]>([]);
  const seenSuccessIdsRef = useRef<Set<string>>(new Set());
  const successListRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const [rightTab, setRightTab] = useState<'errors' | 'files' | 'retransl'>('errors');
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
    const hasPersisted = translators.some((item) => item.name === persisted);
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
        setRuntimeError(normalizeError(error, '读取运行时快照失败'));
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
  const currentJob = runtimeMatchesProject
    ? (runtime?.job ?? (currentProjectJobFallback ? toRuntimeJob(currentProjectJobFallback) : null))
    : (currentProjectJobFallback ? toRuntimeJob(currentProjectJobFallback) : null);
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
    // Auxiliary flows like 构建输出 (rebuilda/rebuildr) and 提取人名表 (dump-name)
    // reuse the job pipeline but must not hijack the user's translator template
    // selection in the cockpit dropdown.
    if (HIDDEN_TRANSLATORS.has(currentJob.translator)) return;
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
        const message = normalizeError(error, '提交任务失败');
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
      setSubmitError('请选择翻译模板。');
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
      const message = normalizeError(error, '停止任务失败');
      setSubmitError(message);
      void refreshRuntime();
      void refreshJobs();
    } finally {
      setStopping(false);
    }
  }, [projectId, refreshJobs, refreshRuntime]);

  const summary = runtimeMatchesProject ? (runtime?.summary ?? null) : null;
  const runtimeFiles = runtimeMatchesProject ? (runtime?.files ?? []) : [];
  const prioritizedRuntimeFiles = useMemo(() => {
    return runtimeFiles
      .map((file, index) => ({
        file,
        index,
        isTranslating: file.translated > 0 && file.translated < file.total }))
      .sort((a, b) => {
        if (a.isTranslating !== b.isTranslating) return a.isTranslating ? -1 : 1;
        return a.index - b.index;
      })
      .map((item) => item.file);
  }, [runtimeFiles]);

  const unfinishedRuntimeFilesCount = useMemo(
    () => runtimeFiles.filter((file) => file.translated < file.total).length,
    [runtimeFiles],
  );

  const projectName = projectDir ? projectDir.split(/[/\\]/).filter(Boolean).pop() || '' : '';
  const runtimeStage = (runtimeMatchesProject ? (runtime?.stage ?? '') : '').trim();
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
  const statusLabel = runtimeStage === '检查模型可用性' ? '测试模型可用性' : getStatusLabel(currentJob?.status);
  const currentJobError = currentJob?.error?.trim() ?? '';
  const progressPercent = clampPercent(summary?.percent ?? 0);
  const progressPercentText = formatPercentDisplay(summary?.percent ?? 0);
  const translatedCount = summary?.translated ?? 0;
  const totalCount = summary?.total ?? 0;
  const remainingCount = Math.max(totalCount - translatedCount, 0);
  const workersActive = summary?.workers_active ?? 0;
  const workersConfigured = summary?.workers_configured ?? 0;
  const speedText = formatSpeed(summary?.translation_speed_lpm ?? 0);
  const etaText = formatEta(summary?.eta_seconds ?? 0);
  const elapsedText = formatElapsedTime(currentJob, nowMs);
  const updatedAtText = summary?.updated_at ? formatDate(summary.updated_at) : '等待首次快照';

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
    return extraCount > 0 ? `${preview.join('、')} 等 ${selectedSuccessFiles.length} 个文件` : preview.join('、');
  }, [hasSelectedSuccessFileFilter, selectedSuccessFiles]);

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
  const primaryActionDisabled =
    connectionPhase !== 'online'
    || submitting
    || stopping
    || (!isCurrentProjectActive && !isSelectedTranslatorValid);
  const primaryActionLabel = isCurrentProjectActive ? (stopping ? '停止中…' : '停止翻译') : (submitting ? '提交中…' : '启动翻译');
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
  const normalizedProjectDir = projectDir.replace(/[\\/]+$/, '');
  const inputFolderPath = projectDir ? `${normalizedProjectDir}\\${INPUT_FOLDER_NAME}` : '';
  const outputFolderPath = projectDir ? `${normalizedProjectDir}\\${OUTPUT_FOLDER_NAME}` : '';
  const cacheFolderPath = projectDir ? `${normalizedProjectDir}\\${CACHE_FOLDER_NAME}` : '';

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
            <span className="ptv2-cockpit__eyebrow">Translation Cockpit</span>
            <div className="ptv2-cockpit__title-row">
              <h1 className="ptv2-cockpit__title">
                翻译工作台
                {projectName ? (
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
                  <div className="project-translate-page__folder-menu-dropdown" role="menu">
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(projectDir)} title={projectDir} variant="secondary">📂 项目文件夹</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(inputFolderPath)} title={inputFolderPath} variant="secondary">📥 输入文件夹</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(outputFolderPath)} title={outputFolderPath} variant="secondary">📤 输出文件夹</Button>
                    <Button className="project-translate-page__folder-menu-item" disabled={!projectDir} onClick={() => handleOpenFolder(cacheFolderPath)} title={cacheFolderPath} variant="secondary">💾 缓存文件夹</Button>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
          <div className="ptv2-cockpit__statusline">
            <StatusBadge label={statusLabel} tone={statusTone} celebrate={justCompleted} />
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
              <span className="ptv2-gauge__fraction-unit">句</span>
              <span className="ptv2-gauge__fraction-divider" aria-hidden="true" />
              <span className="ptv2-gauge__fraction-remain">剩余 {remainingCount}</span>
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
              <span className="ptv2-cockpit__field-label">翻译模板</span>
              <CustomSelect
                disabled={submitting || stopping || isCurrentProjectActive || translators.length === 0}
                onChange={(event) => {
                  const nextTranslator = event.target.value;
                  setSelectedTranslator(nextTranslator);
                  if (projectDir) setSelectedTranslatorTemplate(projectDir, nextTranslator);
                }}
                value={selectedTranslator}
              >
                {translators.length === 0 ? <option value="">暂无可用模板</option> : null}
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
                <span className="ptv2-launch-btn__glyph" aria-hidden="true">{isCurrentProjectActive ? '■' : '▶'}</span>
                <span className="ptv2-launch-btn__label">{primaryActionLabel}</span>
              </Button>
            </div>
          </div>
        </div>

        <div className="ptv2-cockpit__ribbon">
          <div className="ptv2-stat ptv2-stat--primary">
            <span className="ptv2-stat__value">{speedText}</span>
            <span className="ptv2-stat__label">实时速度</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{etaText}</span>
            <span className="ptv2-stat__label">预计剩余</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{workersActive}<span className="ptv2-stat__value-sep">/</span>{workersConfigured}</span>
            <span className="ptv2-stat__label">工作线程</span>
          </div>
          <div className="ptv2-stat">
            <span className="ptv2-stat__value">{elapsedText}</span>
            <span className="ptv2-stat__label">已用时长</span>
          </div>
        </div>
      </section>

      {submitError ? <InlineFeedback tone="error" title="启动翻译失败" description={submitError} className="ptv2-alert inline-alert--floating" /> : null}
      {runtimeError ? <InlineFeedback tone="error" title="运行时状态异常" description={runtimeError} className="ptv2-alert inline-alert--floating" /> : null}
      {currentJob?.status === 'failed' && currentJobError ? (
        <InlineFeedback className="ptv2-alert inline-alert--floating" tone="error" title="任务失败" description={currentJobError} />
      ) : null}
      {currentJob?.status === 'cancelled' && currentJobError && cancelledAlertJobId === currentJob.job_id ? (
        <InlineFeedback
          className="ptv2-alert inline-alert--floating"
          tone="info"
          title="任务已取消"
          description={currentJobError}
          autoDismiss={2800}
          onDismiss={() => setCancelledAlertJobId(null)}
        />
      ) : null}

      {/* Main area: success stream (wide) + recent errors (narrower) */}
      <div className="ptv2-main">
        <div className="ptv2-main__success">
          <Panel title="成功句流">
            {hasSelectedSuccessFileFilter ? (
              <div className="runtime-success-filter-hint" role="status">
                <span className="runtime-success-filter-hint__text" title={selectedSuccessFiles.join('\n')}>
                  已筛选文件：{selectedSuccessFileFilterSummary}
                </span>
                <button className="runtime-success-filter-hint__clear" onClick={handleClearSuccessFileFilters} type="button">
                  取消所有筛选
                </button>
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
              <EmptyState title="还没有成功句流" description="任务开始输出后，最近成功的句子会滚动显示在这里。" />
            )}
          </Panel>
        </div>

        <div className="ptv2-main__side">
          <section className="panel ptv2-tabpanel">
            <header className="panel__header ptv2-tabpanel__header">
              <div role="tablist" aria-label="辅助信息" className="ptv2-tabs">
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'errors'}
                  className={`ptv2-tab${rightTab === 'errors' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('errors')}
                >
                  <span className="ptv2-tab__label">最近错误</span>
                  {recentErrors.length > 0 ? (
                    <span className="ptv2-tab__badge ptv2-tab__badge--danger">{recentErrors.length}</span>
                  ) : null}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'files'}
                  className={`ptv2-tab${rightTab === 'files' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('files')}
                >
                  <span className="ptv2-tab__label">文件进度</span>
                  {unfinishedRuntimeFilesCount > 0 ? (
                    <span className="ptv2-tab__badge" title="未完成的文件数量">{unfinishedRuntimeFilesCount}</span>
                  ) : null}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={rightTab === 'retransl'}
                  className={`ptv2-tab${rightTab === 'retransl' ? ' ptv2-tab--active' : ''}`}
                  onClick={() => setRightTab('retransl')}
                >
                  <span className="ptv2-tab__label">重翻词条</span>
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
                  <EmptyState title="最近没有错误" description="接口错误、解析错误会显示在这里。" />
                )
              ) : rightTab === 'files' ? (
                prioritizedRuntimeFiles.length > 0 ? (
                  <div className="ptv2-filelist">
                    {prioritizedRuntimeFiles.map((file) => (
                      <FileProgressRow
                        key={file.filename}
                        file={file}
                        isSuccessFileFilterActive={selectedSuccessFileSet.has(file.filename)}
                        onToggleSuccessFileFilter={handleToggleSuccessFileFilter}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState title="暂无文件进度" description="启动翻译后，文件级进度会在这里展开。" />
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
                      <span className="ptv2-retransl-auto__toggle-label">自动持续重翻</span>
                    </button>
                    <p className="ptv2-retransl-auto__hint">
                      翻译结束后，自动启动翻译，直到连续3次待重翻的句子仍不减少。
                    </p>
                  </div>
                  {retranslKeys.length > 0 ? (
                    <ul className="ptv2-retransl-list">
                      {retranslKeys.map((item, idx) => (
                        <li
                          className="ptv2-retransl-list__item ptv2-retransl-list__item--link"
                          key={`${idx}-${item.key}`}
                          role="button"
                          tabIndex={0}
                          title="点击跳转到配置编辑-重翻关键字"
                          onClick={() => navigate(`/project/${projectId}/config?section=retranslKey`)}
                          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(`/project/${projectId}/config?section=retranslKey`); } }}
                        >
                          <span className="ptv2-retransl-list__index">{idx + 1}</span>
                          <span className="ptv2-retransl-list__text">{item.key}</span>
                          <span className="ptv2-retransl-list__count">{item.count} 句</span>
                          <span className="ptv2-retransl-list__arrow">›</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyState title="暂无重翻词条" description="在项目配置「重翻关键字」中添加后，启动翻译时命中的句子会被重新翻译。" />
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
