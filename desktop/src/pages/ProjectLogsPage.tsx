import { useCallback, useEffect, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Panel } from '../components/Panel';
import {
  ApiError,
  fetchProjectLogs,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';

const LOG_LEVELS: LogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

function parseLogLevel(line: string): LogLevel | null {
  for (const level of LOG_LEVELS) {
    if (line.includes(`[${level}]`)) return level;
  }
  return null;
}

function levelToNumber(level: LogLevel | null): number {
  if (!level) return 0;
  const map: Record<LogLevel, number> = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
  return map[level];
}

export function ProjectLogsPage() {
  const { projectDir, projectId } = useOutletContext<OutletContext>();

  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [logExists, setLogExists] = useState(false);
  const [minLevel, setMinLevel] = useState<number>(1); // INFO by default
  const [searchTerm, setSearchTerm] = useState('');
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const logEndRef = useRef<HTMLDivElement>(null);

  // Load logs
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    fetchProjectLogs(projectId)
      .then((res) => {
        if (!cancelled) {
          setLines(res.lines);
          setLogExists(res.exists);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载日志失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId]);

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && !paused && logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [lines, autoScroll, paused]);

  const filteredLines = lines.filter((line) => {
    const level = parseLogLevel(line);
    if (levelToNumber(level) < minLevel) return false;
    if (searchTerm) {
      return line.toLowerCase().includes(searchTerm.toLowerCase());
    }
    return true;
  });

  const getLineClass = (line: string): string => {
    const level = parseLogLevel(line);
    switch (level) {
      case 'ERROR':
      case 'CRITICAL':
        return 'log-line log-line--error';
      case 'WARNING':
        return 'log-line log-line--warning';
      case 'DEBUG':
        return 'log-line log-line--debug';
      default:
        return 'log-line';
    }
  };

  if (loading) {
    return (
      <div className="project-logs-page">
        <div className="project-logs-page__header"><h1>日志查看</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-logs-page">
        <div className="project-logs-page__header"><h1>日志查看</h1></div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  return (
    <div className="project-logs-page">
      <div className="project-logs-page__header">
        <h1>日志查看</h1>
        <p>查看翻译日志，排查问题。</p>
      </div>

      <div className="project-logs-page__content">
        <Panel
          title="翻译日志"
          description={logExists ? `${lines.length} 行日志` : '暂无日志文件（需在配置中开启 saveLog）'}
        >
          <div className="log-toolbar">
            <input
              type="text"
              placeholder="搜索日志…"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="log-search"
            />
            <select
              value={minLevel}
              onChange={(e) => setMinLevel(Number(e.target.value))}
              className="log-level-select"
            >
              <option value={0}>DEBUG</option>
              <option value={1}>INFO</option>
              <option value={2}>WARNING</option>
              <option value={3}>ERROR</option>
              <option value={4}>CRITICAL</option>
            </select>
            <button
              className={`log-pause-btn ${paused ? 'log-pause-btn--paused' : ''}`}
              onClick={() => setPaused(!paused)}
            >
              {paused ? '▶ 继续' : '⏸ 暂停'}
            </button>
          </div>

          {!logExists ? (
            <div className="empty-state">
              <strong>日志文件不存在</strong>
              <span>在项目配置中将 saveLog 设为 true 以启用日志记录。</span>
            </div>
          ) : (
            <div className="log-viewer">
              {filteredLines.map((line, i) => (
                <div key={i} className={getLineClass(line)}>
                  {line}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
