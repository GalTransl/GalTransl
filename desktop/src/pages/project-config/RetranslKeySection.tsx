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
  const keys = useMemo(() => readKeys(config, field), [config, field]);
  const isFilter = field === 'problemFilterKey';

  return (
    <Panel
      title={isFilter ? '问题过滤关键字' : '重翻关键字'}
      description={isFilter ? undefined : '原文、译文、问题中命中这些关键字的句子会在下次启动时被重翻。'}
    >
      <KeyListEditor
        keys={keys}
        onChange={onChange}
        onDirty={onDirty}
        placeholder={isFilter ? '输入问题关键字后按回车或点击添加' : '输入关键字后按回车或点击添加'}
        emptyText={isFilter ? '暂无过滤关键字' : '暂无重翻关键字。添加后，下次启动时命中这些关键字的句子会被重新翻译。'}
      />
    </Panel>
  );
}
