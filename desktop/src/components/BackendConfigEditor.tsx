import { t as translate, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useRef, useState } from 'react';
import { CustomSelect } from './CustomSelect';
import { Icon } from './Icon';
import { fetchOpenAIModels } from '../lib/api';
import { message, resolveMessage, type LocalizedText } from '../i18n/core';
import { normalizeError } from '../lib/errors';

type TokenEntry = {
  token: string;
  endpoint: string;
  modelName: string;
  stream?: boolean;
  /** 模型上下文窗口（token）：数字，或后端能解析的写法（如 "128k"）；留空表示用默认窗口 */
  contextWindow?: number | string;
};

/**
 * 留空时的上下文窗口，与后端 DEFAULT_CONTEXT_WINDOW（GalTransl/Agent/runtime.py）保持一致。
 * 这里只是把"留空等于多少"显示给人看，真正的默认值由后端兜底。
 */
const DEFAULT_CONTEXT_WINDOW = 128000;

/**
 * 「上下文大小」输入 → 配置值。
 *
 * 纯数字写成 number（配置文件里就是 `contextWindow: 200000`）；`128k` 这类带单位的写法原样交给
 * 后端解析（Agent 侧 _parse_context_window 认 k 后缀）；清空则删掉这个键，让后端用默认窗口。
 */
function parseContextWindowInput(raw: string): number | string | undefined {
  const text = raw.trim();
  if (!text) return undefined;
  return /^\d+$/.test(text) ? Number(text) : text;
}

type BackendConfigEditorProps = {
  config: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
  readOnly?: boolean;
  /** Optional proxy config (e.g., project-level proxy) to use when fetching the model list. */
  proxy?: { http?: string; https?: string } | null;
};

