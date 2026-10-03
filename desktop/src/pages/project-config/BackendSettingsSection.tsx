import { t as translate, useUiLanguage } from "../../i18n";
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
  useUiLanguage();
  const resolvedProfile = selectedProfile === '__default__' ? defaultProfileName : selectedProfile;
  const commonConfig = (config?.common as Record<string, unknown>) || {};
  const autoAdjustWorkers = commonConfig.autoAdjustWorkers === true;

  return (
    <Panel title={translate("config:backendSettingsSection.backendSettingsSection_title_translationBackend")} description={translate("config:backendSettingsSection.backendSettingsSection_description_openAISakuraModelProxyConfig")}>
      <div className="config-form">
        <label className="field">
          <span>{translate("config:backendSettingsSection.field_message_backendConfig")}</span>
          <CustomSelect
            value={selectedProfile}
            onChange={(e) => onProfileChange(e.target.value)}
          >
            <option value="__default__">{translate("config:backendSettingsSection.field_message_default")}</option>
            <option value="">{translate("config:backendSettingsSection.field_message_projectConfig")}</option>
            {backendProfileNames.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </CustomSelect>
          <span className="field__hint">
            {selectedProfile === '__default__'
              ? defaultProfileName
                ? translate("config:backendSettingsSection.fieldHint_message_currentDefaultConfigModelSettingsChange", { defaultProfileName: defaultProfileName })
                : translate("config:backendSettingsSection.fieldHint_message_notSettingsDefaultConfigModelSettingsSettings")
              : selectedProfile
                ? translate("config:backendSettingsSection.fieldHint_message_translationConfigProjectBackendSettings", { selectedProfile: selectedProfile })
                : translate("config:backendSettingsSection.fieldHint_message_configProjectBackendSettings")}
          </span>
        </label>

        {resolvedProfile ? (
          <InlineFeedback
            tone="info"
            title={translate("config:backendSettingsSection.configForm_title_currentConfig", { resolvedProfile: resolvedProfile })}
            description={translate("config:backendSettingsSection.configForm_description_translationConfigProjectBackendSettingsChangeConfig")}
          />
        ) : (
          <BackendConfigEditor
            config={config?.backendSpecific as Record<string, unknown> || {}}
            onChange={(newBackend) => { onBackendChange(newBackend); onDirty(); }}
            proxy={(config?.proxy as { http?: string; https?: string } | undefined) ?? null}
          />
        )}

        <label className="field">
          <span>{translate("config:backendSettingsSection.field_message_autoConcurrencyWorker")}</span>
          <CustomSelect
            value={String(autoAdjustWorkers)}
            onChange={(e) => {
              onCommonChange({ ...commonConfig, autoAdjustWorkers: e.target.value === 'true' });
              onDirty();
            }}
          >
            <option value="true">{translate("common:actions.enabled")}</option>
            <option value="false">{translate("common:actions.close")}</option>
          </CustomSelect>
          <span className="field__hint">{translate("config:backendSettingsSection.field_message_429AutoWorkerConcurrency")}</span>
        </label>

        <ProxyConfigEditor
          proxyConfig={(config?.proxy as Record<string, unknown>) || {}}
          onChange={(newProxy) => { onProxyChange(newProxy); onDirty(); }}
        />
      </div>
    </Panel>
  );
}
