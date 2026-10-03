import { t as translate, useUiLanguage } from "../i18n";
import { useCallback } from 'react';
import { CustomSelect } from './CustomSelect';
import { Icon } from './Icon';

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
  useUiLanguage();
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
      <h3 className="config-section-title" style={{ marginTop: '24px' }}>{translate("common:proxyConfigEditor.proxyConfigEditor_message_proxySettings")}</h3>

      <label className="field">
        <span>{translate("common:proxyConfigEditor.field_message_enableProxy")}</span>
        <CustomSelect
          disabled={readOnly}
          value={String(enableProxy)}
          onChange={(e) => toggleEnableProxy(e.target.value === 'true')}
        >
          <option value="true">{translate("common:proxyConfigEditor.field_message_text")}</option>
          <option value="false">{translate("common:proxyConfigEditor.field_message_textVariant2")}</option>
        </CustomSelect>
        <span className="field__hint">{translate("common:proxyConfigEditor.field_message_proxy")}</span>
      </label>

      {enableProxy && (
        <div className="token-list">
          <div className="token-list__header">
            <span className="token-list__title">{translate("common:proxyConfigEditor.tokenListHeader_message_proxy")}</span>
            {!readOnly && (
              <button type="button" className="token-list__add-btn" onClick={addProxy}>{translate("common:proxyConfigEditor.tokenListHeader_message_addProxy")}</button>
            )}
          </div>

          {proxies.length === 0 && (
            <div className="token-list__empty">{translate("common:proxyConfigEditor.tokenList_message_emptyProxyAddProxyButtonAdd")}</div>
          )}

          {proxies.map((p, idx) => (
            <div key={idx} className="token-entry">
              <div className="token-entry__header">
                <span className="token-entry__index">{translate("common:proxyConfigEditor.tokenEntryHeader_message_proxy", { value: idx + 1 })}</span>
                {!readOnly && (
                  <button
                    type="button"
                    className="token-entry__remove-btn"
                    onClick={() => removeProxy(idx)}
                    title={translate("common:proxyConfigEditor.tokenEntryRemoveBtn_title_deleteProxy")}
                  >
                    <Icon name="close" />
                  </button>
                )}
              </div>
              <label className="field field--inline">
                <span>{translate("common:proxyConfigEditor.fieldFieldInline_message_proxyAddress")}</span>
                <input
                  type="text"
                  disabled={readOnly}
                  value={p.address ?? ''}
                  onChange={(e) => updateProxy(idx, 'address', e.target.value)}
                  placeholder={translate("common:proxyConfigEditor.fieldFieldInline_placeholder_http1270017890")}
                />
              </label>
              <label className="field field--inline">
                <span>{translate("common:proxyConfigEditor.fieldFieldInline_message_text")}</span>
                <input
                  type="text"
                  disabled={readOnly}
                  value={p.username ?? ''}
                  onChange={(e) => updateProxy(idx, 'username', e.target.value)}
                  placeholder={translate("common:proxyConfigEditor.fieldFieldInline_placeholder_text")}
                />
              </label>
              <label className="field field--inline">
                <span>{translate("common:proxyConfigEditor.fieldFieldInline_message_password")}</span>
                <input
                  type="password"
                  disabled={readOnly}
                  value={p.password ?? ''}
                  onChange={(e) => updateProxy(idx, 'password', e.target.value)}
                  placeholder={translate("common:proxyConfigEditor.fieldFieldInline_placeholder_text")}
                />
              </label>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
