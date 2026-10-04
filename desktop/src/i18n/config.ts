import { t, type TranslationKey } from './core';

export type ConfigOption = string | { value: string; labelKey: TranslationKey };

export function resolveConfigOption(option: ConfigOption): { value: string; label: string } {
  return typeof option === 'string' ? { value: option, label: option } : { value: option.value, label: t(option.labelKey) };
}
