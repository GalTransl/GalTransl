import { invoke } from '@tauri-apps/api/core';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Button } from '../components/Button';
import { EmptyState } from '../components/EmptyState';
import { Panel } from '../components/Panel';
import { StatusBadge } from '../components/StatusBadge';
import { useConnection } from '../features/connection/ConnectionContext';
import { speakerStyle } from '../lib/speaker';
import {
  ApiError,
  type FileProgress,
  type Job,
  type ProjectRuntimeErrorEntry,
  type ProjectRuntimeResponse,
  type ProjectRuntimeSuccessEntry,
  type RuntimeJob,
  type SubmitJobPayload,
  fetchJobs,
  fetchProjectRuntime,
  getSelectedTranslatorTemplate,
  getSelectedBackendProfile,
  setSelectedTranslatorTemplate,
  stopProjectTranslation,
  submitJob,
} from '../lib/api';

const JOB_POLL_INTERVAL_MS = 2000;
const RUNTIME_POLL_INTERVAL_MS = 1000;
const SUCCESS_STICK_BOTTOM_THRESHOLD_PX = 24;
const INPUT_FOLDER_NAME = 'gt_input';
const OUTPUT_FOLDER_NAME = 'gt_output';
const CACHE_FOLDER_NAME = 'transl_cache';
const LAUNCH_CHARGE_MS = 500;
const LAUNCH_BLAST_MS = 600;
const STRIP_BOOT_MS = 1200;
const BAR_SURGE_MS = 800;
const COMPLETE_CELEBRATE_MS = 1200;
const PARTICLE_COUNT = 12;
const PARTICLE_DISTANCE_MIN = 30;
const PARTICLE_DISTANCE_MAX = 80;

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

