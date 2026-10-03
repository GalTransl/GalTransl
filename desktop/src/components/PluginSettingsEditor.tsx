import { localizePlugin } from "../i18n/plugins";
import { t as translate, useUiLanguage } from "../i18n";
import { Fragment, useEffect, useId, useState, type ReactNode } from 'react';
import type { PluginInfo, PluginSettingSchema } from '../lib/api';
import { CustomSelect } from './CustomSelect';

interface PluginSettingsEditorProps {
  plugin: PluginInfo;
  overrides: Record<string, unknown>;
  onChange: (pluginName: string, key: string, value: unknown) => void;
  afterField?: { key: string; content: ReactNode };
}

/** Settings 保持运行时默认值，SettingsSchema 仅描述如何展示和编辑。 */
export function PluginSettingsEditor({ plugin, overrides, onChange, afterField }: PluginSettingsEditorProps) {
  const uiLanguage = useUiLanguage();
  const settings = plugin.settings || {};
  const keys = Object.keys(settings);
  const schema = localizePlugin(plugin).settings_schema || {};
  const common = keys.filter((key) => !schema[key]?.advanced);
  const advanced = keys.filter((key) => schema[key]?.advanced);
  const renderFields = (fields: string[]) => (
    <div className="plugin-settings-panel__fields">
      {fields.map((key) => (
        <Fragment key={key}>
          <PluginSettingRow settingKey={key} schema={schema[key] || {}}
            defaultValue={settings[key]}
            value={overrides[key] !== undefined ? overrides[key] : settings[key]}
            onChange={(value) => onChange(plugin.name, key, value)} />
          {afterField?.key === key && afterField.content}
        </Fragment>
      ))}
    </div>
  );
  if (!keys.length) return <div className="plugin-settings-empty">{translate("plugins:pluginSettingsEditor.pluginSettingsEditor_message_pluginConfigSettingsItem")}</div>;
  return (
    <div className="plugin-settings-panel">
      <div className="plugin-settings-panel__title">{translate("plugins:pluginSettingsEditor.pluginSettingsPanel_message_settings", { display_name: localizePlugin(plugin).display_name })}</div>
      {renderFields(common)}
      {afterField && !keys.includes(afterField.key) && afterField.content}
      {advanced.length > 0 && (
        <details key={plugin.name} className="plugin-settings-advanced">
          <summary>{translate("plugins:pluginSettingsEditor.pluginSettingsAdvanced_message_advancedSettingsItem", { count: advanced.length })}</summary>
          {renderFields(advanced)}
        </details>
      )}
    </div>
  );
}

