import { t as translate, useUiLanguage } from "../../i18n";
import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { ConfigFieldRow, ConfigFieldGroup, type ConfigFieldDef } from './ConfigFieldRow';
import { fetchTranslationGuidelines } from '../../lib/api';

// ── Primary (high-frequency) fields ──
const PRIMARY_FIELDS: ConfigFieldDef[] = [
  { key: 'workersPerProject', labelKey: "config:fields.workersPerProject.label", descriptionKey: "config:fields.workersPerProject.description", type: 'number', placeholder: '16' },
  { key: 'gpt.numPerRequestTranslate', labelKey: "config:fields.gpt_numPerRequestTranslate.label", descriptionKey: "config:fields.gpt_numPerRequestTranslate.description", type: 'number', placeholder: '16' },
  { key: 'gpt.dynamicNumPerRequestTranslate', labelKey: "config:fields.gpt_dynamicNumPerRequestTranslate.label", descriptionKey: "config:fields.gpt_dynamicNumPerRequestTranslate.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'gpt.dynamicNumPerRequestTranslate.min', labelKey: "config:fields.gpt_dynamicNumPerRequestTranslate_min.label", descriptionKey: "config:fields.gpt_dynamicNumPerRequestTranslate_min.description", type: 'number', placeholder: '8' },
  { key: 'gpt.dynamicNumPerRequestTranslate.max', labelKey: "config:fields.gpt_dynamicNumPerRequestTranslate_max.label", descriptionKey: "config:fields.gpt_dynamicNumPerRequestTranslate_max.description", type: 'number', placeholder: '64' },
  { key: 'language', labelKey: "config:fields.language.label", descriptionKey: "config:fields.language.description", type: 'select', options: [{ value: 'zh-cn', labelKey: "config:translationSettingsSection.option_zhCn" }, { value: 'zh-tw', labelKey: "config:translationSettingsSection.option_zhTw" }, { value: 'en', labelKey: "config:translationSettingsSection.option_en" }, { value: 'ja', labelKey: "config:translationSettingsSection.option_ja" }, { value: 'ko', labelKey: "config:translationSettingsSection.option_ko" }, { value: 'ru', labelKey: "config:translationSettingsSection.option_ru" }, { value: 'fr', labelKey: "config:translationSettingsSection.option_fr" }] },
  { key: 'sortBy', labelKey: "config:fields.sortBy.label", descriptionKey: "config:fields.sortBy.description", type: 'select', options: [{ value: 'name', labelKey: "config:translationSettingsSection.option_name" }, { value: 'size', labelKey: "config:translationSettingsSection.option_size" }] },
  { key: 'gpt.multiTurn', labelKey: "config:fields.gpt_multiTurn.label", descriptionKey: "config:fields.gpt_multiTurn.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'gpt.contextNum', labelKey: "config:fields.gpt_contextNum.label", descriptionKey: "config:fields.gpt_contextNum.description", type: 'number', placeholder: '8' },
  { key: 'gpt.translation_guideline', labelKey: "config:fields.gpt_translation_guideline.label", descriptionKey: "config:fields.gpt_translation_guideline.description", type: 'select', options: [] },
];

// 只在「动态句数调整」开启时才有意义的两个字段：关闭时从常用设置里隐去
const DYNAMIC_RANGE_KEYS = new Set([
  'gpt.dynamicNumPerRequestTranslate.min',
  'gpt.dynamicNumPerRequestTranslate.max',
]);

// ── Advanced (low-frequency) fields ──
const ADVANCED_FIELDS: ConfigFieldDef[] = [
  { key: 'gpt.multiTurn.maxTurns', labelKey: "config:fields.gpt_multiTurn_maxTurns.label", descriptionKey: "config:fields.gpt_multiTurn_maxTurns.description", type: 'number', placeholder: '8' },
  { key: 'gpt.multiTurn.maxChars', labelKey: "config:fields.gpt_multiTurn_maxChars.label", descriptionKey: "config:fields.gpt_multiTurn_maxChars.description", type: 'number', placeholder: '24000' },
  { key: 'start_time', labelKey: "config:fields.start_time.label", descriptionKey: "config:fields.start_time.description", type: 'text', placeholderKey: "config:fields.start_time.placeholder" },
  { key: 'skipH', labelKey: "config:fields.skipH.label", descriptionKey: "config:fields.skipH.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'smartRetry', labelKey: "config:fields.smartRetry.label", descriptionKey: "config:fields.smartRetry.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'retranslFail', labelKey: "config:fields.retranslFail.label", descriptionKey: "config:fields.retranslFail.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'gpt.enhance_jailbreak', labelKey: "config:fields.gpt_enhance_jailbreak.label", descriptionKey: "config:fields.gpt_enhance_jailbreak.description", type: 'select', options: [{ value: 'true', labelKey: "config:translationSettingsSection.option_true" }, { value: 'false', labelKey: "config:translationSettingsSection.option_false" }] },
  { key: 'gpt.token_limit', labelKey: "config:fields.gpt_token_limit.label", descriptionKey: "config:fields.gpt_token_limit.description", type: 'number', placeholder: '0' },
];

interface TranslationSettingsSectionProps {
  commonConfig: Record<string, unknown>;
  onFieldChange: (path: string, value: string) => void;
  onListFieldChange?: (path: string, value: string[]) => void;
}

export function TranslationSettingsSection({ commonConfig, onFieldChange, onListFieldChange }: TranslationSettingsSectionProps) {
  const uiLanguage = useUiLanguage();
  const [guidelines, setGuidelines] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetchTranslationGuidelines()
      .then((list) => { if (!cancelled) setGuidelines(list); })
      .catch(() => { /* optional; fallback to current value only */ });
    return () => { cancelled = true; };
  }, []);

  const primaryFields = useMemo<ConfigFieldDef[]>(() => {
    const currentGuideline = String(getFieldValue(commonConfig, 'gpt.translation_guideline') ?? '');
    const merged = [...guidelines];
    if (currentGuideline && !merged.includes(currentGuideline)) {
      merged.unshift(currentGuideline);
    }
    // 动态句数调整关闭时，上下限没人用，不显示
    const dynamicRaw = getFieldValue(commonConfig, 'gpt.dynamicNumPerRequestTranslate');
    const dynamicEnabled = dynamicRaw === true || String(dynamicRaw ?? '').trim().toLowerCase() === 'true';
    return PRIMARY_FIELDS
      .filter((field) => dynamicEnabled || !DYNAMIC_RANGE_KEYS.has(field.key))
      .map((field) =>
        field.key === 'gpt.translation_guideline'
          ? { ...field, options: merged }
          : field,
      );
  }, [commonConfig, guidelines]);

  return (
    <Panel title={translate("config:translationSettingsSection.translationSettingsSection_title_translationSettings")} description={translate("config:translationSettingsSection.translationSettingsSection_description_configTargetLanguageTranslationConcurrencySentenceContext")}>
      <ConfigFieldGroup title={translate("common:actions.commonSettings")} tier="primary">
        {primaryFields.map((field) => (
          <ConfigFieldRow
            key={field.key}
            field={field}
            value={getFieldValue(commonConfig, field.key) ?? (field.key === 'gpt.multiTurn' ? true : undefined)}
            onChange={onFieldChange}
            pathPrefix="common"
            tier="primary"
          />
        ))}
      </ConfigFieldGroup>

      <details className="config-advanced-details">
        <summary className="config-advanced-details__summary">{translate("common:actions.advancedSettings")}</summary>
        <ConfigFieldGroup title={translate("common:actions.advancedSettings")} tier="advanced">
          {ADVANCED_FIELDS.map((field) => (
            <ConfigFieldRow
              key={field.key}
              field={field}
              value={getFieldValue(commonConfig, field.key)}
              onChange={onFieldChange}
              onListChange={onListFieldChange}
              pathPrefix="common"
              tier="advanced"
            />
          ))}
        </ConfigFieldGroup>
      </details>
    </Panel>
  );
}

/**
 * Get a value from an object by dot-separated path. Prefers literal flat keys
 * (YAML under `common:` uses flat dotted keys like `gpt.translation_guideline`).
 */
function getFieldValue(obj: Record<string, unknown>, path: string): unknown {
  const keys = path.split('.');
  let current: unknown = obj;
  for (let i = 0; i < keys.length; i++) {
    if (current == null || typeof current !== 'object') return undefined;
    const remaining = keys.slice(i).join('.');
    const cur = current as Record<string, unknown>;
    if (Object.prototype.hasOwnProperty.call(cur, remaining)) {
      return cur[remaining];
    }
    current = cur[keys[i]];
  }
  return current;
}
