import { t } from '../../i18n';
import type labels from '../../i18n/locales/zh-CN/extraction.json';
export type Label = keyof typeof labels;
export const x = (key: Label, values?: Record<string, unknown>) => t(`extraction:${key}`, values);
export const statusLabel = (status: string) => x((['idle', 'starting', 'running', 'awaiting_permission', 'awaiting_input', 'stopping', 'stopped', 'failed', 'interrupted'].includes(status) ? status : 'idle') as Label);
