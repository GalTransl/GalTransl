import { ApiError } from './api';
import { UiError, resolveMessage, type LocalizedText } from '../i18n/core';

export function normalizeError(error: unknown, fallback: string): string;
export function normalizeError(error: unknown, fallback: LocalizedText): LocalizedText;
export function normalizeError(error: unknown, fallback: LocalizedText): LocalizedText {
  if (error instanceof UiError) return typeof fallback === 'string' ? resolveMessage(error.uiMessage) : error.uiMessage;
  if (error instanceof ApiError) {
    return error.uiMessage ? (typeof fallback === 'string' ? resolveMessage(error.uiMessage) : error.uiMessage) : error.message;
  }

  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }

  return fallback;
}
