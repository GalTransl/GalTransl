import { useEffect, useState, useSyncExternalStore, type Dispatch, type ReactElement, type SetStateAction } from 'react';
import { I18nextProvider, Trans } from 'react-i18next';
import { getUiLanguage, i18n, normalizeUiLanguage, resolveMessage, setUiLanguage, subscribeUiLanguage, UI_LANGUAGE_STORAGE_KEY, type LocalizedText, type MessageValues, type TranslationKey } from './core';

export function useUiLanguage() {
  return useSyncExternalStore(subscribeUiLanguage, getUiLanguage, () => 'zh-CN' as const);
}

export function UiI18nProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === UI_LANGUAGE_STORAGE_KEY || event.key === null) {
        void i18n.changeLanguage(normalizeUiLanguage(event.newValue));
      }
    };
    window.addEventListener('storage', sync);
    return () => window.removeEventListener('storage', sync);
  }, []);
  return <I18nextProvider i18n={i18n}>{children}</I18nextProvider>;
}

export function UiTrans({ k, values, components }: {
  k: TranslationKey;
  values?: MessageValues;
  components?: readonly ReactElement[];
}) {
  useUiLanguage();
  return <Trans i18n={i18n} i18nKey={k as string} values={values} components={components} />;
}

// Store references so existing feedback changes language without resetting forms.
export function useMessageState<T extends string | null = string | null>(
  initial: LocalizedText | null | (() => LocalizedText | null),
): [T, Dispatch<SetStateAction<LocalizedText | null>>] {
  useUiLanguage();
  const [value, setValue] = useState(initial);
  return [(value === null ? null : resolveMessage(value)) as T, setValue];
}

type Feedback = { type: 'success' | 'error' | 'info'; message: LocalizedText };
export function useFeedbackState(initial: null = null): [
  (Omit<Feedback, 'message'> & { message: string }) | null,
  Dispatch<SetStateAction<Feedback | null>>,
] {
  useUiLanguage();
  const [value, setValue] = useState<Feedback | null>(initial);
  return [value && { ...value, message: resolveMessage(value.message) }, setValue];
}

export { setUiLanguage };
