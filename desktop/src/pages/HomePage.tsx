import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { Button } from '../components/Button';
import { StatusBadge } from '../components/StatusBadge';
import { InlineFeedback } from '../components/page-state';
import {
  encodeProjectDir,
  fetchJobs,
  fetchProjectRuntime,
  getHomeHistoryRetentionLimit,
  getHomeJobRetentionLimit,
  HOME_HISTORY_LIMIT_CHANGE_EVENT,
  HOME_JOB_LIMIT_CHANGE_EVENT,
  stopProjectTranslation,
  type Job,
} from '../lib/api';
import { formatTimestamp } from '../lib/format';
import { normalizeError } from '../lib/errors';
import desktopPackage from '../../package.json';

const HISTORY_KEY = 'galtransl-project-history';
const JOB_MEMORY_KEY = 'galtransl-home-jobs-memory';
const PROJECT_HOMEPAGE = 'https://github.com/XD2333/GalTransl';
const APP_VERSION = desktopPackage.version ? `v${desktopPackage.version}` : 'dev';
const MIN_REFRESH_SPIN_MS = 420;
const REFRESH_SPIN_CYCLE_MS = 500;

export type ProjectHistoryEntry = {
  projectDir: string;
  configFileName: string;
  lastOpened: string;
};

function loadHistory(limit = getHomeHistoryRetentionLimit()): ProjectHistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? (JSON.parse(raw) as ProjectHistoryEntry[]) : [];
    return parsed.slice(0, limit);
  } catch {
    return [];
  }
}

function saveHistory(entries: ProjectHistoryEntry[]) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
}

function getJobSortTimestamp(job: Job): number {
  const timestamp = Date.parse(job.finished_at || job.started_at || job.created_at || '');
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function normalizeRememberedJob(value: unknown): Job | null {
  if (!value || typeof value !== 'object') {
    return null;
  }

  const raw = value as Partial<Record<keyof Job, unknown>>;
  const status = raw.status;
  if (status !== 'pending' && status !== 'running' && status !== 'completed' && status !== 'failed' && status !== 'cancelled') {
    return null;
  }

  if (typeof raw.job_id !== 'string' || typeof raw.project_dir !== 'string') {
    return null;
  }

  return {
    config_file_name: typeof raw.config_file_name === 'string' ? raw.config_file_name : '',
    created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    error: typeof raw.error === 'string' ? raw.error : '',
    finished_at: typeof raw.finished_at === 'string' ? raw.finished_at : '',
    job_id: raw.job_id,
    project_dir: raw.project_dir,
    started_at: typeof raw.started_at === 'string' ? raw.started_at : '',
    status,
    success: typeof raw.success === 'boolean' ? raw.success : false,
    translator: typeof raw.translator === 'string' ? raw.translator : '',
  };
}

function sortAndLimitJobs(jobs: Job[], limit: number): Job[] {
  return jobs
    .sort((a, b) => getJobSortTimestamp(b) - getJobSortTimestamp(a))
    .slice(0, limit);
}

function loadRememberedJobs(limit = getHomeJobRetentionLimit()): Job[] {
  try {
    const raw = localStorage.getItem(JOB_MEMORY_KEY);
    if (!raw) {
      return [];
    }

    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }

    return sortAndLimitJobs(
      parsed.map(normalizeRememberedJob).filter((job): job is Job => job !== null),
      limit,
    );
  } catch {
    return [];
  }
}

function saveRememberedJobs(jobs: Job[], limit: number) {
  try {
    localStorage.setItem(JOB_MEMORY_KEY, JSON.stringify(sortAndLimitJobs([...jobs], limit)));
  } catch {
    // ignore storage errors
  }
}

function mergeJobsWithMemory(existing: Job[], incoming: Job[], limit: number): Job[] {
  const merged = new Map<string, Job>();

  incoming.forEach((job) => {
    merged.set(job.job_id, job);
  });

  existing.forEach((job) => {
    if (!merged.has(job.job_id)) {
      merged.set(job.job_id, job);
    }
  });

  return sortAndLimitJobs(Array.from(merged.values()), limit);
}

