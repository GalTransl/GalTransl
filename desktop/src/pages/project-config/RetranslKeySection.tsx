import { t as translate, useUiLanguage } from "../../i18n";
import { useMemo } from 'react';
import { Panel } from '../../components/Panel';
import { normalizeKeywordList } from '../../lib/problemFilter';
import { KeyListEditor } from './KeyListEditor';

interface RetranslKeySectionProps {
  config: Record<string, unknown> | null;
  onChange: (keys: string[]) => void;
  onDirty: () => void;
  field?: 'retranslKey' | 'problemFilterKey';
}

function readKeys(config: Record<string, unknown> | null, field: 'retranslKey' | 'problemFilterKey'): string[] {
  const common = (config?.common as Record<string, unknown>) || {};
  return normalizeKeywordList(common[field]);
}

export function RetranslKeySection({ config, onChange, onDirty, field = 'retranslKey' }: RetranslKeySectionProps) {
  const uiLanguage = useUiLanguage();
  const keys = useMemo(() => readKeys(config, field), [config, field]);
  const isFilter = field === 'problemFilterKey';

  return (
    <Panel
      title={isFilter ? translate("config:retranslKeySection.retranslKeySection_title_problemFilter") : translate("config:retranslKeySection.retranslKeySection_title_retranslate")}
      description={isFilter ? undefined : translate("config:retranslKeySection.retranslKeySection_description_sourceTranslationTextProblemSentenceRetranslate")}
    >
      <KeyListEditor
        keys={keys}
        onChange={onChange}
        onDirty={onDirty}
        placeholder={isFilter ? translate("config:retranslKeySection.retranslKeySection_placeholder_problemAdd") : translate("config:retranslKeySection.retranslKeySection_placeholder_add")}
        emptyText={isFilter ? translate("config:retranslKeySection.retranslKeySection_emptyText_emptyFilter") : translate("config:retranslKeySection.retranslKeySection_emptyText_emptyRetranslateAddSentenceTranslation")}
      />
    </Panel>
  );
}
