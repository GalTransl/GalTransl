import { createInstance, type Resource } from 'i18next';
import { resources, type TranslationKey } from './resources';

export const UI_LANGUAGES = ['zh-CN', 'en'] as const;
export type UiLanguage = (typeof UI_LANGUAGES)[number];
export const UI_LANGUAGE_STORAGE_KEY = 'galtransl.ui.language';
export type MessageValues = Record<string, unknown>;
export type UiMessage = { key: TranslationKey; values?: MessageValues };
export type LocalizedText = string | UiMessage;

export function normalizeUiLanguage(value: unknown): UiLanguage {
  return value === 'en' ? 'en' : 'zh-CN';
}

type Environment = {
  storage?: Pick<Storage, 'getItem' | 'setItem'>;
  document?: Pick<Document, 'documentElement'>;
  resources?: Resource;
};

export function createUiI18n(environment: Environment = {}) {
  let preferred: UiLanguage = 'zh-CN';
  try {
    preferred = normalizeUiLanguage(environment.storage?.getItem(UI_LANGUAGE_STORAGE_KEY));
  } catch { /* Storage can be unavailable in private browsing. */ }

  const instance = createInstance();
  void instance.init({
    resources: environment.resources ?? resources,
    lng: preferred,
    fallbackLng: 'zh-CN',
    supportedLngs: [...UI_LANGUAGES],
    load: 'currentOnly',
    defaultNS: 'common',
    returnEmptyString: false,
    returnNull: false,
    initAsync: false,
    interpolation: { escapeValue: false },
  });

  const language = () => normalizeUiLanguage(instance.language);
  const updateDocument = () => {
    if (environment.document) environment.document.documentElement.lang = language();
  };
  instance.on('languageChanged', updateDocument);
  updateDocument();

  const translate = (key: TranslationKey, values?: MessageValues): string => {
    const resolved = values && Object.fromEntries(Object.entries(values).map(([name, value]) =>
      [name, isUiMessage(value) ? translate(value.key, value.values) : value]));
    return instance.t(key, resolved) as string;
  };
  const resolve = (value: LocalizedText | null | undefined): string =>
    isUiMessage(value) ? translate(value.key, value.values) : value ?? '';

  return {
    instance,
    language,
    translate,
    resolve,
    async changeLanguage(value: unknown) {
      const next = normalizeUiLanguage(value);
      try { environment.storage?.setItem(UI_LANGUAGE_STORAGE_KEY, next); } catch { /* The current session can still switch. */ }
      await instance.changeLanguage(next);
    },
    subscribe(listener: () => void) {
      instance.on('languageChanged', listener);
      return () => { instance.off('languageChanged', listener); };
    },
  };
}

export function isUiMessage(value: unknown): value is UiMessage {
  return !!value && typeof value === 'object' && 'key' in value && typeof value.key === 'string';
}

let storage: Storage | undefined;
try { storage = globalThis.localStorage; } catch { /* Access itself can throw. */ }
export const uiI18n = createUiI18n({ storage, document: typeof document === 'undefined' ? undefined : document });
export const i18n = uiI18n.instance;
export const t = uiI18n.translate;
export const resolveMessage = uiI18n.resolve;
export const getUiLanguage = uiI18n.language;
export const setUiLanguage = uiI18n.changeLanguage;
export const subscribeUiLanguage = uiI18n.subscribe;
export const message = (key: TranslationKey, values?: MessageValues): UiMessage => ({ key, values });

export class UiError extends Error {
  constructor(public readonly uiMessage: UiMessage) {
    super(resolveMessage(uiMessage));
    this.name = 'UiError';
  }
}

export type { TranslationKey } from './resources';
