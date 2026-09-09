import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { fetchProblemTypes, type ProblemTypeInfo } from '../../lib/api';

interface ProblemAnalyzeSectionProps {
  config: Record<string, unknown> | null;
  onProblemListChange: (lines: string[]) => void;
  onThresholdChange: (value: number) => void;
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

const DEFAULT_THRESHOLD = 17;
// 十进制数字字面量：Number() 会接受 "0x10"/"Infinity" 等后端 float() 会拒的写法
const DECIMAL_LITERAL_RE = /^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$/;

// 与后端 CProjectConfig.getAvgSentenceLengthThreshold 守卫口径逐条对齐：
// 拒布尔、拒非十进制字面量、拒非有限数、拒非整数、拒 <=0（统一回退 17）。
function resolveThreshold(config: Record<string, unknown> | null): number {
  const pa = (config?.problemAnalyze as Record<string, unknown>) || {};
  const raw = pa.avgSentenceLengthThreshold;
  if (typeof raw === 'boolean') return DEFAULT_THRESHOLD;
  if (typeof raw === 'string' && !DECIMAL_LITERAL_RE.test(raw.trim())) return DEFAULT_THRESHOLD;
  const f = Number(raw);
  if (!Number.isFinite(f) || !Number.isInteger(f)) return DEFAULT_THRESHOLD;
  if (f <= 0) return DEFAULT_THRESHOLD;
  return f;
}

export function ProblemAnalyzeSection({ config, onProblemListChange, onThresholdChange, onDirty }: ProblemAnalyzeSectionProps) {
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

  const threshold = useMemo(() => resolveThreshold(config), [config]);

  // 本地输入态：保留用户输入的原始字符串（含空串 / 非整数等中间态），
  // 以便对阈值类型做实时检测与提示，而不是直接吞掉非法输入。
  const [thresholdInput, setThresholdInput] = useState<string>(String(threshold));
  const [thresholdError, setThresholdError] = useState<string | null>(null);

  // 依赖必须是 config 而非 threshold：阈值数值未变时（如两个项目都默认 17）
  // 依赖 threshold 会让上一项目的错误输入与提示残留到新项目。
  useEffect(() => {
    setThresholdInput(String(resolveThreshold(config)));
    setThresholdError(null);
  }, [config]);

  const validateAndCommitThreshold = (raw: string) => {
    setThresholdInput(raw);
    const trimmed = raw.trim();
    if (trimmed === '') {
      setThresholdError('阈值不能为空，请输入大于 0 的整数');
      return;
    }
    const v = Number(trimmed);
    if (!Number.isInteger(v) || v <= 0) {
      setThresholdError('阈值类型无效：请输入大于 0 的整数');
      return;
    }
    setThresholdError(null);
    onThresholdChange(v);
    onDirty();
  };

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
      description="选择启用的翻译质量问题检测项。翻译过程中命中的问题会写入缓存并展示在缓存与问题页。"
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

            <div className="problem-analyze-section__threshold">
              <label className="problem-analyze-section__threshold-label">
                <span>长句判定阈值（avgSentenceLengthThreshold）</span>
                <input
                  type="number"
                  className={`problem-analyze-section__threshold-input${thresholdError ? ' problem-analyze-section__threshold-input--error' : ''}`}
                  min={1}
                  max={99}
                  value={thresholdInput}
                  onChange={(e) => validateAndCommitThreshold(e.target.value)}
                  onBlur={(e) => validateAndCommitThreshold(e.target.value)}
                />
              </label>
              <span className="problem-analyze-section__threshold-desc">
                译文平均分句长度超过此值时标记为"单句过长"。默认 17，建议范围 15~25。
              </span>
              {thresholdError && (
                <span className="problem-analyze-section__threshold-error">
                  {thresholdError}
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </Panel>
  );
}
