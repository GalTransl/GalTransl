import { useCallback } from 'react';
import type { PluginInfo } from '../lib/api';

/**
 * 通用插件设置编辑器组件。
 * 根据插件 YAML Settings 中每个键的值类型自动渲染对应的输入控件：
 * - boolean → checkbox 开关
 * - number  → 数字输入框
 * - string  → 文本输入框
 * - array   → 多行文本框（每行一项）
 */

interface PluginSettingsEditorProps {
  /** 插件信息（用于显示名称等） */
  plugin: PluginInfo;
  /** 项目配置中该插件的覆盖值 (config.plugin[pluginName]) */
  overrides: Record<string, unknown>;
  /** 设置变更回调 */
  onChange: (pluginName: string, key: string, value: unknown) => void;
}

/** 计算设置项的有效值：项目覆盖 > 插件默认 */
function getEffectiveValue(
  defaultValue: unknown,
  overrideValue: unknown | undefined,
): unknown {
  return overrideValue !== undefined ? overrideValue : defaultValue;
}

/** 判断值是否为"简单"数组（元素都是 string/number） */
function isSimpleArray(v: unknown): v is (string | number)[] {
  if (!Array.isArray(v)) return false;
  return v.every((item) => typeof item === 'string' || typeof item === 'number');
}

export function PluginSettingsEditor({ plugin, overrides, onChange }: PluginSettingsEditorProps) {
  const settings = plugin.settings || {};
  const settingKeys = Object.keys(settings);

  // 无设置项
  if (settingKeys.length === 0) {
    return <div className="plugin-settings-empty">此插件无可配置的设置项</div>;
  }

  return (
    <div className="plugin-settings-panel">
      <div className="plugin-settings-panel__title">
        {plugin.display_name} 设置
      </div>
      <div className="plugin-settings-panel__fields">
        {settingKeys.map((key) => {
          const defaultValue = settings[key];
          const overrideValue = overrides[key];
          const effectiveValue = getEffectiveValue(defaultValue, overrideValue);

          return (
            <PluginSettingRow
              key={key}
              settingKey={key}
              defaultValue={defaultValue}
              effectiveValue={effectiveValue}
              pluginName={plugin.name}
              onChange={onChange}
            />
          );
        })}
      </div>
    </div>
  );
}

// ── 单个设置行 ──

interface PluginSettingRowProps {
  settingKey: string;
  defaultValue: unknown;
  effectiveValue: unknown;
  pluginName: string;
  onChange: (pluginName: string, key: string, value: unknown) => void;
}

function PluginSettingRow({
  settingKey,
  defaultValue,
  effectiveValue,
  pluginName,
  onChange,
}: PluginSettingRowProps) {
  // 根据默认值类型决定渲染控件
  const valueType = getSettingType(defaultValue);

  const handleBooleanChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onChange(pluginName, settingKey, e.target.checked);
    },
    [pluginName, settingKey, onChange],
  );

  const handleNumberChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const raw = e.target.value;
      if (raw === '' || raw === '-') {
        onChange(pluginName, settingKey, raw);
      } else {
        const num = Number(raw);
        onChange(pluginName, settingKey, isNaN(num) ? raw : num);
      }
    },
    [pluginName, settingKey, onChange],
  );

  const handleStringChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onChange(pluginName, settingKey, e.target.value);
    },
    [pluginName, settingKey, onChange],
  );

  const handleArrayChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const lines = e.target.value.split('\n');
      onChange(pluginName, settingKey, lines);
    },
    [pluginName, settingKey, onChange],
  );

  // 布尔值 → 开关
  if (valueType === 'boolean') {
    return (
      <label className="plugin-setting-row plugin-setting-row--boolean">
        <span className="plugin-setting-row__label">{settingKey}</span>
        <div className="plugin-setting-row__control">
          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={Boolean(effectiveValue)}
              onChange={handleBooleanChange}
            />
            <span className="toggle-switch__slider" />
          </label>
        </div>
      </label>
    );
  }

  // 数字 → 数字输入框
  if (valueType === 'number') {
    return (
      <label className="plugin-setting-row">
        <span className="plugin-setting-row__label">{settingKey}</span>
        <div className="plugin-setting-row__control">
          <input
            type="number"
            value={effectiveValue == null ? '' : String(effectiveValue)}
            onChange={handleNumberChange}
            className="plugin-setting-input plugin-setting-input--number"
          />
        </div>
      </label>
    );
  }

  // 数组 → 多行文本框
  if (valueType === 'array') {
    const arrayValue = isSimpleArray(effectiveValue)
      ? (effectiveValue as (string | number)[]).join('\n')
      : Array.isArray(effectiveValue)
        ? JSON.stringify(effectiveValue, null, 2)
        : String(effectiveValue ?? '');
    return (
      <label className="plugin-setting-row plugin-setting-row--array">
        <span className="plugin-setting-row__label">{settingKey}</span>
        <div className="plugin-setting-row__control">
          <textarea
            rows={Math.min(Math.max((arrayValue.split('\n').length), 2), 6)}
            value={arrayValue}
            onChange={handleArrayChange}
            className="plugin-setting-textarea"
          />
          <span className="plugin-setting-row__hint">每行一项</span>
        </div>
      </label>
    );
  }

  // 字符串 → 文本输入框（默认）
  return (
    <label className="plugin-setting-row">
      <span className="plugin-setting-row__label">{settingKey}</span>
      <div className="plugin-setting-row__control">
        <input
          type="text"
          value={effectiveValue == null ? '' : String(effectiveValue)}
          onChange={handleStringChange}
          className="plugin-setting-input"
        />
      </div>
    </label>
  );
}

// ── 类型检测工具 ──

function getSettingType(defaultValue: unknown): 'boolean' | 'number' | 'array' | 'string' {
  if (typeof defaultValue === 'boolean') return 'boolean';
  if (typeof defaultValue === 'number') return 'number';
  if (Array.isArray(defaultValue)) return 'array';
  return 'string';
}
