import { localizePlugin } from "../../i18n/plugins";
import { t as translate, useUiLanguage } from "../../i18n";
import { Panel } from '../../components/Panel';
import { PluginSettingsEditor } from '../../components/PluginSettingsEditor';
import type { PluginInfo } from '../../lib/api';

interface TextProcessingSettingsSectionProps {
  config: Record<string, unknown> | null;
  textPlugins: PluginInfo[];
  onPluginSettingChange: (pluginName: string, key: string, value: unknown) => void;
  onToggleTextPlugin: (pluginName: string) => void;
}

export function TextProcessingSettingsSection({
  config,
  textPlugins,
  onPluginSettingChange,
  onToggleTextPlugin,
}: TextProcessingSettingsSectionProps) {
  useUiLanguage();
  const pluginConfig = (config?.plugin as Record<string, unknown>) || {};
  const enabledTextPlugins = new Set(
    Array.isArray(pluginConfig.textPlugins) ? pluginConfig.textPlugins as string[] : []
  );

  return (
    <Panel title={translate("config:textProcessingSettingsSection.textProcessingSettingsSection_title_textProcess")} description={translate("config:textProcessingSettingsSection.textProcessingSettingsSection_description_enableTextPluginConfigTranslationTextProcess")}>
      <div className="config-form">
        <div className="plugin-section">
          <div className="plugin-section__title">{translate("config:textProcessingSettingsSection.pluginSection_message_textPlugin")}</div>
          {textPlugins.length > 0 ? (
            <div className="plugin-check-list">
              {textPlugins.map((plugin) => {
                const isChecked = enabledTextPlugins.has(plugin.name);
                const hasSettings = Object.keys(plugin.settings || {}).length > 0;

                return (
                  <div key={plugin.name} className="plugin-check-item">
                    <label className="plugin-check-item__header">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => onToggleTextPlugin(plugin.name)}
                      />
                      <span className="plugin-check-item__name">
                        {localizePlugin(plugin).display_name}
                      </span>
                      <span className="plugin-check-item__module">
                        ({plugin.name})
                      </span>
                      {plugin.version && (
                        <span className="plugin-check-item__version">{translate("config:textProcessingSettingsSection.pluginCheckItemHeader_message_v", { version: plugin.version })}</span>
                      )}
                    </label>
                    {localizePlugin(plugin).description && (
                      <div className="plugin-check-item__desc">{localizePlugin(plugin).description}</div>
                    )}
                    {isChecked && hasSettings && (
                      <div className="plugin-check-item__settings">
                        <PluginSettingsEditor
                          plugin={plugin}
                          overrides={(pluginConfig[plugin.name] as Record<string, unknown>) || {}}
                          onChange={onPluginSettingChange}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="plugin-check-empty">{translate("config:textProcessingSettingsSection.pluginSection_message_notTextPlugin")}</div>
          )}
        </div>
      </div>
    </Panel>
  );
}
