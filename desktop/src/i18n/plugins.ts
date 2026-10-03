import type { PluginInfo, PluginSettingSchema } from '../lib/api';
import pluginMap from './plugin-map.json';
import { t, type TranslationKey } from './core';

type PluginMapping = {
  name: string;
  nameKey: TranslationKey;
  descriptionKey: TranslationKey;
  settings: {
    key: string;
    labelKey?: TranslationKey;
    descriptionKey?: TranslationKey;
    placeholderKey?: TranslationKey;
    options: { value: string | number | boolean; labelKey: TranslationKey }[];
  }[];
};
const mappings = new Map((pluginMap as PluginMapping[]).map((plugin) => [plugin.name, plugin]));

export function pluginSettingDisplayValue(plugin: PluginInfo, key: string, value: unknown): string {
  const schema = localizePlugin(plugin).settings_schema?.[key];
  const format = (item: unknown): string => schema?.options?.find((option) => Object.is(option.value, item))?.label
    ?? (typeof item === 'boolean' ? t(item ? 'common:values.enabled' : 'common:values.disabled')
      : item === '' || item == null ? t('common:values.empty')
      : typeof item === 'object' ? JSON.stringify(item) : String(item));
  if (schema?.secret) return t(value ? 'common:values.set' : 'common:values.notSet');
  return Array.isArray(value) ? value.map(format).join('、') || t('common:values.notSelected') : format(value);
}

export function localizePlugin(plugin: PluginInfo): PluginInfo;
export function localizePlugin(plugin: PluginInfo | undefined): PluginInfo | undefined;
export function localizePlugin(plugin: PluginInfo | undefined): PluginInfo | undefined {
  if (!plugin) return plugin;
  const mapping = mappings.get(plugin.name);
  if (!mapping) return plugin;
  const settingsSchema = plugin.settings_schema && Object.fromEntries(Object.entries(plugin.settings_schema).map(([key, schema]) => {
    const setting = mapping.settings.find((item) => item.key === key);
    if (!setting) return [key, schema];
    const localized: PluginSettingSchema = {
      ...schema,
      label: setting.labelKey ? t(setting.labelKey) : schema.label,
      description: setting.descriptionKey ? t(setting.descriptionKey) : schema.description,
      placeholder: setting.placeholderKey ? t(setting.placeholderKey) : schema.placeholder,
      options: schema.options?.map((option) => {
        const match = setting.options.find((item) => Object.is(item.value, option.value));
        return match ? { ...option, label: t(match.labelKey) } : option;
      }),
    };
    return [key, localized];
  }));
  return {
    ...plugin,
    display_name: t(mapping.nameKey),
    description: t(mapping.descriptionKey),
    settings_schema: settingsSchema,
  };
}
