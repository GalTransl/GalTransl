import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { Button } from '../components/Button';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback } from '../components/page-state';
import { StatsGrid } from '../components/StatsGrid';
import { JobCard } from '../features/jobs/JobCard';
import { encodeProjectDir, fetchJobs, fetchProjectRuntime, type Job } from '../lib/api';
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
    lastOpened: new Date().toISOString() });
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
  const [jobProgressById, setJobProgressById] = useState<Record<string, {
    currentFile?: string;
    percent: number;
    total: number;
    translated: number;
  }>>({});

  useEffect(() => {
    setHistory(loadHistory());
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
              return [job.job_id, {
                currentFile: runtime.current_file,
                percent: runtime.summary.percent,
                total: runtime.summary.total,
                translated: runtime.summary.translated }] as const;
            } catch {
              return null;
            }
          }),
        );

        setJobProgressById(
          progressEntries.reduce<Record<string, { currentFile?: string; percent: number; total: number; translated: number }>>((acc, entry) => {
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
      ] });
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

  const handleRemoveHistory = useCallback(
    (projectDirToRemove: string, event: React.MouseEvent) => {
      event.stopPropagation();
      removeProjectFromHistory(projectDirToRemove);
      setHistory(loadHistory());
    },
    [],
  );

  const activeJobsCount = useMemo(
    () => jobs.filter((job) => job.status === 'pending' || job.status === 'running').length,
    [jobs],
  );
  const completedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'completed').length, [jobs]);
  const failedJobsCount = useMemo(() => jobs.filter((job) => job.status === 'failed').length, [jobs]);

  return (
    <div className="home-page">
      <PageHeader
        className="home-page__hero"
        title="GalTransl Desktop"
        description="本地翻译后端桌面控制台，管理翻译项目与实时查看翻译进度。"
        status={
          <StatsGrid className="home-page__overview" compact>
            <MetricCard label="历史项目" value={history.length} tone="accent" />
            <MetricCard label="活跃任务" value={activeJobsCount} tone="success" />
            <MetricCard label="已完成" value={completedJobsCount} />
            <MetricCard label="失败" value={failedJobsCount} tone={failedJobsCount > 0 ? 'danger' : 'default'} />
          </StatsGrid>
        }
      />

      <div className="home-page__content">
        <div className="home-page__left">
          <Panel title="打开项目">
            <form
              className="form-stack"
              onSubmit={(e) => {
                e.preventDefault();
                handleOpenProject();
              }}
            >
              <label className="field">
                <span>项目目录</span>
                <input
                  autoComplete="off"
                  onChange={(e) => setProjectDir(e.target.value)}
                  placeholder="E:\GalTransl\sampleProject"
                  value={projectDir}
                />
              </label>

              <label className="field">
                <span>配置文件</span>
                <input
                  autoComplete="off"
                  onChange={(e) => setConfigFileName(e.target.value)}
                  value={configFileName}
                />
              </label>

              <div className="form-actions">
                <Button type="button" variant="secondary" onClick={handleSelectConfigFile}>
                  浏览
                </Button>
                <Button type="submit" disabled={!projectDir.trim()}>
                  打开
                </Button>
                <Button type="button" variant="secondary" onClick={() => navigate('/new-project')}>
                  新建
                </Button>
              </div>
            </form>
          </Panel>

          <Panel title="历史项目">
            {history.length === 0 ? (
              <EmptyState title="暂无历史" description="打开项目后自动出现在这里。" />
            ) : (
              <div className="history-list">
                {history.map((entry) => (
                  <div key={entry.projectDir} className="history-item">
                    <button
                      type="button"
                      className="history-item__button"
                      onClick={() => handleHistoryClick(entry)}
                    >
                      <div className="history-item__info">
                        <div className="history-item__path">{entry.projectDir}</div>
                        <div className="history-item__meta">
                          {entry.configFileName} · {formatDate(entry.lastOpened)}
                        </div>
                      </div>
                    </button>
                    <button
                      type="button"
                      className="history-item__remove"
                      onClick={(e) => handleRemoveHistory(entry.projectDir, e)}
                      title="从历史中移除"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </div>

        <div className="home-page__right">
          <Panel
            title="全局翻译任务"
            actions={
              <Button disabled={refreshingJobs} onClick={() => void refreshJobs()} variant="secondary">
                {refreshingJobs ? '…' : '刷新'}
              </Button>
            }
          >
            {jobsError ? <InlineFeedback tone="error" title="加载失败" description={jobsError} /> : null}

            {jobs.length === 0 ? (
              <EmptyState title="还没有翻译任务" description="启动翻译后，任务会汇总在这里。" />
            ) : (
              <div className="job-list">
                {jobs.map((job) => (
                  <JobCard job={job} key={job.job_id} progress={jobProgressById[job.job_id]} />
                ))}
              </div>
            )}
          </Panel>
        </div>
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
      minute: '2-digit' });
  } catch {
    return isoString;
  }
}