export function ProjectTranslatePage() {
  const { projectDir, projectId, configFileName } = useOutletContext<OutletContext>();
  const { connectionPhase, translators, loadJobs } = useConnection();

  const [jobs, setJobs] = useState<Job[]>([]);
  const [refreshingJobs, setRefreshingJobs] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [selectedTranslator, setSelectedTranslator] = useState('');
  const [runtime, setRuntime] = useState<ProjectRuntimeResponse | null>(null);
  const [selectedSuccessFiles, setSelectedSuccessFiles] = useState<string[]>([]);
  const [freshSuccessIds, setFreshSuccessIds] = useState<string[]>([]);
  const seenSuccessIdsRef = useRef<Set<string>>(new Set());
  const successListRef = useRef<HTMLDivElement | null>(null);
  const fileProgressListRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const [hasFileProgressScrollbar, setHasFileProgressScrollbar] = useState(false);
  const [launchPhase, setLaunchPhase] = useState<'idle' | 'charging' | 'blasting'>('idle');
  const [stripBooting, setStripBooting] = useState(false);
  const [barSurging, setBarSurging] = useState(false);
  const [justCompleted, setJustCompleted] = useState(false);
  const [particles, setParticles] = useState<Array<{ id: number; x: number; y: number; dx: number; dy: number; color: string }>>([]);
  const [ripples, setRipples] = useState<Array<{ id: number; x: number; y: number; size: number }>>([]);
  const launchButtonRef = useRef<HTMLDivElement | null>(null);
  const prevShouldPollRuntimeRef = useRef(false);
  const prevJobCompletedRef = useRef<boolean | null>(null);

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

  const refreshJobs = useCallback(async (silent = false) => {
    if (!silent) setRefreshingJobs(true);
    try {
      const nextJobs = await fetchJobs();
      setJobs(nextJobs);
      setJobsError(null);
    } catch (error) {
      setJobsError(getErrorMessage(error, '读取任务列表失败'));
    } finally {
      if (!silent) setRefreshingJobs(false);
    }
  }, []);

  const refreshRuntime = useCallback(async (silent = false) => {
    if (!projectId) {
      setRuntime(null);
      return;
    }
    try {
      const data = await fetchProjectRuntime(projectId);
      setRuntime(data);
      setRuntimeError(null);
    } catch (error) {
      if (!silent) {
        setRuntimeError(getErrorMessage(error, '读取运行时快照失败'));
      }
    }
  }, [projectId]);

  useEffect(() => {
    setRuntime(null);
    setRuntimeError(null);
    void refreshJobs();
    void refreshRuntime();
  }, [refreshJobs, refreshRuntime]);

  useEffect(() => {
    const poller = window.setInterval(() => {
      void loadJobs(true);
      void refreshJobs(true);
    }, JOB_POLL_INTERVAL_MS);
    return () => window.clearInterval(poller);
  }, [loadJobs, refreshJobs]);

  const runningJobs = useMemo(
    () => jobs.filter((job) => job.status === 'pending' || job.status === 'running'),
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

  // Fire strip boot-up and bar surge when translation first starts
  useEffect(() => {
    const justStarted = shouldPollRuntime && !prevShouldPollRuntimeRef.current;
    prevShouldPollRuntimeRef.current = shouldPollRuntime;
    if (!justStarted) return;

    setStripBooting(true);
    setBarSurging(true);
    const stripTimer = window.setTimeout(() => setStripBooting(false), STRIP_BOOT_MS);
    const barTimer = window.setTimeout(() => setBarSurging(false), BAR_SURGE_MS);
    return () => {
      window.clearTimeout(stripTimer);
      window.clearTimeout(barTimer);
    };
  }, [shouldPollRuntime]);

  // Fire celebration animation when translation completes
  useEffect(() => {
    const isCompleted = currentJob?.status === 'completed';
    const wasPreviously = prevJobCompletedRef.current;
    prevJobCompletedRef.current = !!isCompleted;
    // Only celebrate on transition: not-completed → completed (skip initial load)
    if (!isCompleted || wasPreviously !== false) return;

    setJustCompleted(true);
    const timer = window.setTimeout(() => setJustCompleted(false), COMPLETE_CELEBRATE_MS);
    return () => window.clearTimeout(timer);
  }, [currentJob?.status]);

  useEffect(() => {
    if (!projectDir || !runtimeMatchesProject || !currentJob?.translator) return;
    setSelectedTranslator((current) => (current === currentJob.translator ? current : currentJob.translator));
    setSelectedTranslatorTemplate(projectDir, currentJob.translator);
  }, [currentJob?.translator, projectDir, runtimeMatchesProject]);

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

    for (const entry of successEntries) {
      seen.add(entry.id);
    }

    if (nextFresh.length === 0) return;

    setFreshSuccessIds((current) => Array.from(new Set([...current, ...nextFresh])));

    const timeout = window.setTimeout(() => {
      setFreshSuccessIds((current) => current.filter((id) => !nextFresh.includes(id)));
    }, 2200);

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
        const message = getErrorMessage(error, '提交任务失败');
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

    // Spawn ripple at button center
    const btnEl = launchButtonRef.current;
    if (btnEl) {
      const rect = btnEl.getBoundingClientRect();
      const cx = rect.width / 2;
      const cy = rect.height / 2;
      setRipples([{ id: Date.now(), x: cx, y: cy, size: Math.max(rect.width, rect.height) }]);
      window.setTimeout(() => setRipples([]), 700);
    }

    // Phase 1: charge-up
    setLaunchPhase('charging');

    window.setTimeout(() => {
      // Phase 2: blast-off + particle burst
      setLaunchPhase('blasting');

      // Spawn particles
      const newParticles = Array.from({ length: PARTICLE_COUNT }, (_, i) => {
        const angle = (Math.PI * 2 * i) / PARTICLE_COUNT + (Math.random() - 0.5) * 0.4;
        const dist = PARTICLE_DISTANCE_MIN + Math.random() * (PARTICLE_DISTANCE_MAX - PARTICLE_DISTANCE_MIN);
        const colors = ['#3b82f6', '#22d3ee', '#34d399', '#a78bfa', '#fbbf24'];
        return {
          id: Date.now() + i,
          x: 50,
          y: 50,
          dx: Math.cos(angle) * dist,
          dy: Math.sin(angle) * dist,
          color: colors[i % colors.length],
        };
      });
      setParticles(newParticles);
      window.setTimeout(() => setParticles([]), 800);

      // Submit the actual job
      const backendProfile = getSelectedBackendProfile(projectDir);
      void handleSubmit({
        config_file_name: configFileName || 'config.yaml',
        project_dir: projectDir,
        translator: selectedTranslator,
        ...(backendProfile ? { backend_profile: backendProfile } : {}),
      });

      // Phase 3: settle
      window.setTimeout(() => setLaunchPhase('idle'), LAUNCH_BLAST_MS);
    }, LAUNCH_CHARGE_MS);
  }, [configFileName, handleSubmit, isSelectedTranslatorValid, projectDir, selectedTranslator]);

  const handleStopTranslation = useCallback(async () => {
    if (!projectId) return;
    setStopping(true);
    setSubmitError(null);
    try {
      const stoppedJob = await stopProjectTranslation(projectId);
      setJobs((current) =>
        current.map((job) =>
          job.job_id === stoppedJob.job_id
            ? {
                ...job,
                status: stoppedJob.status,
                success: stoppedJob.success,
              }
            : job,
        ),
      );
      await refreshRuntime(true);
      await refreshJobs(true);
    } catch (error) {
      const message = getErrorMessage(error, '停止任务失败');
      setSubmitError(message);
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
        isTranslating: file.translated > 0 && file.translated < file.total,
      }))
      .sort((a, b) => {
        if (a.isTranslating !== b.isTranslating) {
          return a.isTranslating ? -1 : 1;
        }
        return a.index - b.index;
      })
      .map((item) => item.file);
  }, [runtimeFiles]);

  useEffect(() => {
    if (runtimeFiles.length === 0) {
      setHasFileProgressScrollbar(false);
      return;
    }

    const container = fileProgressListRef.current;
    if (!container) {
      setHasFileProgressScrollbar(false);
      return;
    }

    const syncScrollbarState = () => {
      setHasFileProgressScrollbar(container.scrollHeight > container.clientHeight + 1);
    };

    syncScrollbarState();

    const resizeObserver = new ResizeObserver(syncScrollbarState);
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
    };
  }, [runtimeFiles.length]);
  const projectName = projectDir ? projectDir.split(/[/\\]/).filter(Boolean).pop() || '' : '';
  const updatedAtText = summary?.updated_at ? formatDate(summary.updated_at) : '等待首次快照';
  const statusTone = currentJob?.status ?? 'pending';
  const statusLabel = getStatusLabel(currentJob?.status);
  const progressPercent = clampPercent(summary?.percent ?? 0);
  const translatedCount = summary?.translated ?? 0;
  const totalCount = summary?.total ?? 0;
  const workersActive = summary?.workers_active ?? 0;
  const workersConfigured = summary?.workers_configured ?? 0;
  const speedText = formatSpeed(summary?.translation_speed_lpm ?? 0);
  const etaText = formatEta(summary?.eta_seconds ?? 0);

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
      return [...filteredEntries].reverse();
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

  return (
    <div className="project-translate-page">
      <div className="project-translate-page__header">
        <div className="project-translate-page__header-row">
          <h1>翻译工作台{projectName ? `-${projectName}` : ''}</h1>
          <div className="project-translate-page__folder-menu">
            <Button
              disabled={!projectDir}
              onClick={() => handleOpenFolder(projectDir)}
              title={projectDir}
              variant="secondary"
            >
              📂 打开项目文件夹
            </Button>
            <div className="project-translate-page__folder-menu-dropdown" role="menu">
              <Button
                className="project-translate-page__folder-menu-item"
                disabled={!projectDir}
                onClick={() => handleOpenFolder(inputFolderPath)}
                title={inputFolderPath}
                variant="secondary"
              >
                📥 输入文件夹
              </Button>
              <Button
                className="project-translate-page__folder-menu-item"
                disabled={!projectDir}
                onClick={() => handleOpenFolder(outputFolderPath)}
                title={outputFolderPath}
                variant="secondary"
              >
                📤 输出文件夹
              </Button>
              <Button
                className="project-translate-page__folder-menu-item"
                disabled={!projectDir}
                onClick={() => handleOpenFolder(cacheFolderPath)}
                title={cacheFolderPath}
                variant="secondary"
              >
                💾 缓存文件夹
              </Button>
            </div>
          </div>
        </div>
        <p>启动翻译任务后，这里会切换为运行时仪表盘，持续展示状态、吞吐、错误与成功句流。</p>
      </div>

      <div className="project-translate-page__content">
        <div className="project-translate-page__sidebar">
          <Panel title="翻译控制">
            <div className="form-stack">
              <label className="field">
                <span>翻译模板</span>
                <select
                  disabled={submitting || stopping || isCurrentProjectActive || translators.length === 0}
                  onChange={(event) => {
                    const nextTranslator = event.target.value;
                    setSelectedTranslator(nextTranslator);
                    if (projectDir) {
                      setSelectedTranslatorTemplate(projectDir, nextTranslator);
                    }
                  }}
                  value={selectedTranslator}
                >
                  {translators.length === 0 ? <option value="">暂无可用模板</option> : null}
                  {translators.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name} · {item.description}
                    </option>
                  ))}
                </select>
              </label>

              {submitError ? (
                <div className="inline-alert inline-alert--error" role="alert">
                  {submitError}
                </div>
              ) : null}

              <div className={`form-actions${launchPhase !== 'idle' ? ` project-translate-page__launch-${launchPhase}` : ''}`}>
                <div className="project-translate-page__launch-wrapper" ref={launchButtonRef}>
                  {ripples.map((r) => (
                    <span
                      key={r.id}
                      className="project-translate-page__launch-ripple"
                      style={{
                        left: r.x - r.size / 2,
                        top: r.y - r.size / 2,
                        width: r.size,
                        height: r.size,
                      }}
                    />
                  ))}
                  {particles.map((p) => (
                    <span
                      key={p.id}
                      className="project-translate-page__launch-particle"
                      style={{
                        left: `${p.x}%`,
                        top: `${p.y}%`,
                        background: p.color,
                        '--launch-particle-x': `${p.dx}px`,
                        '--launch-particle-y': `${p.dy}px`,
                      } as React.CSSProperties}
                    />
                  ))}
                  <Button
                    className={primaryActionClassName}
                    disabled={primaryActionDisabled}
                    onClick={handlePrimaryAction}
                  >
                    {primaryActionLabel}
                  </Button>
                </div>
              </div>

              <div className={`runtime-summary-strip runtime-summary-strip--sidebar${shouldPollRuntime ? ' runtime-summary-strip--live' : ''}${stripBooting ? ' project-translate-page__strip-booting' : ''}${barSurging ? ' project-translate-page__bar-surge' : ''}`}>
                <div className="runtime-summary-strip__topline">
                  <div className="runtime-summary-strip__status">
                    <StatusBadge label={statusLabel} tone={statusTone} celebrate={justCompleted} />
                    <span className="runtime-summary-strip__updated">{updatedAtText}</span>
                  </div>
                </div>

                <div className="runtime-summary-strip__progress">
                  <div className="runtime-summary-strip__bar">
                    <div className={`runtime-summary-strip__bar-fill${justCompleted ? ' runtime-summary-strip__bar-fill--complete' : ''}`} style={{ width: `${progressPercent}%` }} />
                  </div>
                  <span className="runtime-summary-strip__percent">{progressPercent}%</span>
                </div>

                <dl className="runtime-summary-strip__metrics">
                  <div>
                    <dt>进度</dt>
                    <dd>{translatedCount}/{totalCount}</dd>
                  </div>
                  <div>
                    <dt>线程</dt>
                    <dd>{workersActive}/{workersConfigured}</dd>
                  </div>
                  <div>
                    <dt>速度</dt>
                    <dd>{speedText}</dd>
                  </div>
                  <div>
                    <dt>ETA</dt>
                    <dd>{etaText}</dd>
                  </div>
                </dl>
              </div>
            </div>
          </Panel>

          <Panel title="文件进度">
            {prioritizedRuntimeFiles.length > 0 ? (
                <div
                  className={`file-progress-list file-progress-list--runtime${hasFileProgressScrollbar ? ' file-progress-list--runtime-has-scrollbar' : ''}`}
                  ref={fileProgressListRef}
                >
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
              <EmptyState title="暂无文件进度" description="启动翻译后，文件级进度会在这里逐步展开。" />
            )}
          </Panel>
        </div>

        <div className="project-translate-page__main">
          {runtimeError ? (
            <div className="inline-alert inline-alert--error" role="alert">
              {runtimeError}
            </div>
          ) : null}

          <div className="runtime-dashboard-grid">
            <div className={`project-translate-page__success-panel${successEntries.length ? ' project-translate-page__success-panel--active' : ''}`}>
              <Panel title="成功句流">
                {hasSelectedSuccessFileFilter ? (
                  <div className="runtime-success-filter-hint" role="status">
                    <span className="runtime-success-filter-hint__text" title={selectedSuccessFiles.join('\n')}>
                      已筛选文件：{selectedSuccessFileFilterSummary}
                    </span>
                    <button
                      className="runtime-success-filter-hint__clear"
                      onClick={handleClearSuccessFileFilters}
                      type="button"
                    >
                      取消所有筛选
                    </button>
                  </div>
                ) : null}
                {successEntries.length ? (
                  <div className="runtime-event-list runtime-event-list--success" onScroll={handleSuccessListScroll} ref={successListRef}>
                    {successEntries.map((entry) => (
                      <RuntimeSuccessRow
                        entry={entry}
                        isFresh={freshSuccessIds.includes(entry.id)}
                        isSuccessFileFilterActive={selectedSuccessFileSet.has(entry.filename || '')}
                        onToggleSuccessFileFilter={handleToggleSuccessFileFilter}
                        key={entry.id}
                      />
                    ))}
                  </div>
                ) : (
                  <EmptyState title="还没有成功句流" description="任务开始输出后，最近成功的句子会滚动显示在这里。" />
                )}
              </Panel>
            </div>

            <Panel title="最近错误">
              {runtime?.recent_errors.length ? (
                <div className="runtime-event-list runtime-event-list--error">
                  {runtime.recent_errors.map((entry) => (
                    <RuntimeErrorRow entry={entry} key={entry.id} />
                  ))}
                </div>
              ) : (
                <EmptyState title="最近没有错误" description="接口错误、解析错误或重试退避会显示在这里。" />
              )}
            </Panel>
          </div>

        </div>
      </div>
    </div>
  );
}