export function addProjectToHistory(projectDir: string, configFileName: string) {
  const entries = loadHistory();
  const withoutDuplicate = entries.filter((e) => e.projectDir !== projectDir);
  withoutDuplicate.unshift({
    projectDir,
    configFileName,
    lastOpened: new Date().toISOString(),
  });
  saveHistory(withoutDuplicate.slice(0, getHomeHistoryRetentionLimit()));
}

export function removeProjectFromHistory(projectDir: string) {
  const entries = loadHistory().filter((e) => e.projectDir !== projectDir);
  saveHistory(entries);
}

type HomePageProps = {
  onOpenProject: (projectDir: string, configFileName: string) => void;
};

export function HomePage({ onOpenProject }: HomePageProps) {
  const navigate = useNavigate();
  const [historyLimit, setHistoryLimit] = useState(() => getHomeHistoryRetentionLimit());
  const [jobMemoryLimit, setJobMemoryLimit] = useState(() => getHomeJobRetentionLimit());
  const [history, setHistory] = useState<ProjectHistoryEntry[]>([]);
  const [jobs, setJobs] = useState<Job[]>(() => loadRememberedJobs(getHomeJobRetentionLimit()));
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [refreshingJobs, setRefreshingJobs] = useState(false);
  const [stoppingJobId, setStoppingJobId] = useState<string | null>(null);
  const [shouldLoadJobProgress, setShouldLoadJobProgress] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [jobProgressById, setJobProgressById] = useState<
    Record<
      string,
      {
        currentFile?: string;
        percent: number;
        total: number;
        translated: number;
      }
    >
  >({});

  useEffect(() => {
    setHistory(loadHistory(historyLimit));
    // Stagger entrance animation
    const t = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(t);
  }, [historyLimit]);

  useEffect(() => {
    const handleHistoryLimitChanged = () => {
      const nextLimit = getHomeHistoryRetentionLimit();
      setHistoryLimit(nextLimit);
      setHistory(loadHistory(nextLimit));
    };

    const handleJobLimitChanged = () => {
      const nextLimit = getHomeJobRetentionLimit();
      setJobMemoryLimit(nextLimit);
      setJobs((currentJobs) => sortAndLimitJobs([...currentJobs], nextLimit));
    };

    window.addEventListener(HOME_HISTORY_LIMIT_CHANGE_EVENT, handleHistoryLimitChanged as EventListener);
    window.addEventListener(HOME_JOB_LIMIT_CHANGE_EVENT, handleJobLimitChanged as EventListener);
    return () => {
      window.removeEventListener(HOME_HISTORY_LIMIT_CHANGE_EVENT, handleHistoryLimitChanged as EventListener);
      window.removeEventListener(HOME_JOB_LIMIT_CHANGE_EVENT, handleJobLimitChanged as EventListener);
    };
  }, []);

  useEffect(() => {
    let delayTimer = 0;
    const frameId = window.requestAnimationFrame(() => {
      delayTimer = window.setTimeout(() => {
        setShouldLoadJobProgress(true);
      }, 300);
    });

    return () => {
      window.cancelAnimationFrame(frameId);
      if (delayTimer) {
        window.clearTimeout(delayTimer);
      }
    };
  }, []);

  useEffect(() => {
    saveRememberedJobs(jobs, jobMemoryLimit);
  }, [jobMemoryLimit, jobs]);

  const refreshJobs = useCallback(async (silent = false) => {
    const startedAt = Date.now();
    if (!silent) setRefreshingJobs(true);
    try {
      const nextJobs = await fetchJobs();
      setJobs((currentJobs) => mergeJobsWithMemory(currentJobs, nextJobs, jobMemoryLimit));
      setJobsError(null);

      if (!shouldLoadJobProgress) {
        setJobProgressById({});
      } else {
        const activeJobs = nextJobs.filter((job) => job.status === 'pending' || job.status === 'running');
        if (activeJobs.length === 0) {
          setJobProgressById({});
          return;
        }

        const progressEntries = await Promise.all(
          activeJobs.map(async (job) => {
            try {
              const runtime = await fetchProjectRuntime(encodeProjectDir(job.project_dir));
              return [
                job.job_id,
                {
                  currentFile: runtime.current_file,
                  percent: runtime.summary.percent,
                  total: runtime.summary.total,
                  translated: runtime.summary.translated,
                },
              ] as const;
            } catch {
              return null;
            }
          }),
        );

        setJobProgressById(
          progressEntries.reduce<
            Record<string, { currentFile?: string; percent: number; total: number; translated: number }>
          >((acc, entry) => {
            if (entry) {
              acc[entry[0]] = entry[1];
            }
            return acc;
          }, {}),
        );
      }
    } catch (error) {
      setJobsError(normalizeError(error, '读取全局任务列表失败'));
    } finally {
      if (!silent) {
        const elapsedMs = Date.now() - startedAt;
        const minReachedMs = Math.max(elapsedMs, MIN_REFRESH_SPIN_MS);
        const remainToFullCycleMs = (REFRESH_SPIN_CYCLE_MS - (minReachedMs % REFRESH_SPIN_CYCLE_MS)) % REFRESH_SPIN_CYCLE_MS;
        const remainMs = Math.max(0, MIN_REFRESH_SPIN_MS - elapsedMs) + remainToFullCycleMs;
        if (remainMs > 0) {
          await new Promise<void>((resolve) => window.setTimeout(resolve, remainMs));
        }
        setRefreshingJobs(false);
      }
    }
  }, [jobMemoryLimit, shouldLoadJobProgress]);

  useEffect(() => {
    void refreshJobs();
    const poller = window.setInterval(() => {
      void refreshJobs(true);
    }, 3000);
    return () => window.clearInterval(poller);
  }, [refreshJobs]);

  useEffect(() => {
    if (!shouldLoadJobProgress) {
      return;
    }
    void refreshJobs(true);
  }, [refreshJobs, shouldLoadJobProgress]);

  const handleOpenProject = useCallback(async () => {
    const selected = await open({
      multiple: false,
      filters: [
        { name: '配置文件', extensions: ['yaml', 'yml', 'inc.yaml', 'inc.yml'] },
        { name: '所有文件', extensions: ['*'] },
      ],
    });
    if (!selected) return;
    const filePath = selected as string;
    const normalized = filePath.replace(/\\/g, '/');
    const lastSlash = normalized.lastIndexOf('/');
    const dir = (lastSlash >= 0 ? normalized.substring(0, lastSlash) : '').replace(/\//g, '\\');
    const config = (lastSlash >= 0 ? normalized.substring(lastSlash + 1) : normalized).trim() || 'config.yaml';

    if (!dir.trim()) return;
    addProjectToHistory(dir, config);
    onOpenProject(dir, config);
    const projectId = encodeProjectDir(dir);
    navigate(`/project/${projectId}/translate`);
  }, [onOpenProject, navigate]);

  const handleHistoryClick = useCallback(
    (entry: ProjectHistoryEntry) => {
      onOpenProject(entry.projectDir, entry.configFileName);
      const projectId = encodeProjectDir(entry.projectDir);
      navigate(`/project/${projectId}/translate`);
    },
    [onOpenProject, navigate],
  );

  const handleJobClick = useCallback((job: Job) => {
    onOpenProject(job.project_dir, job.config_file_name);
    const projectId = encodeProjectDir(job.project_dir);
    navigate(`/project/${projectId}/translate`);
  }, [onOpenProject, navigate]);

  const handleStopJob = useCallback(async (job: Job, event: React.MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (stoppingJobId) return;
    if (job.status !== 'pending' && job.status !== 'running') return;

    setStoppingJobId(job.job_id);
    setJobsError(null);

    try {
      const projectId = encodeProjectDir(job.project_dir);
      const stoppedJob = await stopProjectTranslation(projectId);
      setJobs((current) =>
        current.map((currentJob) =>
          currentJob.job_id === stoppedJob.job_id
            ? {
                ...currentJob,
                status: stoppedJob.status,
                success: stoppedJob.success,
              }
            : currentJob,
        ),
      );
      await refreshJobs(true);
    } catch (error) {
      setJobsError(normalizeError(error, '停止任务失败'));
      void refreshJobs(true);
    } finally {
      setStoppingJobId(null);
    }
  }, [refreshJobs, stoppingJobId]);

  const handleRemoveHistory = useCallback((projectDirToRemove: string, event: React.MouseEvent) => {
    event.stopPropagation();
    removeProjectFromHistory(projectDirToRemove);
    setHistory(loadHistory(historyLimit));
  }, [historyLimit]);

  const activeJobsCount = useMemo(
    () => jobs.filter((job) => job.status === 'pending' || job.status === 'running').length,
    [jobs],
  );
  const completedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'completed').length, [jobs]);
  const failedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'failed').length, [jobs]);

  return (
    <div className={`home-page${mounted ? ' home-page--mounted' : ''}`}>
      {/* ── Hero Brand Area ── */}
      <div className="home-hero">
        <div className="home-hero__brand">
          <div className="home-hero__text">
            <span className="home-hero__eyebrow">Desktop Translation Console</span>
            <h1 className="home-hero__title">GalTransl</h1>
            <p className="home-hero__subtitle">Translate your favorite Galgame</p>
            <p className="home-hero__description">从项目打开到任务追踪，一屏完成翻译工作流。</p>
            <div className="home-hero__chips" aria-label="首页信息">
              <span className="home-hero__chip">版本 {APP_VERSION}</span>
              <a className="home-hero__chip home-hero__chip--link" href={PROJECT_HOMEPAGE} target="_blank" rel="noreferrer noopener">
                项目主页
              </a>
            </div>
          </div>
        </div>

        <div className="home-hero__stats">
          <div className="home-hero__stat">
            <span className="home-hero__stat-value">{history.length}</span>
            <span className="home-hero__stat-label">历史项目</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className="home-hero__stat-value home-hero__stat-value--active">{activeJobsCount}</span>
            <span className="home-hero__stat-label">活跃任务</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className="home-hero__stat-value">{completedJobsCount}</span>
            <span className="home-hero__stat-label">已完成</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className={`home-hero__stat-value${failedJobsCount > 0 ? ' home-hero__stat-value--danger' : ''}`}>
              {failedJobsCount}
            </span>
            <span className="home-hero__stat-label">失败</span>
          </div>
        </div>

        {/* Decorative mesh gradient */}
        <div className="home-hero__glow" aria-hidden="true" />
      </div>

      {/* ── Main Content Grid ── */}
      <div className="home-grid">
        {/* Left: Open Project */}
        <section className="home-open">
          <div className="home-open__header">
            <h2>打开项目</h2>
            <p>打开或新建翻译项目</p>
          </div>
          <div className="home-open__form">
            <div className="home-open__actions">
              <Button type="button" className="home-open__action-btn" onClick={() => void handleOpenProject()}>
                打开项目
              </Button>
              <Button type="button" className="home-open__action-btn" variant="secondary" onClick={() => navigate('/new-project')}>
                新建项目
              </Button>
            </div>
          </div>
        </section>

        {/* Center: History */}
        <section className="home-history">
          <div className="home-history__header">
            <div>
              <h2>历史项目</h2>
              <p>最近打开的项目</p>
            </div>
            <span className="home-history__count">{history.length}</span>
          </div>
          {history.length === 0 ? (
            <div className="home-history__empty">
              <span>暂无历史</span>
              <span>打开项目后自动出现在这里</span>
            </div>
          ) : (
            <div className="home-history__list">
              {history.map((entry) => (
                <div key={entry.projectDir} className="home-history__item">
                  <button
                    type="button"
                    className="home-history__item-button"
                    onClick={() => handleHistoryClick(entry)}
                  >
                    <div className="home-history__item-icon">
                      <svg viewBox="0 0 16 16" width="16" height="16" fill="none">
                        <path
                          d="M2 3.5A1.5 1.5 0 013.5 2h3.586a1 1 0 01.707.293l1.914 1.914a1 1 0 00.707.293H12.5A1.5 1.5 0 0114 6v5.5a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 11.5v-8z"
                          stroke="currentColor"
                          strokeWidth="1.2"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </div>
                    <div className="home-history__item-info">
                      <div className="home-history__item-path">{projectName(entry.projectDir)}</div>
                      <div className="home-history__item-meta">
                        {entry.configFileName} · {formatDate(entry.lastOpened)}
                      </div>
                    </div>
                  </button>
                  <button
                    type="button"
                    className="home-history__item-remove"
                    onClick={(e) => handleRemoveHistory(entry.projectDir, e)}
                    title="从历史中移除"
                  >
                    <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
                      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Right: Jobs */}
        <section className="home-jobs">
          <div className="home-jobs__header">
            <div>
              <h2>翻译任务</h2>
              <p>进度与状态汇总</p>
            </div>
            <button
              type="button"
              className={`icon-btn icon-btn--refresh${refreshingJobs ? ' icon-btn--spinning' : ''}`}
              disabled={refreshingJobs}
              onClick={() => void refreshJobs()}
              title="刷新任务列表"
              aria-label="刷新任务列表"
            >
              <svg viewBox="0 0 16 16" width="15" height="15" fill="none">
                <path d="M13.5 8a5.5 5.5 0 11-1.4-3.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                <path d="M12 2v3.5H8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
          {jobsError ? <InlineFeedback tone="error" title="加载失败" description={jobsError} /> : null}
          {jobs.length === 0 ? (
            <div className="home-jobs__empty">
              <span>还没有翻译任务</span>
              <span>启动翻译后，任务会汇总在这里</span>
            </div>
          ) : (
            <div className="home-jobs__list">
              {jobs.map((job) => {
                const prog = jobProgressById[job.job_id];
                const isRunningJob = job.status === 'running';
                const isStoppingThisJob = stoppingJobId === job.job_id;
                return (
                  <div
                    key={job.job_id}
                    className="home-job-row home-job-row--clickable"
                    role="button"
                    tabIndex={0}
                    onClick={() => handleJobClick(job)}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        handleJobClick(job);
                      }
                    }}
                  >
                    <div className="home-job-row__top">
                      <div className="home-job-row__path" title={job.project_dir}>{projectName(job.project_dir)}</div>
                      <div className="home-job-row__actions">
                        {isRunningJob ? (
                          <div className={`home-job-row__status-switch${isStoppingThisJob ? ' is-stopping' : ''}`}>
                            <span className="home-job-row__status-pill" aria-hidden={isStoppingThisJob}>
                              <StatusBadge label={job.status} tone={job.status} />
                            </span>
                            <button
                              type="button"
                              className={`home-job-row__stop-btn${isStoppingThisJob ? ' is-stopping' : ''}`}
                              onClick={(event) => void handleStopJob(job, event)}
                              disabled={Boolean(stoppingJobId) && !isStoppingThisJob}
                              aria-label={isStoppingThisJob ? '正在停止任务' : `停止任务 ${projectName(job.project_dir)}`}
                              title={isStoppingThisJob ? '正在停止任务' : '停止任务'}
                            >
                              {isStoppingThisJob ? '停止中…' : '停止'}
                            </button>
                          </div>
                        ) : (
                          <StatusBadge label={job.status} tone={job.status} />
                        )}
                      </div>
                    </div>
                    <div className="home-job-row__meta">
                      <span>{job.translator}</span>
                      <span className="home-job-row__sep">·</span>
                      <span>{formatTimestamp(job.created_at)}</span>
                      {prog ? (
                        <>
                          <span className="home-job-row__sep">·</span>
                          <span className="home-job-row__progress-text">
                            {prog.translated}/{prog.total} · {prog.percent}%
                          </span>
                        </>
                      ) : null}
                    </div>
                    {prog ? (
                      <div className="home-job-row__bar-track">
                        <div className="home-job-row__bar-fill" style={{ width: `${prog.percent}%` }} />
                      </div>
                    ) : null}
                    {job.error ? (
                      <div className="home-job-row__error" title={job.error}>
                        {job.error.length > 80 ? `${job.error.slice(0, 80)}…` : job.error}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function projectName(projectDir: string): string {
  return projectDir.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || projectDir;
}

function formatDate(isoString: string): string {
  try {
    const date = new Date(isoString);
    return date.toLocaleDateString('zh-CN', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return isoString;
  }
}
