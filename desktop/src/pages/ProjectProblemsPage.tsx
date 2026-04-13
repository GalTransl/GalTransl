import { useCallback, useEffect, useMemo, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Panel } from '../components/Panel';
import {
  ApiError,
  type ProblemEntry,
  fetchProjectProblems,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

/** 兼容读取问题条目字段：优先新key，回退旧key */
function pSrc(p: ProblemEntry): string { return p.post_src || p.post_jp || ''; }
function pDst(p: ProblemEntry): string { return p.pre_dst || p.pre_zh || ''; }

const PROBLEM_TYPES = [
  '词频过高',
  '标点错漏',
  '残留日文',
  '丢失换行',
  '多加换行',
  '比日文长',
  '字典使用',
  '引入英文',
  '语言不通',
  '缺控制符',
  '比日文长严格',
];

export function ProjectProblemsPage() {
  const { projectDir, projectId } = useOutletContext<OutletContext>();

  const [problems, setProblems] = useState<ProblemEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    fetchProjectProblems(projectId)
      .then((res) => {
        if (!cancelled) setProblems(res.problems);
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载问题列表失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId]);

  // Group problems by type for stats
  const problemStats = useMemo(() => {
    const stats: Record<string, number> = {};
    for (const p of problems) {
      // Extract problem type (first part before special chars)
      const type = p.problem.split('：')[0].split('-')[0].trim();
      stats[type] = (stats[type] || 0) + 1;
    }
    return Object.entries(stats).sort((a, b) => b[1] - a[1]);
  }, [problems]);

  const filteredProblems = useMemo(() => {
    return problems.filter((p) => {
      if (typeFilter) {
        const type = p.problem.split('：')[0].split('-')[0].trim();
        if (type !== typeFilter) return false;
      }
      if (searchTerm) {
        const term = searchTerm.toLowerCase();
        return (
          pSrc(p)?.toLowerCase().includes(term) ||
          pDst(p)?.toLowerCase().includes(term) ||
          p.problem?.toLowerCase().includes(term)
        );
      }
      return true;
    });
  }, [problems, typeFilter, searchTerm]);

  if (loading) {
    return (
      <div className="project-problems-page">
        <div className="project-problems-page__header"><h1>问题审查</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-problems-page">
        <div className="project-problems-page__header"><h1>问题审查</h1></div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  return (
    <div className="project-problems-page">
      <div className="project-problems-page__header">
        <h1>问题审查</h1>
        <p>审查翻译质量问题，共 {problems.length} 个问题。</p>
      </div>

      <div className="project-problems-page__content">
        {/* Problem Stats */}
        <Panel title="问题统计" description="按类型分组统计。">
          <div className="problem-stats">
            {problemStats.map(([type, count]) => (
              <button
                key={type}
                className={`problem-stat-item ${typeFilter === type ? 'problem-stat-item--active' : ''}`}
                onClick={() => setTypeFilter(typeFilter === type ? '' : type)}
              >
                <span className="problem-stat-item__type">{type}</span>
                <span className="problem-stat-item__count">{count}</span>
              </button>
            ))}
          </div>
        </Panel>

        {/* Problem List */}
        <Panel title="问题列表" description={`${filteredProblems.length} 个问题`}>
          <div className="problem-toolbar">
            <input
              type="text"
              placeholder="搜索原文、译文或问题…"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="problem-search"
            />
            {typeFilter && (
              <button className="problem-clear-filter" onClick={() => setTypeFilter('')}>
                清除筛选: {typeFilter} ✕
              </button>
            )}
          </div>

          {filteredProblems.length === 0 ? (
            <div className="empty-state">
              <strong>没有匹配的问题</strong>
              <span>{problems.length === 0 ? '翻译质量良好，没有发现问题。' : '调整筛选条件查看更多问题。'}</span>
            </div>
          ) : (
            <div className="problem-list">
              {filteredProblems.map((p, i) => (
                <div key={`${p.filename}-${p.index}-${i}`} className="problem-entry">
                  <div className="problem-entry__header">
                    <span className="problem-entry__file">{p.filename}</span>
                    <span className="problem-entry__index">#{p.index}</span>
                    <span className="problem-badge">{p.problem}</span>
                  </div>
                  <div className="problem-entry__body">
                    <div className="problem-entry__row">
                      <span className="problem-entry__label">原文：</span>
                      <span className="problem-entry__jp">{pSrc(p)}</span>
                    </div>
                    <div className="problem-entry__row">
                      <span className="problem-entry__label">译文：</span>
                      <span className="problem-entry__zh">{pDst(p)}</span>
                    </div>
                    {p.speaker && (
                      <div className="problem-entry__row">
                        <span className="problem-entry__label">说话人：</span>
                        <span>{Array.isArray(p.speaker) ? p.speaker.join('/') : p.speaker}</span>
                      </div>
                    )}
                  </div>
                </div>
              ))}
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
