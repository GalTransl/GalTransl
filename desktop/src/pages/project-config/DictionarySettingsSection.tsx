import { t as translate, useUiLanguage } from "../../i18n";
import { Panel } from '../../components/Panel';
import { Switch } from '../../components/Switch';

interface DictionarySettingsSectionProps {
  dictConfig: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
}

export function DictionarySettingsSection({ dictConfig, onChange }: DictionarySettingsSectionProps) {
  const uiLanguage = useUiLanguage();
  return (
    <Panel title={translate("config:dictionarySettingsSection.dictionarySettingsSection_title_dictionarySettings")} description={translate("config:dictionarySettingsSection.dictionarySettingsSection_description_gPTDictionaryFileConfigProjectDirProject")}>
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
  useUiLanguage();
  return (
    <>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryFile")}</span>
        <input
          type="text"
          value={String(dictConfig.defaultDictFolder ?? 'Dict')}
          onChange={(e) => onChange({ ...dictConfig, defaultDictFolder: e.target.value })}
        />
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionary")}</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.preDict) ? (dictConfig.preDict as string[]).join('\n') : String(dictConfig.preDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, preDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">{translate("config:dictionarySettingsSection.field_message_countDictionaryFile")}</span>
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_gPTDictionary")}</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig['gpt.dict']) ? (dictConfig['gpt.dict'] as string[]).join('\n') : String(dictConfig['gpt.dict'] ?? '')}
          onChange={(e) => onChange({ ...dictConfig, 'gpt.dict': e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">{translate("config:dictionarySettingsSection.field_message_countDictionaryFile")}</span>
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryVariant2")}</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.postDict) ? (dictConfig.postDict as string[]).join('\n') : String(dictConfig.postDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, postDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">{translate("config:dictionarySettingsSection.field_message_countDictionaryFile")}</span>
      </label>
      <label className="field field--switch">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryName")}</span>
        <Switch
          checked={dictConfig.usePreDictInName === true}
          onChange={(next) => onChange({ ...dictConfig, usePreDictInName: next })}
        />
      </label>
      <label className="field field--switch">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryNameGPT")}</span>
        <Switch
          checked={dictConfig.useGPTDictInName === true}
          onChange={(next) => onChange({ ...dictConfig, useGPTDictInName: next })}
        />
      </label>
      <label className="field field--switch">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryNameVariant2")}</span>
        <Switch
          checked={dictConfig.usePostDictInName === true}
          onChange={(next) => onChange({ ...dictConfig, usePostDictInName: next })}
        />
      </label>
      <label className="field field--switch">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryVariant3")}</span>
        <Switch
          checked={dictConfig.sortDict !== false}
          onChange={(next) => onChange({ ...dictConfig, sortDict: next })}
        />
      </label>
    </>
  );
}
