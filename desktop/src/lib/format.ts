import { getUiLanguage, t as translate, type UiLanguage } from "../i18n/core";
import type { Job } from './api';

const formatters = new Map<UiLanguage, Intl.DateTimeFormat>();
function getFormatter() {
  const language = getUiLanguage();
  let formatter = formatters.get(language);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(language, {
      day: '2-digit', hour: '2-digit', minute: '2-digit', month: '2-digit', second: '2-digit', year: 'numeric',
    });
    formatters.set(language, formatter);
  }
  return formatter;
}

export function formatTimestamp(value: string) {
  if (!value) {
    return '—';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return getFormatter().format(parsed);
}

export function formatJobResult(job: Job) {
  if (job.status === 'completed' && job.success) {
    return translate("common:format.formatJobResult_message_completedSuccessfully");
  }

  if (job.status === 'failed') {
    return translate("common:format.formatJobResult_message_failed");
  }

  if (job.status === 'cancelled') {
    return translate("common:format.formatJobResult_message_cancelled");
  }

  return translate("common:format.formatJobResult_message_inProgress");
}