export function BackendConfigEditor({ config, onChange, readOnly = false, proxy = null }: BackendConfigEditorProps) {
  useUiLanguage();
  const hasOai = 'OpenAI-Compatible' in config;
  const hasSakura = 'SakuraLLM' in config;

  const oaiConfig = (config?.['OpenAI-Compatible'] || {}) as Record<string, unknown>;
  const sakuraConfig = (config?.['SakuraLLM'] || {}) as Record<string, unknown>;

  const tokens = (Array.isArray(oaiConfig.tokens) ? oaiConfig.tokens : []) as TokenEntry[];

  // Toggle backend type presence
  const toggleBackendType = useCallback((type: 'OpenAI-Compatible' | 'SakuraLLM', enabled: boolean) => {
    if (readOnly) return;
    if (enabled) {
      if (type === 'OpenAI-Compatible') {
        onChange({
          ...config,
          'OpenAI-Compatible': {
            // 勾选后端类型时自动预置一条令牌，相当于替用户点了一次「添加令牌」
            tokens: [{ token: '', endpoint: '', modelName: '', contextWindow: DEFAULT_CONTEXT_WINDOW }],
            tokenStrategy: 'random',
            checkAvailable: true,
            globalRequestRPM: 0,
            apiTimeout: 300,
            apiErrorWait: 'auto',
          },
        });
      } else {
        onChange({
          ...config,
          SakuraLLM: {
            // 勾选后端类型时自动预置一个端点，相当于替用户点了一次「添加端点」
            endpoints: [''],
            rewriteModelName: '',
          },
        });
      }
    } else {
      const next = { ...config };
      delete next[type];
      onChange(next);
    }
  }, [config, onChange, readOnly]);

  // Update a single key in OpenAI-Compatible section
  const updateOai = useCallback((key: string, value: unknown) => {
    if (readOnly) return;
    onChange({ ...config, 'OpenAI-Compatible': { ...oaiConfig, [key]: value } });
  }, [config, oaiConfig, onChange, readOnly]);

  // Update a single key in SakuraLLM section
  const updateSakura = useCallback((key: string, value: unknown) => {
    if (readOnly) return;
    onChange({ ...config, SakuraLLM: { ...sakuraConfig, [key]: value } });
  }, [config, sakuraConfig, onChange, readOnly]);

  const sakuraEndpoints = (Array.isArray(sakuraConfig.endpoints)
    ? sakuraConfig.endpoints
    : [String(sakuraConfig.endpoints ?? sakuraConfig.endpoint ?? '')]) as string[];

  const updateSakuraEndpoint = useCallback((index: number, val: string) => {
    if (readOnly) return;
    const next = [...sakuraEndpoints];
    next[index] = val;
    updateSakura('endpoints', next);
  }, [sakuraEndpoints, updateSakura, readOnly]);

  const addSakuraEndpoint = useCallback(() => {
    if (readOnly) return;
    const next = [...sakuraEndpoints, ''];
    updateSakura('endpoints', next);
  }, [sakuraEndpoints, updateSakura, readOnly]);

  const removeSakuraEndpoint = useCallback((index: number) => {
    if (readOnly) return;
    const next = sakuraEndpoints.filter((_, i) => i !== index);
    updateSakura('endpoints', next);
  }, [sakuraEndpoints, updateSakura, readOnly]);

  // Tokens list operations
  const addToken = useCallback(() => {
    if (readOnly) return;
    // 新令牌带上默认上下文窗口（128000），免得留空让人以为没生效
    const next = [...tokens, { token: '', endpoint: '', modelName: '', contextWindow: DEFAULT_CONTEXT_WINDOW }];
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  const removeToken = useCallback((index: number) => {
    if (readOnly) return;
    const next = tokens.filter((_, i) => i !== index);
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  const updateToken = useCallback((index: number, field: keyof TokenEntry, value: string | number | boolean | undefined) => {
    if (readOnly) return;
    const next = tokens.map((t, i) => i === index ? { ...t, [field]: value } : t);
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  // Per-token model-list fetch state
  type ModelsState = { loading: boolean; error: LocalizedText | null; models: string[] };
  const [modelsState, setModelsState] = useState<Record<number, ModelsState>>({});
  // Which token's model dropdown is currently open (null = none)
  const [openDropdownIdx, setOpenDropdownIdx] = useState<number | null>(null);
  const dropdownContainerRef = useRef<HTMLDivElement | null>(null);

  // Close dropdown on outside click
  useEffect(() => {
    if (openDropdownIdx == null) return;
    const handler = (e: MouseEvent) => {
      if (dropdownContainerRef.current && !dropdownContainerRef.current.contains(e.target as Node)) {
        setOpenDropdownIdx(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [openDropdownIdx]);

  const handleFetchModels = useCallback(async (index: number) => {
    const entry = tokens[index];
    if (!entry) return;
    setModelsState((prev) => ({
      ...prev,
      [index]: { loading: true, error: null, models: prev[index]?.models ?? [] },
    }));
    try {
      const res = await fetchOpenAIModels({
        endpoint: entry.endpoint || '',
        token: entry.token || '',
        proxy: proxy && (proxy.http || proxy.https) ? proxy : null,
      });
      setModelsState((prev) => ({
        ...prev,
        [index]: { loading: false, error: null, models: res.models || [] },
      }));
      if ((res.models?.length ?? 0) > 0) {
        setOpenDropdownIdx(index);
      }
    } catch (err) {
      const msg = normalizeError(err, message("common:backendConfigEditor.msg_message_failed"));
      setModelsState((prev) => ({
        ...prev,
        [index]: { loading: false, error: msg, models: prev[index]?.models ?? [] },
      }));
    }
  }, [tokens, proxy]);

  return (
    <>
      {/* Backend type selector */}
      <label className="field">
        <span>{translate("common:backendConfigEditor.field_message_backend")}</span>
        <div className="backend-type-toggle">
          <label className="toggle-checkbox">
            <input
              type="checkbox"
              disabled={readOnly}
              checked={hasOai}
              onChange={(e) => toggleBackendType('OpenAI-Compatible', e.target.checked)}
            />
            <span>{translate("common:backendConfigEditor.toggleCheckbox_message_openAI")}</span>
          </label>
          <label className="toggle-checkbox">
            <input
              type="checkbox"
              disabled={readOnly}
              checked={hasSakura}
              onChange={(e) => toggleBackendType('SakuraLLM', e.target.checked)}
            />
            <span>{translate("common:backendConfigEditor.toggleCheckbox_message_sakuraModel")}</span>
          </label>
        </div>
      </label>

      {/* OpenAI-Compatible section */}
      {hasOai && (
        <>
          <h3 className="config-section-title">{translate("common:backendConfigEditor.backendConfigEditor_message_openAI")}</h3>

          {/* Tokens list */}
          <div className="token-list">
            <div className="token-list__header">
              <span className="token-list__title">{translate("common:backendConfigEditor.tokenListHeader_message_aPIToken")}</span>
              {!readOnly && (
                <button type="button" className="token-list__add-btn" onClick={addToken}>{translate("common:backendConfigEditor.tokenListHeader_message_addToken")}</button>
              )}
            </div>

            {tokens.length === 0 && (
              <div className="token-list__empty">{translate("common:backendConfigEditor.tokenList_message_emptyTokenAddTokenButtonAdd")}</div>
            )}

            {tokens.map((t, idx) => {
              const ms = modelsState[idx];
              return (
              <div key={idx} className="token-entry">
                <div className="token-entry__header">
                  <span className="token-entry__index">{translate("common:backendConfigEditor.tokenEntryHeader_message_token", { value: idx + 1 })}</span>
                  {!readOnly && (
                    <button
                      type="button"
                      className="token-entry__remove-btn"
                      onClick={() => removeToken(idx)}
                      title={translate("common:backendConfigEditor.tokenEntryRemoveBtn_title_deleteToken")}
                    >
                      <Icon name="close" />
                    </button>
                  )}
                </div>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_aPIKey")}</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={t.token ?? ''}
                    onChange={(e) => updateToken(idx, 'token', e.target.value)}
                    placeholder={translate("common:backendConfigEditor.fieldFieldInline_placeholder_sk")}
                  />
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_baseURL")}</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={t.endpoint ?? ''}
                    onChange={(e) => updateToken(idx, 'endpoint', e.target.value)}
                    placeholder={translate("common:backendConfigEditor.fieldFieldInline_placeholder_http1270018080")}
                  />
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_modelName")}</span>
                  <div className="model-name-row">
                    <div
                      className={`model-name-combo${openDropdownIdx === idx ? ' model-name-combo--open' : ''}`}
                      ref={openDropdownIdx === idx ? dropdownContainerRef : undefined}
                    >
                      <input
                        type="text"
                        disabled={readOnly}
                        value={t.modelName ?? ''}
                        onChange={(e) => updateToken(idx, 'modelName', e.target.value)}
                        placeholder={translate("common:backendConfigEditor.modelNameRow_placeholder_gpt4oMini")}
                        className="model-name-combo__input"
                        onFocus={() => {
                          if (ms && ms.models.length > 0) setOpenDropdownIdx(idx);
                        }}
                      />
                      {ms && ms.models.length > 0 && (
                        <button
                          type="button"
                          className="model-name-combo__arrow"
                          onClick={() => setOpenDropdownIdx((cur) => (cur === idx ? null : idx))}
                          aria-label={translate("common:backendConfigEditor.modelNameComboArrow_ariaLabel_model")}
                          aria-expanded={openDropdownIdx === idx}
                          tabIndex={-1}
                        >
                          <svg width="12" height="8" viewBox="0 0 12 8" fill="none" aria-hidden="true">
                            <path d="M1.5 1.5L6 6l4.5-4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </button>
                      )}
                      {openDropdownIdx === idx && ms && ms.models.length > 0 && (
                        <div className="custom-select__panel model-name-combo__panel" role="listbox">
                          {ms.models.map((m) => (
                            <div
                              key={m}
                              role="option"
                              aria-selected={t.modelName === m}
                              className={`custom-select__option${t.modelName === m ? ' custom-select__option--selected' : ''}`}
                              onMouseDown={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                updateToken(idx, 'modelName', m);
                                setOpenDropdownIdx(null);
                              }}
                            >
                              {m}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                    {!readOnly && (
                      <button
                        type="button"
                        className="model-name-row__fetch-btn"
                        onClick={() => handleFetchModels(idx)}
                        disabled={modelsState[idx]?.loading}
                        title={translate("common:backendConfigEditor.modelNameRowFetchBtn_title_currentAPIKeyBaseURLModel")}
                      >
                        {modelsState[idx]?.loading ? translate("common:backendConfigEditor.modelNameRowFetchBtn_message_text") : translate("common:backendConfigEditor.modelNameRowFetchBtn_message_model")}
                      </button>
                    )}
                  </div>
                  {ms?.error && (
                    <span className="field__hint field__hint--error">{translate("common:backendConfigEditor.fieldFieldInline_message_failed", { error: resolveMessage(ms.error) })}</span>
                  )}
                  {ms && !ms.error && ms.models.length > 0 && (
                    <span className="field__hint">{translate("common:backendConfigEditor.fieldFieldInline_message_doneCountModelSelect", { count: ms.models.length })}</span>
                  )}
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_text")}</span>
                  <CustomSelect
                    disabled={readOnly}
                    value={t.stream == null ? '' : String(t.stream)}
                    onChange={(e) => {
                      if (e.target.value === '') updateToken(idx, 'stream', undefined);
                      else updateToken(idx, 'stream', e.target.value === 'true');
                    }}
                  >
                    <option value="">{translate("common:backendConfigEditor.fieldFieldInline_message_settings")}</option>
                    <option value="true">{translate("common:backendConfigEditor.fieldFieldInline_message_textVariant2")}</option>
                    <option value="false">{translate("common:backendConfigEditor.fieldFieldInline_message_textVariant3")}</option>
                  </CustomSelect>
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_context")}</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    disabled={readOnly}
                    value={t.contextWindow == null ? '' : String(t.contextWindow)}
                    onChange={(e) => updateToken(idx, 'contextWindow', parseContextWindowInput(e.target.value))}
                    placeholder={translate("common:backendConfigEditor.fieldFieldInline_placeholder_default", { DEFAULT_CONTEXT_WINDOW: DEFAULT_CONTEXT_WINDOW })}
                  />
                  <span className="field__hint">{translate("common:backendConfigEditor.fieldFieldInline_message_modelContextTokenAgentContextUsageEmpty", { DEFAULT_CONTEXT_WINDOW: DEFAULT_CONTEXT_WINDOW })}</span>
                </label>
              </div>
              );
            })}
          </div>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_token")}</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.tokenStrategy ?? 'random')}
              onChange={(e) => updateOai('tokenStrategy', e.target.value)}
            >
              <option value="random">{translate("common:backendConfigEditor.field_message_text")}</option>
              <option value="fallback">{translate("common:backendConfigEditor.field_message_textVariant2")}</option>
            </CustomSelect>
            <span className="field__hint">{translate("common:backendConfigEditor.field_message_randomFallbackCountCount")}</span>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_textVariant3")}</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.stream ?? true)}
              onChange={(e) => updateOai('stream', e.target.value === 'true')}
            >
              <option value="true">{translate("common:backendConfigEditor.field_message_default")}</option>
              <option value="false">{translate("common:actions.close")}</option>
            </CustomSelect>
            <span className="field__hint">{translate("common:backendConfigEditor.field_message_defaultSentenceTranslationTextFileProgressTranslationDisable")}</span>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_model")}</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.checkAvailable ?? 'true')}
              onChange={(e) => updateOai('checkAvailable', e.target.value === 'true')}
            >
              <option value="true">{translate("common:backendConfigEditor.field_message_textVariant4")}</option>
              <option value="false">{translate("common:backendConfigEditor.field_message_textVariant5")}</option>
            </CustomSelect>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_seconds")}</span>
            <input
              disabled={readOnly}
              type="number"
              value={String(oaiConfig.apiTimeout ?? 300)}
              onChange={(e) => updateOai('apiTimeout', Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_rPM")}</span>
            <input
              disabled={readOnly}
              type="number"
              min={0}
              value={String(oaiConfig.globalRequestRPM ?? 0)}
              onChange={(e) => updateOai('globalRequestRPM', Number(e.target.value))}
            />
            <span className="field__hint">{translate("common:backendConfigEditor.field_message_0Job")}</span>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_aPIErrorWait")}</span>
            <input
              disabled={readOnly}
              type="text"
              value={String(oaiConfig.apiErrorWait ?? 'auto')}
              onChange={(e) => updateOai('apiErrorWait', e.target.value)}
            />
            <span className="field__hint">{translate("common:backendConfigEditor.field_message_auto0120Seconds")}</span>
          </label>
        </>
      )}

      {/* SakuraLLM section */}
      {hasSakura && (
        <>
          <h3 className="config-section-title" style={{ marginTop: hasOai ? '24px' : undefined }}>{translate("common:backendConfigEditor.backendConfigEditor_message_sakuraModel")}</h3>

          <div className="token-list">
            <div className="token-list__header">
              <span className="token-list__title">{translate("common:backendConfigEditor.tokenListHeader_message_text")}</span>
              {!readOnly && (
                <button type="button" className="token-list__add-btn" onClick={addSakuraEndpoint}>{translate("common:backendConfigEditor.tokenListHeader_message_add")}</button>
              )}
            </div>

            {sakuraEndpoints.length === 0 && (
              <div className="token-list__empty">{translate("common:backendConfigEditor.tokenList_message_emptyAddButtonAdd")}</div>
            )}

            {sakuraEndpoints.map((ep, idx) => (
              <div key={idx} className="token-entry" style={{ marginBottom: '12px' }}>
                <div className="token-entry__header">
                  <span className="token-entry__index">{translate("common:backendConfigEditor.tokenEntryHeader_message_text", { value: idx + 1 })}</span>
                  {!readOnly && (
                    <button
                      type="button"
                      className="token-entry__remove-btn"
                      onClick={() => removeSakuraEndpoint(idx)}
                      title={translate("common:backendConfigEditor.tokenEntryRemoveBtn_title_delete")}
                    >
                      <Icon name="close" />
                    </button>
                  )}
                </div>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_address")}</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={ep}
                    onChange={(e) => updateSakuraEndpoint(idx, e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        addSakuraEndpoint();
                      }
                    }}
                    placeholder={translate("common:backendConfigEditor.fieldFieldInline_placeholder_http1270018501")}
                  />
                </label>
              </div>
            ))}
          </div>

          <label className="field" style={{ marginTop: '12px' }}>
            <span>{translate("common:backendConfigEditor.field_message_customModelName")}</span>
            <input
              disabled={readOnly}
              type="text"
              value={String(sakuraConfig.rewriteModelName ?? '')}
              onChange={(e) => updateSakura('rewriteModelName', e.target.value)}
            />
            <span className="field__hint">{translate("common:backendConfigEditor.field_message_ollamaChangeItem")}</span>
          </label>
        </>
      )}
    </>
  );
}
