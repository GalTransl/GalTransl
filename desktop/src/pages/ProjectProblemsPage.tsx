import { useEffect, useMemo, useState } from 'react';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import { StatsGrid } from '../components/StatsGrid';
import {
  type ProblemEntry,
  fetchProjectProblems } from '../lib/api';
import { normalizeError } from '../lib/errors';

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

export function ProjectProblemsPage({ ctx }: { ctx: ProjectPageContext }) {
  const { projectDir, projectId } = ctx;

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
        if (!cancelled) setError(normalizeError(err, '加载问题列表失败'));
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
        <PageHeader className="project-problems-page__header" title="问题审查" />
        <LoadingState title="加载问题列表中…" description="正在读取项目问题检测结果。" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-problems-page">
        <PageHeader className="project-problems-page__header" title="问题审查" />
        <ErrorState title="加载问题列表失败" description={error} />
      </div>
    );
  }

  return (
    <div className="project-problems-page">
      <PageHeader className="project-problems-page__header" title="问题审查" description={`审查翻译质量问题，共 ${problems.length} 个问题。`} />

      <div className="project-problems-page__content">
        {/* Problem Stats */}
        <Panel title="问题统计" description="按类型分组统计。">
          <StatsGrid className="problem-stats" compact>
            {problemStats.map(([type, count]) => (
              <MetricCard
                key={type}
                active={typeFilter === type}
                hint={typeFilter === type ? '再次点击取消筛选' : '点击按该问题类型筛选'}
                label={type}
                onClick={() => setTypeFilter(typeFilter === type ? '' : type)}
                tone={typeFilter === type ? 'danger' : 'default'}
                value={count}
              />
            ))}
          </StatsGrid>
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
            <EmptyState
              title={problems.length === 0 ? '暂未发现问题' : '没有匹配的问题'}
              description={problems.length === 0 ? '翻译质量良好，目前没有检测到问题条目。' : '调整筛选条件后再试一次。'}
            />
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

