import { UiTrans, t as translate, useUiLanguage } from "../../i18n";
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
  const uiLanguage = useUiLanguage();
  const common = (config?.common as Record<string, unknown>) || {};
  const filterKeys = useMemo(() => normalizeKeywordList(common.problemFilterKey), [common.problemFilterKey]);
  const whiteList = useMemo(() => normalizeKeywordList(common.problemWhiteList), [common.problemWhiteList]);
  const [tab, setTab] = useState<ProblemFilterTab>('filter');

  const tabs: { key: ProblemFilterTab; label: string; count: number }[] = [
    { key: 'filter', label: translate("config:problemFilterSection.label_label_filter"), count: filterKeys.length },
    { key: 'whiteList', label: translate("config:problemFilterSection.label_label_filterVariant2"), count: whiteList.length },
  ];

  return (
    <Panel title={translate("config:problemFilterSection.problemFilterSection_title_problemFilter")}>
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
          <p className="problem-filter-section__desc"><UiTrans k="config:problemFilterSection.problemFilterSectionGroup_message_0Regex0FilterItemEntryRegex" components={[<strong />, <code />, <code />, <strong />, <code />, <code />]} /></p>
          <KeyListEditor
            keys={filterKeys}
            onChange={(keys) => onChange('problemFilterKey', keys)}
            onDirty={onDirty}
            placeholder={translate("config:problemFilterSection.problemFilterSectionGroup_placeholder_regexJapanese")}
            emptyText={translate("config:problemFilterSection.problemFilterSectionGroup_emptyText_emptyFilter")}
          />
        </div>
      ) : (
        <div className="problem-filter-section__group">
          <p className="problem-filter-section__desc">{translate("config:problemFilterSection.problemFilterSectionGroup_message_cacheFileIndexEntryTranslationTextEntrySkip")}</p>
          <KeyListEditor
            keys={whiteList}
            onChange={(keys) => onChange('problemWhiteList', keys)}
            onDirty={onDirty}
            placeholder={translate("config:problemFilterSection.problemFilterSectionGroup_placeholder_cacheFileIndex01Json1201")}
            emptyText={translate("config:problemFilterSection.problemFilterSectionGroup_emptyText_emptyEntry")}
            validate={validateProblemWhiteListEntry}
          />
        </div>
      )}
    </Panel>
  );
}
