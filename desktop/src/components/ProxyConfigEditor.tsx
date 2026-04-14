import { useCallback } from 'react';

type ProxyEntry = {
  address: string;
  username?: string;
  password?: string;
};

type ProxyConfigEditorProps = {
  proxyConfig: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
  readOnly?: boolean;
};

export function ProxyConfigEditor({ proxyConfig, onChange, readOnly = false }: ProxyConfigEditorProps) {
  const enableProxy = proxyConfig.enableProxy === true;
  const proxies = (Array.isArray(proxyConfig.proxies) ? proxyConfig.proxies : []) as ProxyEntry[];

  const toggleEnableProxy = useCallback((enabled: boolean) => {
    if (readOnly) return;
    onChange({ ...proxyConfig, enableProxy: enabled });
  }, [proxyConfig, onChange, readOnly]);

  const addProxy = useCallback(() => {
    if (readOnly) return;
    onChange({ ...proxyConfig, proxies: [...proxies, { address: '' }] });
  }, [proxyConfig, proxies, onChange, readOnly]);

  const removeProxy = useCallback((index: number) => {
    if (readOnly) return;
    const next = proxies.filter((_, i) => i !== index);
    onChange({ ...proxyConfig, proxies: next });
  }, [proxyConfig, proxies, onChange, readOnly]);

  const updateProxy = useCallback((index: number, field: keyof ProxyEntry, value: string) => {
    if (readOnly) return;
    const next = proxies.map((p, i) => i === index ? { ...p, [field]: value } : p);
    onChange({ ...proxyConfig, proxies: next });
  }, [proxyConfig, proxies, onChange, readOnly]);

  return (
    <>
      <h3 className="config-section-title" style={{ marginTop: '24px' }}>代理设置</h3>

      <label className="field">
        <span>启用代理</span>
        <select
          disabled={readOnly}
          value={String(enableProxy)}
          onChange={(e) => toggleEnableProxy(e.target.value === 'true')}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
        <span className="field__hint">使用中转供应商时一般不用开代理</span>
      </label>

      {enableProxy && (
        <div className="token-list">
          <div className="token-list__header">
            <span className="token-list__title">代理列表</span>
            {!readOnly && (
              <button type="button" className="token-list__add-btn" onClick={addProxy}>
                + 添加代理
              </button>
            )}
          </div>

          {proxies.length === 0 && (
            <div className="token-list__empty">
              暂无代理，请点击「添加代理」按钮添加。
            </div>
          )}

          {proxies.map((p, idx) => (
            <div key={idx} className="token-entry">
              <div className="token-entry__header">
                <span className="token-entry__index">代理 #{idx + 1}</span>
                {!readOnly && (
                  <button
                    type="button"
                    className="token-entry__remove-btn"
                    onClick={() => removeProxy(idx)}
                    title="删除此代理"
                  >
                    ✕
                  </button>
                )}
              </div>
              <label className="field field--inline">
                <span>代理地址</span>
                <input
                  type="text"
                  disabled={readOnly}
                  value={p.address ?? ''}
                  onChange={(e) => updateProxy(idx, 'address', e.target.value)}
                  placeholder="http://127.0.0.1:7890"
                />
              </label>
              <label className="field field--inline">
                <span>用户名</span>
                <input
                  type="text"
                  disabled={readOnly}
                  value={p.username ?? ''}
                  onChange={(e) => updateProxy(idx, 'username', e.target.value)}
                  placeholder="可选"
                />
              </label>
              <label className="field field--inline">
                <span>密码</span>
                <input
                  type="password"
                  disabled={readOnly}
                  value={p.password ?? ''}
                  onChange={(e) => updateProxy(idx, 'password', e.target.value)}
                  placeholder="可选"
                />
              </label>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
