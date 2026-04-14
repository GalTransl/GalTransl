import { Panel } from '../../components/Panel';

interface DictionarySettingsSectionProps {
  dictConfig: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
}

export function DictionarySettingsSection({ dictConfig, onChange }: DictionarySettingsSectionProps) {
  return (
    <Panel title="字典设置" description="译前/GPT/译后字典文件配置。">
      <DictConfigEditor dictConfig={dictConfig} onChange={onChange} />
    </Panel>
  );
}

// ---- Dictionary Config Sub-editor ----

function DictConfigEditor({
  dictConfig,
  onChange }: {
  dictConfig: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
}) {
  return (
    <>
      <label className="field">
        <span>通用字典文件夹</span>
        <input
          type="text"
          value={String(dictConfig.defaultDictFolder ?? 'Dict')}
          onChange={(e) => onChange({ ...dictConfig, defaultDictFolder: e.target.value })}
        />
      </label>
      <label className="field">
        <span>译前字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.preDict) ? (dictConfig.preDict as string[]).join('\n') : String(dictConfig.preDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, preDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>GPT字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig['gpt.dict']) ? (dictConfig['gpt.dict'] as string[]).join('\n') : String(dictConfig['gpt.dict'] ?? '')}
          onChange={(e) => onChange({ ...dictConfig, 'gpt.dict': e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>译后字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.postDict) ? (dictConfig.postDict as string[]).join('\n') : String(dictConfig.postDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, postDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>字典用在name字段(译前)</span>
        <select
          value={String(dictConfig.usePreDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePreDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典用在name字段(GPT)</span>
        <select
          value={String(dictConfig.useGPTDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, useGPTDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典用在name字段(译后)</span>
        <select
          value={String(dictConfig.usePostDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePostDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典排序</span>
        <select
          value={String(dictConfig.sortDict ?? 'true')}
          onChange={(e) => onChange({ ...dictConfig, sortDict: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
    </>
  );
}