function PluginSettingRow({ settingKey, schema, defaultValue, value, onChange }: {
  settingKey: string;
  schema: PluginSettingSchema;
  defaultValue: unknown;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const uiLanguage = useUiLanguage();
  const id = useId();
  const hintId = `${id}-hint`;
  const label = schema.label || settingKey;
  const describedBy = schema.description ? hintId : undefined;
  const options = schema.options || [];
  let control;
  if (options.length && Array.isArray(defaultValue)) {
    const selected = Array.isArray(value) ? value : [];
    const allOptions = [...options, ...selected.filter((item) => !options.some((option) => option.value === item))
      .map((item) => ({ value: item, label: translate("plugins:pluginSettingsEditor.label_label_currentCustom", { value: String(item) }) }))];
    control = <div role="group" aria-label={label} aria-describedby={describedBy}>
      {allOptions.map((option, index) => <label key={index} className="plugin-setting-choice">
        <input type="checkbox" checked={selected.includes(option.value)}
          onChange={(event) => onChange(event.target.checked
            ? [...selected, option.value] : selected.filter((item) => item !== option.value))} />
        <span>{option.label}</span>
      </label>)}
    </div>;
  } else if (options.length) {
    // 选项使用序号作为 DOM value，保留数字/布尔类型及空字符串。
    const selected = options.findIndex((option) => option.value === value);
    control = (
      <CustomSelect id={id} aria-label={label} aria-describedby={describedBy}
        value={selected < 0 ? 'custom' : String(selected)}
        onChange={(event) => {
          const option = options[Number(event.target.value)];
          if (option) onChange(option.value);
        }}>
        {selected < 0 && <option value="custom">{translate("plugins:pluginSettingsEditor.pluginSettingRow_message_currentCustom", { value: String(value ?? '') })}</option>}
        {options.map((option, index) => <option key={index} value={String(index)}>{option.label}</option>)}
      </CustomSelect>
    );
  } else if (typeof defaultValue === 'boolean') {
    control = (
      <label className="toggle-switch">
        <input id={id} aria-describedby={describedBy} type="checkbox" checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)} />
        <span className="toggle-switch__slider" />
      </label>
    );
  } else if (typeof defaultValue === 'number') {
    control = <input id={id} aria-describedby={describedBy} type="number"
      className="plugin-setting-input plugin-setting-input--number" value={String(value ?? '')}
      min={schema.min} max={schema.max} step={schema.step ?? 'any'}
      onChange={(event) => {
        const raw = event.target.value;
        onChange(raw === '' || raw === '-' ? raw : Number(raw));
      }} />;
  } else if (defaultValue !== null && typeof defaultValue === 'object' && !Array.isArray(defaultValue)) {
    control = <ObjectSettingInput id={id} describedBy={describedBy} value={value} onChange={onChange} />;
  } else if (Array.isArray(defaultValue) || schema.multiline) {
    const isArray = Array.isArray(defaultValue);
    const text = isArray && Array.isArray(value)
      ? value.every((item) => typeof item === 'string' || typeof item === 'number')
        ? value.join('\n') : JSON.stringify(value, null, 2)
      : String(value ?? '');
    control = <>
      <textarea id={id} aria-describedby={describedBy} className="plugin-setting-textarea"
        rows={Math.min(Math.max(text.split('\n').length, 2), 6)} value={text} placeholder={schema.placeholder}
        onChange={(event) => onChange(isArray ? (event.target.value === '' ? [] : event.target.value.split('\n')) : event.target.value)} />
      {isArray && <span className="plugin-setting-row__hint">{translate("common:actions.onePerLine")}</span>}
    </>;
  } else {
    control = <input id={id} aria-describedby={describedBy} type={schema.secret ? 'password' : 'text'} className="plugin-setting-input"
      value={String(value ?? '')} placeholder={schema.placeholder} onChange={(event) => onChange(event.target.value)} />;
  }
  return (
    <div className="plugin-setting-row">
      <label htmlFor={id} className="plugin-setting-row__label">{label}</label>
      <div className="plugin-setting-row__control">
        {control}
        {schema.description && <span id={hintId} className="plugin-setting-row__hint">{schema.description}</span>}
      </div>
    </div>
  );
}

function ObjectSettingInput({ id, describedBy, value, onChange }: {
  id: string; describedBy?: string; value: unknown; onChange: (value: unknown) => void;
}) {
  useUiLanguage();
  const serialized = JSON.stringify(value ?? {}, null, 2);
  const [draft, setDraft] = useState(serialized);
  const [error, setError] = useState(false);
  useEffect(() => { setDraft(serialized); setError(false); }, [serialized]);
  return <>
    <textarea id={id} className="plugin-setting-textarea" rows={5} value={draft}
      aria-invalid={error} aria-describedby={[describedBy, error ? `${id}-error` : null].filter(Boolean).join(' ')}
      onChange={(event) => {
        const text = event.target.value;
        setDraft(text);
        try {
          const parsed = JSON.parse(text);
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
          setError(false);
          onChange(parsed);
        } catch { setError(true); }
      }} />
    {error && <span id={`${id}-error`} role="alert" className="plugin-setting-row__hint">{translate("plugins:pluginSettingsEditor.objectSettingInput_message_enterJSONCurrentEditNotKeepSettings")}</span>}
  </>;
}