function RuntimeErrorRow({ entry }: { entry: ProjectRuntimeErrorEntry }) {
  return (
    <article className="runtime-event runtime-event--error">
      <div className="runtime-event__header">
        <div className="runtime-event__badges">
          <span className="runtime-event__pill runtime-event__pill--danger">{entry.kind || 'error'}</span>
          <span className="runtime-event__pill">{entry.level || 'warn'}</span>
          {(entry.retry_count ?? 0) > 0 ? <span className="runtime-event__pill">重试 {entry.retry_count}</span> : null}
          {(entry.sleep_seconds ?? 0) > 0 ? <span className="runtime-event__pill">退避 {Number(entry.sleep_seconds).toFixed(3)}s</span> : null}
        </div>
        <time className="runtime-event__timestamp">{formatTime(entry.ts)}</time>
      </div>
      <p className="runtime-event__message">{entry.message || '未提供错误详情。'}</p>
      <dl className="runtime-event__meta">
        {entry.kind !== 'api' && (
          <div>
            <dt>文件</dt>
            <dd>{entry.filename || '—'}</dd>
          </div>
        )}
        {entry.kind !== 'api' && (
          <div>
            <dt>范围</dt>
            <dd>{entry.index_range || '—'}</dd>
          </div>
        )}
        <div>
          <dt>模型</dt>
          <dd>{entry.model || '—'}</dd>
        </div>
      </dl>
    </article>
  );
}

