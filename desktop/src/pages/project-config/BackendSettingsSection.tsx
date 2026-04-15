import { Panel } from '../../components/Panel';
import { CustomSelect } from '../../components/CustomSelect';
import { BackendConfigEditor } from '../../components/BackendConfigEditor';
import { InlineFeedback } from '../../components/page-state';
import { ProxyConfigEditor } from '../../components/ProxyConfigEditor';

interface BackendSettingsSectionProps {
  config: Record<string, unknown> | null;
  selectedProfile: string;
  backendProfileNames: string[];
  onProfileChange: (profile: string) => void;
  onBackendChange: (newBackend: Record<string, unknown>) => void;
  onProxyChange: (newProxy: Record<string, unknown>) => void;
  onDirty: () => void;
}

export function BackendSettingsSection({
  config,
  selectedProfile,
  backendProfileNames,
  onProfileChange,
  onBackendChange,
  onProxyChange,
  onDirty,
}: BackendSettingsSectionProps) {
  return (
    <Panel title="翻译后端" description="OpenAI兼容接口、Sakura本地模型和代理配置。">
      <div className="config-form">
        <label className="field">
          <span>全局后端配置</span>
          <CustomSelect
            value={selectedProfile}
            onChange={(e) => onProfileChange(e.target.value)}
          >
            <option value="">不使用（使用项目自身配置）</option>
            {backendProfileNames.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </CustomSelect>
          <span className="field__hint">
            {selectedProfile
              ? `翻译时将使用全局配置「${selectedProfile}」覆盖项目后端设置`
              : '新项目默认使用全局默认配置，可在「翻译后端配置」页面设置默认'}
          </span>
        </label>

        {selectedProfile ? (
          <InlineFeedback
            tone="info"
            title={`当前使用全局配置：${selectedProfile}`}
            description="翻译时将使用该配置覆盖项目后端设置。如需修改配置内容，请前往「翻译后端配置」页面。"
          />
        ) : (
          <BackendConfigEditor
            config={config?.backendSpecific as Record<string, unknown> || {}}
            onChange={(newBackend) => { onBackendChange(newBackend); onDirty(); }}
          />
        )}

        <ProxyConfigEditor
          proxyConfig={(config?.proxy as Record<string, unknown>) || {}}
          onChange={(newProxy) => { onProxyChange(newProxy); onDirty(); }}
        />
      </div>
    </Panel>
  );
}
