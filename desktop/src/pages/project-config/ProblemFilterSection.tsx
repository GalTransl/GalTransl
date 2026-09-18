import { useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { normalizeKeywordList, validateProblemWhiteListEntry } from '../../lib/problemFilter';
import { KeyListEditor } from './KeyListEditor';

export type ProblemFilterListField = 'problemFilterKey' | 'problemWhiteList';

type ProblemFilterTab = 'filter' | 'whiteList';

interface ProblemFilterSectionProps {
  config: Record<string, unknown> | null;
  onChange: (field: ProblemFilterListField, keys: string[]) => void;
  onDirty: () => void;
}

/**
 * 「问题过滤」页：两个 tab 分别管理两份 common 配置。
 * - problemFilterKey：按问题文本子串整类过滤；
 * - problemWhiteList：按「缓存文件名:index」精确豁免条目（等价于给该条勾 skip_check）。
 */
export function ProblemFilterSection({ config, onChange, onDirty }: ProblemFilterSectionProps) {
  const common = (config?.common as Record<string, unknown>) || {};
  const filterKeys = useMemo(() => normalizeKeywordList(common.problemFilterKey), [common.problemFilterKey]);
  const whiteList = useMemo(() => normalizeKeywordList(common.problemWhiteList), [common.problemWhiteList]);
  const [tab, setTab] = useState<ProblemFilterTab>('filter');

  const tabs: { key: ProblemFilterTab; label: string; count: number }[] = [
    { key: 'filter', label: '过滤关键字', count: filterKeys.length },
    { key: 'whiteList', label: '过滤白名单', count: whiteList.length },
  ];

  return (
    <Panel title="问题过滤">
      <div className="problem-filter-tabs" role="tablist">
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            className={`problem-filter-tab${tab === item.key ? ' problem-filter-tab--active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            {item.label}
            <span className="problem-filter-tab__count">{item.count}</span>
          </button>
        ))}
      </div>

      {tab === 'filter' ? (
        <div className="problem-filter-section__group">
          <p className="problem-filter-section__desc">
            按<strong>正则</strong>过滤：每项是一条正则，命中问题项（任意位置）的那条就被丢掉。
            如 <code>缺失.*标点</code>、<code>^残留日文：♪</code>。
            <strong>原则上只过滤小类</strong>：<code>残留日文</code>、<code>^残留日文：</code>
            这类整类写法会把大类里的真问题一起藏起来，不建议用。
            想只按字面过滤某一条，把特殊字符转义（在「缓存与问题」页点问题项后面的 - 号会自动转义）。
          </p>
          <KeyListEditor
            keys={filterKeys}
            onChange={(keys) => onChange('problemFilterKey', keys)}
            onDirty={onDirty}
            placeholder="输入正则（如 ^残留日文：）"
            emptyText="暂无过滤关键字"
          />
        </div>
      ) : (
        <div className="problem-filter-section__group">
          <p className="problem-filter-section__desc">
            按「缓存文件名:index」精确豁免某几条译文，等价于给该条勾选 skip_check：
            不再检测、不计入统计。index 支持闭区间（如 01.json:12-15）。
          </p>
          <KeyListEditor
            keys={whiteList}
            onChange={(keys) => onChange('problemWhiteList', keys)}
            onDirty={onDirty}
            placeholder="输入 缓存文件名:index（如 01.json:12 或 01.json:12-15）"
            emptyText="暂无白名单条目"
            validate={validateProblemWhiteListEntry}
          />
        </div>
      )}
    </Panel>
  );
}
