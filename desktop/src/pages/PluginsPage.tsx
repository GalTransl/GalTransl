import { localizePlugin, pluginSettingDisplayValue } from "../i18n/plugins";
import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useState } from 'react';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import {
  type PluginInfo,
  fetchPlugins } from '../lib/api';
import { normalizeError } from '../lib/errors';

export function PluginsPage() {
  useUiLanguage();
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useMessageState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPlugins()
      .then((res) => {
        if (!cancelled) setPlugins(res);
      })
      .catch((err) => {
        if (!cancelled) setError(normalizeError(err, uiMessage("plugins:pluginsPage.pluginsPage_normalizeError_loadPluginFailed")));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const filePlugins = plugins.filter((p) => p.type === 'file');
  const textPlugins = plugins.filter((p) => p.type === 'text');
  const problemPlugins = plugins.filter((p) => p.type === 'problem');
  const filteredPlugins = typeFilter ? plugins.filter((plugin) => plugin.type === typeFilter) : plugins;

  if (loading) {
    return (
      <div className="plugins-page">
        <PageHeader className="plugins-page__header" title={translate("plugins:pluginsPage.pluginsPage_title_plugin")} />
        <LoadingState title={translate("plugins:pluginsPage.pluginsPage_title_loadPlugin")} description={translate("plugins:pluginsPage.pluginsPage_description_pendingReadCurrentFilePluginTextPlugin")} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="plugins-page">
        <PageHeader className="plugins-page__header" title={translate("plugins:pluginsPage.pluginsPage_title_plugin")} />
        <ErrorState title={translate("plugins:pluginsPage.pluginsPage_title_loadPluginFailed")} description={error} />
      </div>
    );
  }

  return (
    <div className="plugins-page">
      <PageHeader className="plugins-page__header" title={translate("plugins:pluginsPage.pluginsPage_title_plugin")} description={translate("plugins:pluginsPage.pluginsPage_description_translationPluginCountPlugin", { count: plugins.length })} />

      <div className="plugins-page__content">
        <div className="plugin-tabs">
          <button
            className={`plugin-tab ${typeFilter === '' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('')}
          >{translate("plugins:pluginsPage.pluginTabs_message_all", { count: plugins.length })}</button>
          <button
            className={`plugin-tab ${typeFilter === 'file' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('file')}
          >{translate("plugins:pluginsPage.pluginTabs_message_filePlugin", { count: filePlugins.length })}</button>
          <button
            className={`plugin-tab ${typeFilter === 'text' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('text')}
          >{translate("plugins:pluginsPage.pluginTabs_message_textPlugin", { count: textPlugins.length })}</button>
          <button
            className={`plugin-tab ${typeFilter === 'problem' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('problem')}
          >{translate('plugins:pluginsPage.pluginTabs_message_problemPlugin', { count: problemPlugins.length })}</button>
        </div>

        <div className="plugin-list">
          {filteredPlugins.length === 0 ? (
            <EmptyState
              title={typeFilter ? translate("plugins:pluginsPage.pluginList_title_currentFilterEmptyPlugin") : translate("plugins:pluginsPage.pluginList_title_emptyPlugin")}
              description={typeFilter ? translate("plugins:pluginsPage.pluginList_description_pluginCheckBackendPluginDirectory") : translate("plugins:pluginsPage.pluginList_description_backendNotBackPlugin")}
            />
          ) : filteredPlugins.map((plugin) => (
            <div key={plugin.name} className="plugin-card">
              <div className="plugin-card__header">
                <span className="plugin-card__name">{localizePlugin(plugin).display_name}</span>
                <span className="plugin-card__version">{translate("plugins:pluginsPage.pluginCardHeader_message_v", { version: plugin.version })}</span>
                <span className={`plugin-card__type plugin-card__type--${plugin.type}`}>
                  {plugin.type === 'file' ? translate("plugins:pluginsPage.pluginCardHeader_message_file") : plugin.type === 'problem' ? translate('plugins:pluginsPage.pluginCardHeader_message_problem') : translate("plugins:pluginsPage.pluginCardHeader_message_text")}
                </span>
              </div>
              <div className="plugin-card__meta">
                {plugin.author && <span>{translate("plugins:pluginsPage.pluginCardMeta_message_author", { author: plugin.author })}</span>}
                <span>{translate("plugins:pluginsPage.pluginCardMeta_message_module", { module: plugin.module })}</span>
              </div>
              {localizePlugin(plugin).description && (
                <p className="plugin-card__desc">{localizePlugin(plugin).description}</p>
              )}
              {Object.keys(plugin.settings).length > 0 && (
                <div className="plugin-card__settings">
                  <h4>{translate("plugins:pluginsPage.pluginCardSettings_message_settingsItem")}</h4>
                  {(() => {
                    const entries = Object.entries(plugin.settings);
                    const renderSettings = (advanced: boolean) => entries
                      .filter(([key]) => Boolean(localizePlugin(plugin).settings_schema?.[key]?.advanced) === advanced)
                      .map(([key, value]) => {
                        const schema = localizePlugin(plugin).settings_schema?.[key];
                        const displayValue = pluginSettingDisplayValue(plugin, key, value);
                        return <div key={key}>
                          <div className="plugin-setting-item">
                            <span className="plugin-setting-item__key">{schema?.label || key}:</span>
                            <span className="plugin-setting-item__value">{displayValue}</span>
                          </div>
                          {schema?.description && <p className="plugin-setting-row__hint">{schema.description}</p>}
                        </div>;
                      });
                    return <>
                      {renderSettings(false)}
                      {entries.some(([key]) => localizePlugin(plugin).settings_schema?.[key]?.advanced) && (
                        <details className="plugin-settings-advanced">
                          <summary>{translate("common:actions.advancedSettings")}</summary>
                          {renderSettings(true)}
                        </details>
                      )}
                    </>;
                  })()}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