function RuntimeSuccessRow({
  entry,
  isFresh,
  isSuccessFileFilterActive,
  onToggleSuccessFileFilter,
}: {
  entry: ProjectRuntimeSuccessEntry;
  isFresh: boolean;
  isSuccessFileFilterActive: boolean;
  onToggleSuccessFileFilter: (filename: string) => void;
}) {
  const speakerLabel = Array.isArray(entry.speaker) ? entry.speaker.join(' / ') : entry.speaker;
  const speakerStyleVal = speakerLabel ? speakerStyle(speakerLabel) : undefined;
  const entryFilename = entry.filename || '未命名文件';
  const filterFilename = entry.filename;

  return (
    <article className={`runtime-event runtime-event--success${isFresh ? ' runtime-event--fresh' : ''}`}>
      <div className="runtime-event__header">
        <div className="runtime-event__badges">
          <span className="runtime-event__pill runtime-event__pill--success">#{entry.index}</span>
          <span
            className={`runtime-event__pill runtime-event__pill--file${filterFilename ? ' runtime-event__pill--file-clickable' : ''}${isSuccessFileFilterActive ? ' runtime-event__pill--file-active' : ''}`}
            title={entryFilename}
          >
            {filterFilename ? (
              <button
                aria-label="筛选句流"
                aria-pressed={isSuccessFileFilterActive}
                className="runtime-event__file-name-btn"
                onClick={() => onToggleSuccessFileFilter(filterFilename)}
                title="筛选句流"
                type="button"
              >
                {entryFilename}
              </button>
            ) : (
              <span className="runtime-event__file-text">{entryFilename}</span>
            )}
          </span>
        </div>
        <div className="runtime-event__header-right">
          {entry.trans_by ? <span className="runtime-event__pill runtime-event__pill--translator">{entry.trans_by}</span> : null}
          <time className="runtime-event__timestamp">{formatTime(entry.ts)}</time>
        </div>
      </div>
      <div className="runtime-success-compact">
        <p className="runtime-success-compact__line">
          <span className="runtime-success-compact__label">SRC</span>
          {speakerLabel ? <span className="runtime-success-compact__speaker-inline" style={speakerStyleVal}>{speakerLabel}</span> : null}
          <span>{entry.source_preview || '—'}</span>
        </p>
        <p className="runtime-success-compact__line">
          <span className="runtime-success-compact__label">DST</span>
          {speakerLabel ? <span className="runtime-success-compact__speaker-inline" style={speakerStyleVal}>{speakerLabel}</span> : null}
          <span>{entry.translation_preview || '—'}</span>
        </p>
      </div>
    </article>
  );
}

