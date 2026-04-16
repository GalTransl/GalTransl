import { useCallback } from 'react';
import { CustomSelect } from './CustomSelect';

type TokenEntry = {
  token: string;
  endpoint: string;
  modelName: string;
  stream?: boolean;
};

type BackendConfigEditorProps = {
  config: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
  readOnly?: boolean;
};

export function BackendConfigEditor({ config, onChange, readOnly = false }: BackendConfigEditorProps) {
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
            tokens: [],
            tokenStrategy: 'random',
            checkAvailable: true,
            globalRequestRPM: 0,
            apiTimeout: 120,
            apiErrorWait: 'auto',
          },
        });
      } else {
        onChange({
          ...config,
          SakuraLLM: {
            endpoints: [],
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

  // Tokens list operations
  const addToken = useCallback(() => {
    if (readOnly) return;
    const next = [...tokens, { token: '', endpoint: '', modelName: '' }];
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  const removeToken = useCallback((index: number) => {
    if (readOnly) return;
    const next = tokens.filter((_, i) => i !== index);
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  const updateToken = useCallback((index: number, field: keyof TokenEntry, value: string | boolean) => {
    if (readOnly) return;
    const next = tokens.map((t, i) => i === index ? { ...t, [field]: value } : t);
    updateOai('tokens', next);
  }, [tokens, updateOai, readOnly]);

  return (
    <>
      {/* Backend type selector */}
      <label className="field">
        <span>后端类型</span>
        <div className="backend-type-toggle">
          <label className="toggle-checkbox">
            <input
              type="checkbox"
              disabled={readOnly}
              checked={hasOai}
              onChange={(e) => toggleBackendType('OpenAI-Compatible', e.target.checked)}
            />
            <span>OpenAI 兼容接口</span>
          </label>
          <label className="toggle-checkbox">
            <input
              type="checkbox"
              disabled={readOnly}
              checked={hasSakura}
              onChange={(e) => toggleBackendType('SakuraLLM', e.target.checked)}
            />
            <span>Sakura 本地模型</span>
          </label>
        </div>
      </label>

      {/* OpenAI-Compatible section */}
      {hasOai && (
        <>
          <h3 className="config-section-title">OpenAI 兼容接口</h3>

          {/* Tokens list */}
          <div className="token-list">
            <div className="token-list__header">
              <span className="token-list__title">API 令牌列表</span>
              {!readOnly && (
                <button type="button" className="token-list__add-btn" onClick={addToken}>
                  + 添加令牌
                </button>
              )}
            </div>

            {tokens.length === 0 && (
              <div className="token-list__empty">
                暂无令牌，请点击「添加令牌」按钮添加。
              </div>
            )}

            {tokens.map((t, idx) => (
              <div key={idx} className="token-entry">
                <div className="token-entry__header">
                  <span className="token-entry__index">令牌 #{idx + 1}</span>
                  {!readOnly && (
                    <button
                      type="button"
                      className="token-entry__remove-btn"
                      onClick={() => removeToken(idx)}
                      title="删除此令牌"
                    >
                      ✕
                    </button>
                  )}
                </div>
                <label className="field field--inline">
                  <span>API Key</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={t.token ?? ''}
                    onChange={(e) => updateToken(idx, 'token', e.target.value)}
                    placeholder="sk-..."
                  />
                </label>
                <label className="field field--inline">
                  <span>Base URL</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={t.endpoint ?? ''}
                    onChange={(e) => updateToken(idx, 'endpoint', e.target.value)}
                    placeholder="http://127.0.0.1:8080"
                  />
                </label>
                <label className="field field--inline">
                  <span>模型名称</span>
                  <input
                    type="text"
                    disabled={readOnly}
                    value={t.modelName ?? ''}
                    onChange={(e) => updateToken(idx, 'modelName', e.target.value)}
                    placeholder="gpt-4o-mini"
                  />
                </label>
                <label className="field field--inline">
                  <span>流式请求</span>
                  <CustomSelect
                    disabled={readOnly}
                    value={t.stream == null ? '' : String(t.stream)}
                    onChange={(e) => {
                      if (e.target.value === '') updateToken(idx, 'stream', undefined as unknown as boolean);
                      else updateToken(idx, 'stream', e.target.value === 'true');
                    }}
                  >
                    <option value="">默认</option>
                    <option value="true">是</option>
                    <option value="false">否</option>
                  </CustomSelect>
                </label>
              </div>
            ))}
          </div>

          <label className="field">
            <span>令牌策略</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.tokenStrategy ?? 'random')}
              onChange={(e) => updateOai('tokenStrategy', e.target.value)}
            >
              <option value="random">随机轮询</option>
              <option value="fallback">优先降级</option>
            </CustomSelect>
            <span className="field__hint">random 随机轮询；fallback 优先第一个，出错时使用下一个</span>
          </label>
          <label className="field">
            <span>检查可用性</span>
            <CustomSelect
              disabled={readOnly}
              value={String(oaiConfig.checkAvailable ?? 'true')}
              onChange={(e) => updateOai('checkAvailable', e.target.value === 'true')}
            >
              <option value="true">是</option>
              <option value="false">否</option>
            </CustomSelect>
          </label>
          <label className="field">
            <span>请求超时(秒)</span>
            <input
              disabled={readOnly}
              type="number"
              value={String(oaiConfig.apiTimeout ?? 120)}
              onChange={(e) => updateOai('apiTimeout', Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span>全局请求限速(RPM)</span>
            <input
              disabled={readOnly}
              type="number"
              min={0}
              value={String(oaiConfig.globalRequestRPM ?? 0)}
              onChange={(e) => updateOai('globalRequestRPM', Number(e.target.value))}
            />
            <span className="field__hint">0 表示不限制；该限制在多任务间全局共享</span>
          </label>
          <label className="field">
            <span>API错误等待</span>
            <input
              disabled={readOnly}
              type="text"
              value={String(oaiConfig.apiErrorWait ?? 'auto')}
              onChange={(e) => updateOai('apiErrorWait', e.target.value)}
            />
            <span className="field__hint">auto 或 0-120秒</span>
          </label>
        </>
      )}

      {/* SakuraLLM section */}
      {hasSakura && (
        <>
          <h3 className="config-section-title" style={{ marginTop: hasOai ? '24px' : undefined }}>Sakura 本地模型</h3>
          <label className="field">
            <span>端点列表</span>
            <textarea
              disabled={readOnly}
              rows={3}
              value={Array.isArray(sakuraConfig.endpoints) ? (sakuraConfig.endpoints as string[]).join('\n') : String(sakuraConfig.endpoints ?? '')}
              onChange={(e) => {
                const lines = e.target.value.split('\n').filter(Boolean);
                updateSakura('endpoints', lines);
              }}
            />
            <span className="field__hint">每行一个端点地址</span>
          </label>
          <label className="field">
            <span>自定义模型名称</span>
            <input
              disabled={readOnly}
              type="text"
              value={String(sakuraConfig.rewriteModelName ?? '')}
              onChange={(e) => updateSakura('rewriteModelName', e.target.value)}
            />
            <span className="field__hint">使用 ollama 时需修改此项</span>
          </label>
        </>
      )}
    </>
  );
}
