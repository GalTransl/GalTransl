import { Panel } from '../../components/Panel';
import { CustomSelect } from '../../components/CustomSelect';
import { BackendConfigEditor } from '../../components/BackendConfigEditor';
import { InlineFeedback } from '../../components/page-state';
import { ProxyConfigEditor } from '../../components/ProxyConfigEditor';

interface BackendSettingsSectionProps {
  config: Record<string, unknown> | null;
  selectedProfile: string;
  defaultProfileName: string;
  backendProfileNames: string[];
  onProfileChange: (profile: string) => void;
  onBackendChange: (newBackend: Record<string, unknown>) => void;
  onCommonChange: (newCommon: Record<string, unknown>) => void;
  onProxyChange: (newProxy: Record<string, unknown>) => void;
  onDirty: () => void;
}

export function BackendSettingsSection({
  config,
  selectedProfile,
  defaultProfileName,
  backendProfileNames,
  onProfileChange,
  onBackendChange,
  onCommonChange,
  onProxyChange,
  onDirty,
}: BackendSettingsSectionProps) {
  const resolvedProfile = selectedProfile === '__default__' ? defaultProfileName : selectedProfile;
  const commonConfig = (config?.common as Record<string, unknown>) || {};
  const autoAdjustWorkers = commonConfig.autoAdjustWorkers !== false;

  return (
    <Panel title="翻译后端" description="OpenAI兼容接口、Sakura本地模型和代理配置。">
      <div className="config-form">
        <label className="field">
          <span>全局后端配置</span>
          <CustomSelect
            value={selectedProfile}
            onChange={(e) => onProfileChange(e.target.value)}
          >
            <option value="__default__">跟随全局默认</option>
            <option value="">不使用（使用项目自身配置）</option>
            {backendProfileNames.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </CustomSelect>
          <span className="field__hint">
            {selectedProfile === '__default__'
              ? defaultProfileName
                ? `当前默认配置为「${defaultProfileName}」，可在「翻译后端配置」页面修改`
                : '尚未设置默认配置，请在「翻译后端配置」页面设置'
              : selectedProfile
                ? `翻译时将使用全局配置「${selectedProfile}」覆盖项目后端设置`
                : '将忽略全局配置，使用项目自身的后端设置'}
          </span>
        </label>

        {resolvedProfile ? (
          <InlineFeedback
            tone="info"
            title={`当前使用全局配置：${resolvedProfile}`}
            description="翻译时将使用该配置覆盖项目后端设置。如需修改配置内容，请前往「翻译后端配置」页面。"
          />
        ) : (
          <BackendConfigEditor
            config={config?.backendSpecific as Record<string, unknown> || {}}
            onChange={(newBackend) => { onBackendChange(newBackend); onDirty(); }}
            proxy={(config?.proxy as { http?: string; https?: string } | undefined) ?? null}
          />
        )}

        <label className="field">
          <span>自动调节并发 Worker</span>
          <CustomSelect
            value={String(autoAdjustWorkers)}
            onChange={(e) => {
              onCommonChange({ ...commonConfig, autoAdjustWorkers: e.target.value === 'true' });
              onDirty();
            }}
          >
            <option value="true">开启</option>
            <option value="false">关闭</option>
          </CustomSelect>
          <span className="field__hint">根据近期 429 比例和响应延迟自动降/升 worker 并发</span>
        </label>

        <ProxyConfigEditor
          proxyConfig={(config?.proxy as Record<string, unknown>) || {}}
          onChange={(newProxy) => { onProxyChange(newProxy); onDirty(); }}
        />
      </div>
    </Panel>
  );
}
