import { usePageActive } from '../components/PageActivity';
import { getUiLanguage } from "../i18n/core";
import { t as translate, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { Button } from '../components/Button';
import { GettingStarted } from '../components/GettingStarted';
import { HomeTranslationFlow } from '../components/HomeTranslationFlow';
import {
  encodeProjectDir,
  fetchJobs,
  fetchVersion,
  fetchVersionCheck,
  getHomeHistoryRetentionLimit,
  getHomeJobRetentionLimit,
  HOME_HISTORY_LIMIT_CHANGE_EVENT,
  HOME_JOB_LIMIT_CHANGE_EVENT,
  type Job,
} from '../lib/api';
import { basenamePath, dirnamePath } from '../lib/paths';
import { PROJECT_HOMEPAGE, RELEASE_LATEST_URL, RELEASE_LIST_URL } from '../lib/externalLink';
import { PROJECT_HISTORY_KEY, PROJECT_HISTORY_CHANGE_EVENT, readProjectHistory, writeProjectStorage, type ProjectHistoryEntry } from '../lib/projectStorage';
export type { ProjectHistoryEntry } from '../lib/projectStorage';
const JOB_MEMORY_KEY = 'galtransl-home-jobs-memory';
const JOB_CLEARED_KEY = 'galtransl-home-jobs-cleared';

function loadHistory(limit = getHomeHistoryRetentionLimit()): ProjectHistoryEntry[] {
  return readProjectHistory().slice(0, limit);
}

function saveHistory(entries: ProjectHistoryEntry[]) {
  writeProjectStorage(PROJECT_HISTORY_KEY, JSON.stringify(entries));
  window.dispatchEvent(new CustomEvent(PROJECT_HISTORY_CHANGE_EVENT));
}

function getJobSortTimestamp(job: Job): number {
  const timestamp = Date.parse(job.finished_at || job.started_at || job.created_at || '');
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function isActiveJobStatus(status: Job['status']): boolean {
  return status === 'pending' || status === 'running';
}

function isActiveJob(job: Job): boolean {
  return isActiveJobStatus(job.status);
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
      parsed
        .map(normalizeRememberedJob)
        .filter((job): job is Job => job !== null)
        .filter((job) => !isActiveJob(job)),
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

function loadClearedJobIds(): Set<string> {
  try {
    const raw = localStorage.getItem(JOB_CLEARED_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((id): id is string => typeof id === 'string'));
  } catch {
    return new Set();
  }
}

function saveClearedJobIds(ids: Set<string>) {
  try {
    localStorage.setItem(JOB_CLEARED_KEY, JSON.stringify(Array.from(ids)));
  } catch {
    // ignore storage errors
  }
}

function mergeJobsWithMemory(existing: Job[], incoming: Job[], limit: number, cleared: Set<string>): Job[] {
  const merged = new Map<string, Job>();

  incoming.forEach((job) => {
    if (cleared.has(job.job_id)) return;
    merged.set(job.job_id, job);
  });

  existing.forEach((job) => {
    if (cleared.has(job.job_id)) return;
    if (!merged.has(job.job_id) && !isActiveJob(job)) {
      merged.set(job.job_id, job);
    }
  });

  return sortAndLimitJobs(Array.from(merged.values()), limit);
}

function jobsEqual(a: Job[], b: Job[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    const ja = a[i];
    const jb = b[i];
    if (
      ja.job_id !== jb.job_id ||
      ja.status !== jb.status ||
      ja.success !== jb.success ||
      ja.error !== jb.error ||
      ja.finished_at !== jb.finished_at ||
      ja.started_at !== jb.started_at ||
      ja.translator !== jb.translator ||
      ja.project_dir !== jb.project_dir ||
      ja.config_file_name !== jb.config_file_name
    ) {
      return false;
    }
  }
  return true;
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
  const active = usePageActive();
  const uiLanguage = useUiLanguage();
  const navigate = useNavigate();
  const [historyLimit, setHistoryLimit] = useState(() => getHomeHistoryRetentionLimit());
  const [jobMemoryLimit, setJobMemoryLimit] = useState(() => getHomeJobRetentionLimit());
  const [history, setHistory] = useState<ProjectHistoryEntry[]>([]);
  const [jobs, setJobs] = useState<Job[]>(() => loadRememberedJobs(getHomeJobRetentionLimit()));
  const [mounted, setMounted] = useState(false);
  const [coreVersion, setCoreVersion] = useState<string | null>(null);
  const [latestVersion, setLatestVersion] = useState<string | null>(null);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const clearedJobIds = useRef<Set<string>>(loadClearedJobIds());

  useEffect(() => {
    if (!active) return;
    setHistory(loadHistory(historyLimit));
    // Stagger entrance animation
    const t = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(t);
  }, [historyLimit, active]);

  useEffect(() => {
    let cancelled = false;

    // 当前版本：立即请求，快速显示
    fetchVersion()
      .then((version) => {
        if (!cancelled) setCoreVersion(version);
      })
      .catch(() => undefined);

    // 更新检查：异步叠加，不阻塞版本号显示
    fetchVersionCheck()
      .then((result) => {
        if (cancelled) return;
        setCoreVersion(result.version);
        setLatestVersion(result.latest_version);
        setUpdateAvailable(result.update_available);
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, []);

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
    window.addEventListener(PROJECT_HISTORY_CHANGE_EVENT, handleHistoryLimitChanged);
    window.addEventListener(HOME_JOB_LIMIT_CHANGE_EVENT, handleJobLimitChanged as EventListener);
    return () => {
      window.removeEventListener(HOME_HISTORY_LIMIT_CHANGE_EVENT, handleHistoryLimitChanged as EventListener);
      window.removeEventListener(PROJECT_HISTORY_CHANGE_EVENT, handleHistoryLimitChanged);
      window.removeEventListener(HOME_JOB_LIMIT_CHANGE_EVENT, handleJobLimitChanged as EventListener);
    };
  }, []);

  useEffect(() => {
    saveRememberedJobs(jobs, jobMemoryLimit);
  }, [jobMemoryLimit, jobs]);

  const refreshJobs = useCallback(async () => {
    try {
      const nextJobs = await fetchJobs();
      let clearedDirty = false;
      const backendJobIds = new Set(nextJobs.map((job) => job.job_id));
      nextJobs.forEach((job) => {
        if (job.status === 'running' || job.status === 'pending') {
          if (clearedJobIds.current.delete(job.job_id)) {
            clearedDirty = true;
          }
        }
      });
      // 回收：后端已不再返回的 id 无需继续保留在 cleared 集合里
      clearedJobIds.current.forEach((id) => {
        if (!backendJobIds.has(id)) {
          clearedJobIds.current.delete(id);
          clearedDirty = true;
        }
      });
      if (clearedDirty) saveClearedJobIds(clearedJobIds.current);
      setJobs((currentJobs) => {
        const next = mergeJobsWithMemory(currentJobs, nextJobs, jobMemoryLimit, clearedJobIds.current);
        return jobsEqual(currentJobs, next) ? currentJobs : next;
      });
    } catch {
      // 统计数字保持上一次的结果
    }
  }, [jobMemoryLimit]);

  useEffect(() => {
    if (!active) return;
    void refreshJobs();
    const poller = window.setInterval(() => {
      if (!document.hidden) {
        void refreshJobs();
      }
    }, 3000);
    return () => window.clearInterval(poller);
  }, [refreshJobs, active]);

  const handleOpenProject = useCallback(async () => {
    const selected = await open({
      multiple: false,
      filters: [
        { name: translate('common:files.configFiles'), extensions: ['yaml', 'yml', 'inc.yaml', 'inc.yml'] },
        { name: translate('common:files.allFiles'), extensions: ['*'] },
      ],
    });
    if (!selected) return;
    const filePath = selected as string;
    const dir = dirnamePath(filePath);
    const config = basenamePath(filePath).trim() || 'config.yaml';

    if (!dir.trim()) return;
    addProjectToHistory(dir, config);
    onOpenProject(dir, config);
    const projectId = encodeProjectDir(dir);
    navigate(`/project/${projectId}/translate`);
  }, [onOpenProject, navigate]);

  const handleHistoryClick = useCallback(
    (entry: ProjectHistoryEntry, section: 'translate' | 'dictionary' = 'translate') => {
      onOpenProject(entry.projectDir, entry.configFileName);
      const projectId = encodeProjectDir(entry.projectDir);
      navigate(`/project/${projectId}/${section}`);
    },
    [onOpenProject, navigate],
  );

  const handleRemoveHistory = useCallback((projectDirToRemove: string, event: React.MouseEvent) => {
    event.stopPropagation();
    removeProjectFromHistory(projectDirToRemove);
    setHistory(loadHistory(historyLimit));
  }, [historyLimit, active]);

  const activeJobsCount = useMemo(
    () => jobs.filter((job) => job.status === 'pending' || job.status === 'running').length,
    [jobs],
  );
  const completedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'completed').length, [jobs]);
  // 「快速上手」用它判断字典那一步做没做（任务列表里有过跑完的 GenDic 任务）
  const generatedDictOnce = useMemo(
    () => jobs.some((job) => job.translator === 'GenDic' && job.status === 'completed'),
    [jobs],
  );
  const failedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'failed').length, [jobs]);

  return (
    <div className={`home-page${mounted ? ' home-page--mounted' : ''}`}>
      {/* ── Hero Brand Area ── */}
      <div className="home-hero">
        <div className="home-hero__brand">
          <div className="home-hero__text">
            <span className="home-hero__eyebrow">{translate("projects:homePage.homeHeroText_message_desktopTranslationConsole")}</span>
            <h1 className="home-hero__title">{translate("projects:homePage.homeHeroText_message_galTransl")}</h1>
            <p className="home-hero__subtitle">{translate("projects:homePage.homeHeroText_message_translateYourFavoriteGalgame")}</p>
            <p className="home-hero__description">{translate("projects:homePage.homeHeroText_message_aIModelGalgameAutoTranslation")}</p>
            <div className="home-hero__chips" aria-label={translate("projects:homePage.homeHeroChips_ariaLabel_text")}>
              <span className="home-hero__chip">{translate("projects:homePage.homeHeroChip_span_version")}{coreVersion ? `v${coreVersion}` : '—'}</span>
              {updateAvailable && latestVersion ? (
                <a
                  className="home-hero__chip home-hero__chip--update"
                  href={RELEASE_LATEST_URL}
                  target="_blank"
                  rel="noreferrer noopener"
                >{translate("projects:homePage.homeHeroChips_message_versionV", { latestVersion: latestVersion })}</a>
              ) : null}
              <a className="home-hero__chip home-hero__chip--link" href={PROJECT_HOMEPAGE} target="_blank" rel="noreferrer noopener">
                {translate("projects:homePage.homeHeroChips_message_project")}
              </a>
              <a className="home-hero__chip home-hero__chip--link" href={RELEASE_LIST_URL} target="_blank" rel="noreferrer noopener">
                {translate("projects:homePage.changelog")}
              </a>
            </div>
          </div>
        </div>

        <div className="home-hero__stats">
          <div className="home-hero__stat">
            <span className="home-hero__stat-value">{history.length}</span>
            <span className="home-hero__stat-label">{translate("projects:homePage.homeHeroStat_message_project")}</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className="home-hero__stat-value home-hero__stat-value--active">{activeJobsCount}</span>
            <span className="home-hero__stat-label">{translate("projects:homePage.homeHeroStat_message_job")}</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className="home-hero__stat-value">{completedJobsCount}</span>
            <span className="home-hero__stat-label">{translate("projects:homePage.homeHeroStat_message_doneComplete")}</span>
          </div>
          <div className="home-hero__stat-divider" />
          <div className="home-hero__stat">
            <span className={`home-hero__stat-value${failedJobsCount > 0 ? ' home-hero__stat-value--danger' : ''}`}>
              {failedJobsCount}
            </span>
            <span className="home-hero__stat-label">{translate("common:actions.failed")}</span>
          </div>
        </div>

        {/* Decorative mesh gradient */}
        <div className="home-hero__glow" aria-hidden="true" />
      </div>

      <GettingStarted
        hasProject={history.length > 0}
        hasCompletedJob={completedJobsCount > 0}
        hasGeneratedDict={generatedDictOnce}
        onOpenLatestProject={history[0] ? () => handleHistoryClick(history[0]) : undefined}
        onOpenProjectDictionary={history[0] ? () => handleHistoryClick(history[0], 'dictionary') : undefined}
      />

      {/* ── Main Content Grid ── */}
      <div className="home-grid">
        {/* Left: Open Project */}
        <section className="home-open">
          <div className="home-open__header">
            <h2>{translate("projects:homePage.homeOpenHeader_message_openProject")}</h2>
            <p>{translate("projects:homePage.homeOpenHeader_message_openDoneProjectConfigYamlNewProject")}</p>
          </div>
          <div className="home-open__form">
            <div className="home-open__actions">
              <Button type="button" className="home-open__action-btn" onClick={() => void handleOpenProject()}>{translate("projects:homePage.homeOpenActions_message_openProject")}</Button>
              <Button type="button" className="home-open__action-btn" variant="secondary" onClick={() => navigate('/new-project')}>{translate("projects:homePage.homeOpenActions_message_newProject")}</Button>
            </div>
          </div>
        </section>

        {/* Center: History */}
        <section className="home-history">
          <div className="home-history__header">
            <div>
              <h2>{translate("projects:homePage.homeHistoryHeader_message_project")}</h2>
              <p>{translate("projects:homePage.homeHistoryHeader_message_openProject")}</p>
            </div>
            <span className="home-history__count">{history.length}</span>
          </div>
          {history.length === 0 ? (
            <div className="home-history__empty">
              <span>{translate("projects:homePage.homeHistoryEmpty_message_empty")}</span>
              <span>{translate("projects:homePage.homeHistoryEmpty_message_openProjectAuto")}</span>
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
                    title={translate("projects:homePage.homeHistoryItemRemove_title_remove")}
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

        {/* Right: Translation flow */}
        <section className="home-flow">
          <div className="home-flow__header">
            <h2>{translate('projects:homePage.translationFlow.title')}</h2>
          </div>
          <HomeTranslationFlow />
        </section>
      </div>
    </div>
  );
}

function projectName(projectDir: string): string {
  return basenamePath(projectDir) || projectDir;
}

function formatDate(isoString: string): string {
  try {
    const date = new Date(isoString);
    return date.toLocaleDateString(getUiLanguage(), {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return isoString;
  }
}
