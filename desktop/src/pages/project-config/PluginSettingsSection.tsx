import { Panel } from '../../components/Panel';
import { PluginSettingsEditor } from '../../components/PluginSettingsEditor';
import type { PluginInfo } from '../../lib/api';

interface PluginSettingsSectionProps {
  config: Record<string, unknown> | null;
  filePlugins: PluginInfo[];
  textPlugins: PluginInfo[];
  onFilePluginChange: (value: string) => void;
  onPluginSettingChange: (pluginName: string, key: string, value: unknown) => void;
  onToggleTextPlugin: (pluginName: string) => void;
}

export function PluginSettingsSection({
  config,
  filePlugins,
  textPlugins,
  onFilePluginChange,
  onPluginSettingChange,
  onToggleTextPlugin,
}: PluginSettingsSectionProps) {
  return (
    <Panel title="插件设置" description="文件插件和文本插件配置。">
      <div className="config-form">
        {/* ── 文件插件 ── */}
        <div className="plugin-section">
          <div className="plugin-section__title">文件插件</div>
          <label className="field">
            <select
              value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
              onChange={(e) => onFilePluginChange(e.target.value)}
            >
              {filePlugins.length > 0 ? (
                filePlugins.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.display_name} ({p.name})
                  </option>
                ))
              ) : (
                <option value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}>
                  {String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
                </option>
              )}
            </select>
            <span className="field__hint">从全局插件管理中获取可用文件插件</span>
          </label>
          {/* 文件插件设置项 */}
          {(() => {
            const selectedFilePlugin = filePlugins.find(
              (p) => p.name === String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')
            );
            if (!selectedFilePlugin || Object.keys(selectedFilePlugin.settings || {}).length === 0) return null;
            return (
              <PluginSettingsEditor
                plugin={selectedFilePlugin}
                overrides={((config?.plugin as Record<string, unknown>)?.[selectedFilePlugin.name] as Record<string, unknown>) || {}}
                onChange={onPluginSettingChange}
              />
            );
          })()}
        </div>

        {/* ── 文本插件 ── */}
        <div className="plugin-section">
          <div className="plugin-section__title">文本插件</div>
          {textPlugins.length > 0 ? (
            <div className="plugin-check-list">
              {textPlugins.map((plugin) => {
                const enabledTextPlugins = new Set(
                  Array.isArray((config?.plugin as Record<string, unknown>)?.textPlugins)
                    ? ((config?.plugin as Record<string, unknown>).textPlugins as string[])
                    : []
                );
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
                          overrides={((config?.plugin as Record<string, unknown>)?.[plugin.name] as Record<string, unknown>) || {}}
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
