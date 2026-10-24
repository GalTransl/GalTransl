import { t as translate, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useRef, useState } from 'react';
import { CustomSelect } from './CustomSelect';
import { Icon } from './Icon';
import { Switch } from './Switch';
import { fetchOpenAIModels } from '../lib/api';
import { message, resolveMessage, type LocalizedText } from '../i18n/core';
import { normalizeError } from '../lib/errors';
import { AnimatedCollapse } from './UiMotion';
import { useStableListKeys } from '../lib/useStableListKeys';

type TokenEntry = {
  token: string;
  endpoint: string;
  modelName: string;
  stream?: boolean;
  /** 思考参数的令牌级覆盖；留空跟随 OpenAI-Compatible 全局设置 */
  enableThinking?: boolean;
  thinkingType?: 'enabled' | 'disabled';
  reasoningEffort?: string;
  /** 模型上下文窗口（token）：数字，或后端能解析的写法（如 "128k"）；留空表示用默认窗口 */
  contextWindow?: number | string;
};

/**
 * 留空时的上下文窗口，与后端 DEFAULT_CONTEXT_WINDOW（GalTransl/Agent/runtime.py）保持一致。
 * 这里只是把"留空等于多少"显示给人看，真正的默认值由后端兜底。
 */
const DEFAULT_CONTEXT_WINDOW = 128000;

