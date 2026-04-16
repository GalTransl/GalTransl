import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { Button } from '../components/Button';
import { StatusBadge } from '../components/StatusBadge';
import { InlineFeedback } from '../components/page-state';
import { encodeProjectDir, fetchJobs, fetchProjectRuntime, type Job } from '../lib/api';
import { formatTimestamp } from '../lib/format';
import { normalizeError } from '../lib/errors';

const HISTORY_KEY = 'galtransl-project-history';
const MAX_HISTORY = 20;

export type ProjectHistoryEntry = {
  projectDir: string;
  configFileName: string;
  lastOpened: string;
};

function loadHistory(): ProjectHistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveHistory(entries: ProjectHistoryEntry[]) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
}

export function addProjectToHistory(projectDir: string, configFileName: string) {
  const entries = loadHistory();
  const withoutDuplicate = entries.filter((e) => e.projectDir !== projectDir);
  withoutDuplicate.unshift({
    projectDir,
    configFileName,
    lastOpened: new Date().toISOString(),
  });
  saveHistory(withoutDuplicate.slice(0, MAX_HISTORY));
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
  const [history, setHistory] = useState<ProjectHistoryEntry[]>([]);
  const [projectDir, setProjectDir] = useState('');
  const [configFileName, setConfigFileName] = useState('config.yaml');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [refreshingJobs, setRefreshingJobs] = useState(false);
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
    setHistory(loadHistory());
    // Stagger entrance animation
    const t = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(t);
  }, []);

  const refreshJobs = useCallback(async () => {
    setRefreshingJobs(true);
    try {
      const nextJobs = await fetchJobs();
      setJobs(nextJobs);
      setJobsError(null);

      const activeJobs = nextJobs.filter((job) => job.status === 'pending' || job.status === 'running');
      if (activeJobs.length === 0) {
        setJobProgressById({});
      } else {
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
      setRefreshingJobs(false);
    }
  }, []);

  useEffect(() => {
    void refreshJobs();
    const poller = window.setInterval(() => {
      void refreshJobs();
    }, 3000);
    return () => window.clearInterval(poller);
  }, [refreshJobs]);

  const handleSelectConfigFile = useCallback(async () => {
    const selected = await open({
      multiple: false,
      filters: [
        { name: '配置文件', extensions: ['yaml', 'yml', 'inc.yaml', 'inc.yml'] },
        { name: '所有文件', extensions: ['*'] },
      ],
    });
    if (selected) {
      const filePath = selected as string;
      const normalized = filePath.replace(/\\/g, '/');
      const lastSlash = normalized.lastIndexOf('/');
      const dir = lastSlash >= 0 ? normalized.substring(0, lastSlash) : '';
      const fileName = lastSlash >= 0 ? normalized.substring(lastSlash + 1) : normalized;
      setProjectDir(dir.replace(/\//g, '\\'));
      setConfigFileName(fileName);
    }
  }, []);

  const handleOpenProject = useCallback(() => {
    const dir = projectDir.trim();
    if (!dir) return;
    const config = configFileName.trim() || 'config.yaml';
    addProjectToHistory(dir, config);
    onOpenProject(dir, config);
    const projectId = encodeProjectDir(dir);
    navigate(`/project/${projectId}/translate`);
  }, [projectDir, configFileName, onOpenProject, navigate]);

  const handleHistoryClick = useCallback(
    (entry: ProjectHistoryEntry) => {
      onOpenProject(entry.projectDir, entry.configFileName);
      const projectId = encodeProjectDir(entry.projectDir);
      navigate(`/project/${projectId}/translate`);
    },
    [onOpenProject, navigate],
  );

  const handleRemoveHistory = useCallback((projectDirToRemove: string, event: React.MouseEvent) => {
    event.stopPropagation();
    removeProjectFromHistory(projectDirToRemove);
    setHistory(loadHistory());
  }, []);

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
          <div className="home-hero__logo">
            <svg viewBox="0 0 40 40" width="40" height="40" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect width="40" height="40" rx="10" fill="url(#logo-grad)" />
              <path
                d="M12 14h4v12h-4zM18 14h4l4 8V14h4v12h-4l-4-8v8h-4z"
                fill="white"
                fillOpacity="0.95"
              />
              <defs>
                <linearGradient id="logo-grad" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
                  <stop stopColor="#2f6feb" />
                  <stop offset="1" stopColor="#1d5fe0" />
                </linearGradient>
              </defs>
            </svg>
          </div>
          <div className="home-hero__text">
            <h1 className="home-hero__title">GalTransl</h1>
            <p className="home-hero__subtitle">Visual Novel Translation Studio</p>
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
          </div>
          <form
            className="home-open__form"
            onSubmit={(e) => {
              e.preventDefault();
              handleOpenProject();
            }}
          >
            <div className="home-open__fields">
              <label className="home-open__field">
                <span className="home-open__field-label">项目目录</span>
                <input
                  autoComplete="off"
                  className="home-open__input"
                  onChange={(e) => setProjectDir(e.target.value)}
                  placeholder="E:\GalTransl\sampleProject"
                  value={projectDir}
                />
              </label>
              <label className="home-open__field home-open__field--config">
                <span className="home-open__field-label">配置文件</span>
                <input
                  autoComplete="off"
                  className="home-open__input"
                  onChange={(e) => setConfigFileName(e.target.value)}
                  value={configFileName}
                />
              </label>
            </div>
            <div className="home-open__actions">
              <Button type="button" variant="secondary" onClick={handleSelectConfigFile}>
                浏览
              </Button>
              <Button type="submit" disabled={!projectDir.trim()}>
                打开项目
              </Button>
              <Button type="button" variant="secondary" onClick={() => navigate('/new-project')}>
                新建项目
              </Button>
            </div>
          </form>
        </section>

        {/* Center: History */}
        <section className="home-history">
          <div className="home-history__header">
            <h2>历史项目</h2>
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
                      <div className="home-history__item-path">{entry.projectDir}</div>
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
            <h2>翻译任务</h2>
            <Button disabled={refreshingJobs} onClick={() => void refreshJobs()} variant="secondary">
              {refreshingJobs ? '…' : '刷新'}
            </Button>
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
                return (
                  <div key={job.job_id} className="home-job-row">
                    <div className="home-job-row__top">
                      <div className="home-job-row__path" title={job.project_dir}>{job.project_dir}</div>
                      <StatusBadge label={job.status} tone={job.status} />
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
