import { useCallback, useEffect, useState } from 'react';
import { Panel } from '../components/Panel';
import {
  ApiError,
  type PluginInfo,
  fetchPlugins,
} from '../lib/api';

export function PluginsPage() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPlugins()
      .then((res) => {
        if (!cancelled) setPlugins(res);
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载插件列表失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const filePlugins = plugins.filter((p) => p.type === 'file');
  const textPlugins = plugins.filter((p) => p.type === 'text');
  const filteredPlugins = typeFilter === 'file' ? filePlugins : typeFilter === 'text' ? textPlugins : plugins;

  if (loading) {
    return (
      <div className="plugins-page">
        <div className="plugins-page__header"><h1>插件管理</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="plugins-page">
        <div className="plugins-page__header"><h1>插件管理</h1></div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  return (
    <div className="plugins-page">
      <div className="plugins-page__header">
        <h1>插件管理</h1>
        <p>查看和管理翻译插件，共 {plugins.length} 个插件。</p>
      </div>

      <div className="plugins-page__content">
        <div className="plugin-tabs">
          <button
            className={`plugin-tab ${typeFilter === '' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('')}
          >
            全部 ({plugins.length})
          </button>
          <button
            className={`plugin-tab ${typeFilter === 'file' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('file')}
          >
            文件插件 ({filePlugins.length})
          </button>
          <button
            className={`plugin-tab ${typeFilter === 'text' ? 'plugin-tab--active' : ''}`}
            onClick={() => setTypeFilter('text')}
          >
            文本插件 ({textPlugins.length})
          </button>
        </div>

        <div className="plugin-list">
          {filteredPlugins.map((plugin) => (
            <div key={plugin.name} className="plugin-card">
              <div className="plugin-card__header">
                <span className="plugin-card__name">{plugin.display_name}</span>
                <span className="plugin-card__version">v{plugin.version}</span>
                <span className={`plugin-card__type plugin-card__type--${plugin.type}`}>
                  {plugin.type === 'file' ? '文件' : '文本'}
                </span>
              </div>
              <div className="plugin-card__meta">
                {plugin.author && <span>作者: {plugin.author}</span>}
                <span>模块: {plugin.module}</span>
              </div>
              {plugin.description && (
                <p className="plugin-card__desc">{plugin.description}</p>
              )}
              {Object.keys(plugin.settings).length > 0 && (
                <div className="plugin-card__settings">
                  <h4>设置项</h4>
                  {Object.entries(plugin.settings).map(([key, value]) => (
                    <div key={key} className="plugin-setting-item">
                      <span className="plugin-setting-item__key">{key}:</span>
                      <span className="plugin-setting-item__value">{String(value)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