function FileProgressRow({
  file,
  isSuccessFileFilterActive,
  onToggleSuccessFileFilter,
}: {
  file: FileProgress;
  isSuccessFileFilterActive: boolean;
  onToggleSuccessFileFilter: (filename: string) => void;
}) {
  const percent = file.total > 0 ? Math.round((file.translated / file.total) * 100) : 0;
  const isComplete = file.translated === file.total && file.total > 0;
  const hasFailed = file.failed > 0;

  return (
    <div className="file-progress-row file-progress-row--runtime">
      <div className="file-progress-row__info">
        <div className="file-progress-row__identity">
          <span className="file-progress-row__name-wrap">
            <span className="file-progress-row__name">{file.filename}</span>
            <button
              aria-label="筛选句流"
              aria-pressed={isSuccessFileFilterActive}
              className={`file-progress-row__filter-toggle${isSuccessFileFilterActive ? ' file-progress-row__filter-toggle--active' : ''}`}
              onClick={() => onToggleSuccessFileFilter(file.filename)}
              title="筛选句流"
              type="button"
            >
              <FilterFunnelIcon className="file-progress-row__filter-icon" />
              <span className="file-progress-row__filter-tooltip">筛选句流</span>
              {isSuccessFileFilterActive ? <span className="file-progress-row__filter-check">✓</span> : null}
            </button>
          </span>
          <span className="file-progress-row__state">{isComplete ? '已完成' : percent > 0 ? '处理中' : '排队中'}</span>
        </div>
        <span className="file-progress-row__count">
          {file.translated}/{file.total}
          {hasFailed ? <span className="file-progress-row__failed"> · {file.failed}失败</span> : null}
        </span>
      </div>
      <div className="progress-bar progress-bar--small">
        <div className="progress-bar__fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function FilterFunnelIcon({ className }: { className: string }) {
  return (
    <svg aria-hidden="true" className={className} viewBox="0 0 24 24">
      <path d="M3 5h18l-7 8v5.5l-4 1.9V13L3 5z" fill="currentColor" />
    </svg>
  );
}

function toRuntimeJob(job: Job): RuntimeJob {
  return {
    job_id: job.job_id,
    status: job.status,
    translator: job.translator,
    created_at: job.created_at,
    started_at: job.started_at,
    finished_at: job.finished_at,
  };
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

function getStatusLabel(status?: RuntimeJob['status']) {
  switch (status) {
    case 'running':
      return '翻译中';
    case 'pending':
      return '等待中';
    case 'completed':
      return '已完成';
    case 'failed':
      return '失败';
    case 'cancelled':
      return '已取消';
    default:
      return '空闲';
  }
}

function formatDate(isoString: string): string {
  if (!isoString) return '—';
  try {
    const date = new Date(isoString);
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return isoString;
  }
}

function formatTime(isoString: string): string {
  if (!isoString) return '—';
  try {
    return new Date(isoString).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return isoString;
  }
}

function formatSpeed(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 行/分';
  return `${value.toFixed(value >= 10 ? 0 : 1)} 行/分`;
}

function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return `${hours} 小时 ${minutes} 分钟`;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, Math.round(value)));
}
