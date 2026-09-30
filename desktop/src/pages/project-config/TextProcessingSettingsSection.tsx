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
  const pluginConfig = (config?.plugin as Record<string, unknown>) || {};
  const enabledTextPlugins = new Set(
    Array.isArray(pluginConfig.textPlugins) ? pluginConfig.textPlugins as string[] : []
  );

  return (
    <Panel title="文本处理" description="启用文本插件，配置翻译前后的文本处理规则。">
      <div className="config-form">
        <div className="plugin-section">
          <div className="plugin-section__title">文本插件</div>
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
                        {plugin.display_name}
                      </span>
                      <span className="plugin-check-item__module">
                        ({plugin.name})
                      </span>
                      {plugin.version && (
                        <span className="plugin-check-item__version">
                          v{plugin.version}
                        </span>
                      )}
                    </label>
                    {plugin.description && (
                      <div className="plugin-check-item__desc">{plugin.description}</div>
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
            <div className="plugin-check-empty">未找到可用的文本插件</div>
          )}
        </div>
      </div>
    </Panel>
  );
}
