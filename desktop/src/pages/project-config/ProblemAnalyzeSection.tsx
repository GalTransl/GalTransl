import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { fetchProblemTypes, type ProblemTypeInfo } from '../../lib/api';

interface ProblemAnalyzeSectionProps {
  config: Record<string, unknown> | null;
  onProblemListChange: (lines: string[]) => void;
  onDirty: () => void;
}

function readProblemList(config: Record<string, unknown> | null): string[] {
  const pa = (config?.problemAnalyze as Record<string, unknown>) || {};
  const raw = pa.problemList;
  if (Array.isArray(raw)) {
    return raw.map((x) => String(x ?? '').trim()).filter(Boolean);
  }
  if (typeof raw === 'string') {
    return raw.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  }
  return [];
}

export function ProblemAnalyzeSection({ config, onProblemListChange, onDirty }: ProblemAnalyzeSectionProps) {
  const [problemTypes, setProblemTypes] = useState<ProblemTypeInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProblemTypes()
      .then((list) => {
        if (!cancelled) {
          setProblemTypes(list);
          setLoadError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => { cancelled = true; };
  }, []);

  const selected = useMemo(() => readProblemList(config), [config]);
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  // Keep entries the user already has in config even if backend doesn't list them
  // (e.g. future types or custom strings); render them at the bottom.
  const extras = useMemo(() => {
    if (!problemTypes) return [] as string[];
    const known = new Set(problemTypes.map((t) => t.name));
    return selected.filter((name) => !known.has(name));
  }, [problemTypes, selected]);

  const commit = (nextSet: Set<string>) => {
    // Preserve the original order from backend, then append unknown extras.
    const ordered: string[] = [];
    if (problemTypes) {
      for (const t of problemTypes) {
        if (nextSet.has(t.name)) ordered.push(t.name);
      }
    }
    for (const name of extras) {
      if (nextSet.has(name)) ordered.push(name);
    }
    onProblemListChange(ordered);
    onDirty();
  };

  const toggle = (name: string, checked: boolean) => {
    const next = new Set(selectedSet);
    if (checked) next.add(name);
    else next.delete(name);
    commit(next);
  };

  const selectAll = () => {
    if (!problemTypes) return;
    const next = new Set<string>([...problemTypes.map((t) => t.name), ...extras]);
    commit(next);
  };

  const clearAll = () => {
    commit(new Set());
  };

  return (
    <Panel
      title="问题分析"
      description="选择启用的翻译质量问题检测项。翻译过程中命中的问题会写入缓存并展示在缓存编辑页。"
    >
      <div className="problem-analyze-section">
        {loadError && (
          <div className="problem-analyze-section__error">
            加载后端支持的问题项失败：{loadError}
          </div>
        )}

        {problemTypes === null && !loadError ? (
          <div className="problem-analyze-section__loading">正在加载后端支持的问题项…</div>
        ) : (
          <>
            <div className="problem-analyze-section__toolbar">
              <span className="problem-analyze-section__count">
                已启用 {selectedSet.size} / {(problemTypes?.length ?? 0) + extras.length}
              </span>
              <div className="problem-analyze-section__toolbar-actions">
                <button
                  type="button"
                  className="problem-analyze-section__btn"
                  onClick={selectAll}
                  disabled={!problemTypes || problemTypes.length === 0}
                >
                  全选
                </button>
                <button
                  type="button"
                  className="problem-analyze-section__btn"
                  onClick={clearAll}
                  disabled={selectedSet.size === 0}
                >
                  清空
                </button>
              </div>
            </div>

            <ul className="problem-analyze-section__list">
              {(problemTypes ?? []).map((item) => {
                const checked = selectedSet.has(item.name);
                return (
                  <li
                    key={item.name}
                    className={`problem-analyze-section__item${checked ? ' problem-analyze-section__item--checked' : ''}`}
                  >
                    <label className="problem-analyze-section__label">
                      <input
                        type="checkbox"
                        className="problem-analyze-section__checkbox"
                        checked={checked}
                        onChange={(e) => toggle(item.name, e.target.checked)}
                      />
                      <span className="problem-analyze-section__item-body">
                        <span className="problem-analyze-section__name">{item.name}</span>
                        {item.description && (
                          <span className="problem-analyze-section__desc">{item.description}</span>
                        )}
                      </span>
                    </label>
                  </li>
                );
              })}

              {extras.map((name) => (
                <li
                  key={`extra-${name}`}
                  className="problem-analyze-section__item problem-analyze-section__item--checked problem-analyze-section__item--extra"
                >
                  <label className="problem-analyze-section__label">
                    <input
                      type="checkbox"
                      className="problem-analyze-section__checkbox"
                      checked
                      onChange={(e) => toggle(name, e.target.checked)}
                    />
                    <span className="problem-analyze-section__item-body">
                      <span className="problem-analyze-section__name">{name}</span>
                      <span className="problem-analyze-section__desc problem-analyze-section__desc--warn">
                        当前后端未声明此问题项，取消勾选将从配置中移除。
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </Panel>
  );
}
