import { t as translate, useUiLanguage } from "../../i18n";
import { Panel } from '../../components/Panel';
import { CustomSelect } from '../../components/CustomSelect';

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
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryName")}</span>
        <CustomSelect
          value={String(dictConfig.usePreDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePreDictInName: e.target.value === 'true' })}
        >
          <option value="true">{translate("config:dictionarySettingsSection.field_message_text")}</option>
          <option value="false">{translate("config:dictionarySettingsSection.field_message_textVariant2")}</option>
        </CustomSelect>
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryNameGPT")}</span>
        <CustomSelect
          value={String(dictConfig.useGPTDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, useGPTDictInName: e.target.value === 'true' })}
        >
          <option value="true">{translate("config:dictionarySettingsSection.field_message_text")}</option>
          <option value="false">{translate("config:dictionarySettingsSection.field_message_textVariant2")}</option>
        </CustomSelect>
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryNameVariant2")}</span>
        <CustomSelect
          value={String(dictConfig.usePostDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePostDictInName: e.target.value === 'true' })}
        >
          <option value="true">{translate("config:dictionarySettingsSection.field_message_text")}</option>
          <option value="false">{translate("config:dictionarySettingsSection.field_message_textVariant2")}</option>
        </CustomSelect>
      </label>
      <label className="field">
        <span>{translate("config:dictionarySettingsSection.field_message_dictionaryVariant3")}</span>
        <CustomSelect
          value={String(dictConfig.sortDict ?? 'true')}
          onChange={(e) => onChange({ ...dictConfig, sortDict: e.target.value === 'true' })}
        >
          <option value="true">{translate("config:dictionarySettingsSection.field_message_text")}</option>
          <option value="false">{translate("config:dictionarySettingsSection.field_message_textVariant2")}</option>
        </CustomSelect>
      </label>
    </>
  );
}