const REASONING_EFFORT_OPTIONS = [
  { value: 'none', label: 'common:backendConfigEditor.reasoningEffort_none' },
  { value: 'minimal', label: 'common:backendConfigEditor.reasoningEffort_minimal' },
  { value: 'low', label: 'common:backendConfigEditor.reasoningEffort_low' },
  { value: 'medium', label: 'common:backendConfigEditor.reasoningEffort_medium' },
  { value: 'high', label: 'common:backendConfigEditor.reasoningEffort_high' },
  { value: 'xhigh', label: 'common:backendConfigEditor.reasoningEffort_xhigh' },
  { value: 'max', label: 'common:backendConfigEditor.reasoningEffort_max' },
] as const;

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

  const tokenKeys = useStableListKeys(tokens);

  // 哪些令牌是展开的（只记下标，纯视图状态，不写进配置）。
  // 令牌多了整页会拉得很长，所以默认只展开第一张；「添加令牌」时只留新的那张展开。
  const [expandedTokens, setExpandedTokens] = useState<Set<number>>(
    () => new Set(tokens.length > 0 ? [0] : []),
  );

  // 取消勾选后端类型时把整段配置暂存在这里，重新勾选时原样还回去；
  // 删除只有点「保存」后才会真正落盘（取消/关闭弹窗时随组件卸载一起丢弃）。
  const removedSectionsRef = useRef<Record<string, Record<string, unknown>>>({});

  // Toggle backend type presence
  const toggleBackendType = useCallback((type: 'OpenAI-Compatible' | 'SakuraLLM', enabled: boolean) => {
    if (readOnly) return;
    if (enabled) {
      const removed = removedSectionsRef.current[type];
      if (removed) {
        delete removedSectionsRef.current[type];
        onChange({ ...config, [type]: removed });
        return;
      }
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
        // 预置的这条是全新的一张，展开它——否则刚勾上后端类型就只看到一张收起的卡片
        setExpandedTokens(new Set([0]));
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
      const removed = config[type];
      if (removed && typeof removed === 'object') {
        removedSectionsRef.current[type] = removed as Record<string, unknown>;
      }
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

  // Per-token model-list fetch state
  type ModelsState = { loading: boolean; error: LocalizedText | null; models: string[] };
  const [modelsState, setModelsState] = useState<Record<number, ModelsState>>({});
  // Which token's model dropdown is currently open (null = none)
  const [openDropdownIdx, setOpenDropdownIdx] = useState<number | null>(null);
  const dropdownContainerRef = useRef<HTMLDivElement | null>(null);

  // Tokens list operations
  const addToken = useCallback(() => {
    if (readOnly) return;
    // 新令牌带上默认上下文窗口（128000），免得留空让人以为没生效
    const next = [...tokens, { token: '', endpoint: '', modelName: '', contextWindow: DEFAULT_CONTEXT_WINDOW }];
    updateOai('tokens', next);
    // 只展开刚加的这张：连点几次「添加令牌」时，列表不会因为前面几张全张着而越拉越长
    setExpandedTokens(new Set([next.length - 1]));
    setOpenDropdownIdx(null);
  }, [tokens, updateOai, readOnly]);

  const removeToken = useCallback((index: number) => {
    if (readOnly) return;
    const next = tokens.filter((_, i) => i !== index);
    updateOai('tokens', next);
    // 折叠状态是按下标记的，删掉一条之后后面的下标整体前移，这里跟着挪；
    // 删掉的正好是展开的那张时，让相邻的一张接手展开，免得列表里一张细节都看不到。
    setExpandedTokens((prev) => {
      const adjusted = new Set<number>();
      for (const i of prev) {
        if (i === index) continue;
        adjusted.add(i > index ? i - 1 : i);
      }
      if (prev.has(index) && next.length > 0) adjusted.add(Math.min(index, next.length - 1));
      return adjusted;
    });
    // 模型下拉是按下标打开的，删一条之后下标已经对不上了，直接关掉
    setOpenDropdownIdx(null);
  }, [tokens, updateOai, readOnly]);

  const toggleTokenExpanded = useCallback((index: number) => {
    setExpandedTokens((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
    // 收起时输入框会被卸载，留着打开状态的话再展开会莫名其妙弹回一个模型下拉
    setOpenDropdownIdx((cur) => (cur === index ? null : cur));
  }, []);

  const updateToken = useCallback((index: number, field: keyof TokenEntry, value: string | number | boolean | undefined) => {
    if (readOnly) return;
    const next = tokens.map((t, i) => i === index ? { ...t, [field]: value } : t);
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

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
              const open = expandedTokens.has(idx);
              // 收起时头部这行就是这张卡片的全部可见信息：模型名 · 地址 · key 尾号，
              // 足够认出是哪一条；网址去掉协议前缀，不然一行放不下多长的地址。
              const summary = [
                (t.modelName ?? '').trim(),
                (t.endpoint ?? '').trim().replace(/^https?:\/\//i, '').replace(/\/+$/, ''),
                (t.token ?? '').trim() ? `…${(t.token ?? '').trim().slice(-4)}` : '',
              ].filter(Boolean).join(' · ');
              return (
              <div key={tokenKeys[idx]} className={`token-entry token-entry--animated${open ? '' : ' token-entry--collapsed'}`}>
                <div className="token-entry__header">
                  <button
                    type="button"
                    className="token-entry__toggle"
                    onClick={() => toggleTokenExpanded(idx)}
                    aria-expanded={open}
                    title={translate("common:backendConfigEditor.tokenEntryToggleBtn_title_collapseOrExpand")}
                  >
                    <Icon name="chevron-right" className="token-entry__chevron" />
                    <span className="token-entry__index">{translate("common:backendConfigEditor.tokenEntryHeader_message_token", { value: idx + 1 })}</span>
                    {!open && (
                      <span className="token-entry__summary">
                        {summary || translate("common:backendConfigEditor.tokenEntrySummary_message_notConfigured")}
                      </span>
                    )}
                  </button>
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
                <AnimatedCollapse open={open} className="token-entry__collapse">
                <div className="token-entry__body">
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
                  {/* 三态（跟随全局/是/否），开关表达不了「跟随全局」，这里保留下拉框 */}
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
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_enableThinking")}</span>
                  <CustomSelect
                    disabled={readOnly}
                    value={t.enableThinking == null ? '' : String(t.enableThinking)}
                    onChange={(e) => {
                      if (e.target.value === '') updateToken(idx, 'enableThinking', undefined);
                      else updateToken(idx, 'enableThinking', e.target.value === 'true');
                    }}
                  >
                    <option value="">{translate("common:backendConfigEditor.fieldFieldInline_message_settings")}</option>
                    <option value="true">{translate("common:values.enabled")}</option>
                    <option value="false">{translate("common:values.disabled")}</option>
                  </CustomSelect>
                  <span className="field__hint">{translate("common:backendConfigEditor.fieldFieldInline_hint_enableThinking")}</span>
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_thinkingType")}</span>
                  <CustomSelect
                    disabled={readOnly}
                    value={String(t.thinkingType ?? '')}
                    onChange={(e) => updateToken(idx, 'thinkingType', e.target.value ? e.target.value : undefined)}
                  >
                    <option value="">{translate("common:backendConfigEditor.fieldFieldInline_message_settings")}</option>
                    <option value="enabled">{translate("common:values.enabled")}</option>
                    <option value="disabled">{translate("common:values.disabled")}</option>
                  </CustomSelect>
                  <span className="field__hint">{translate("common:backendConfigEditor.fieldFieldInline_hint_thinkingType")}</span>
                </label>
                <label className="field field--inline">
                  <span>{translate("common:backendConfigEditor.fieldFieldInline_message_reasoningEffort")}</span>
                  <CustomSelect
                    disabled={readOnly}
                    value={String(t.reasoningEffort ?? '')}
                    onChange={(e) => updateToken(idx, 'reasoningEffort', e.target.value || undefined)}
                  >
                    <option value="">{translate("common:backendConfigEditor.fieldFieldInline_message_settings")}</option>
                    {REASONING_EFFORT_OPTIONS.map(({ value, label }) => (
                      <option key={value} value={value}>{translate(label)}</option>
                    ))}
                  </CustomSelect>
                  <span className="field__hint">{translate("common:backendConfigEditor.fieldFieldInline_hint_reasoningEffort")}</span>
                </label>
                </div>
                </AnimatedCollapse>
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
          <label className="field field--switch">
            <span>{translate("common:backendConfigEditor.field_message_textVariant3")}</span>
            <Switch
              checked={oaiConfig.stream !== false}
              disabled={readOnly}
              onChange={(next) => updateOai('stream', next)}
            />
            <span className="field__hint">
              {translate("common:backendConfigEditor.field_message_defaultSentenceTranslationTextFileProgressTranslationDisable")}
            </span>
          </label>
          <p className="field__hint">{translate("common:backendConfigEditor.reasoningSettings_hint")}</p>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_enableThinking")}</span>
            <CustomSelect
              disabled={readOnly}
              value={oaiConfig.enableThinking == null ? '' : String(oaiConfig.enableThinking)}
              onChange={(e) => updateOai('enableThinking', e.target.value === '' ? undefined : e.target.value === 'true')}
            >
              <option value="">{translate("common:backendConfigEditor.providerDefault")}</option>
              <option value="true">{translate("common:values.enabled")}</option>
              <option value="false">{translate("common:values.disabled")}</option>
            </CustomSelect>
            <span className="field__hint">{translate("common:backendConfigEditor.field_hint_enableThinking")}</span>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_thinkingType")}</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.thinkingType ?? '')}
              onChange={(e) => updateOai('thinkingType', e.target.value || undefined)}
            >
              <option value="">{translate("common:backendConfigEditor.providerDefault")}</option>
              <option value="enabled">{translate("common:values.enabled")}</option>
              <option value="disabled">{translate("common:values.disabled")}</option>
            </CustomSelect>
            <span className="field__hint">{translate("common:backendConfigEditor.field_hint_thinkingType")}</span>
          </label>
          <label className="field">
            <span>{translate("common:backendConfigEditor.field_message_reasoningEffort")}</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.reasoningEffort ?? '')}
              onChange={(e) => updateOai('reasoningEffort', e.target.value || undefined)}
            >
              <option value="">{translate("common:backendConfigEditor.providerDefault")}</option>
              {REASONING_EFFORT_OPTIONS.map(({ value, label }) => (
                <option key={value} value={value}>{translate(label)}</option>
              ))}
            </CustomSelect>
            <span className="field__hint">{translate("common:backendConfigEditor.field_hint_reasoningEffort")}</span>
          </label>
          <label className="field field--switch">
            <span>{translate("common:backendConfigEditor.field_message_model")}</span>
            <Switch
              checked={oaiConfig.checkAvailable !== false}
              disabled={readOnly}
              onChange={(next) => updateOai('checkAvailable', next)}
            />
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
