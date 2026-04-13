import { useCallback, useEffect, useState } from 'react';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import {
  type PluginInfo,
  fetchPlugins } from '../lib/api';
import { normalizeError } from '../lib/errors';

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
        if (!cancelled) setError(normalizeError(err, '加载插件列表失败'));
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
        <PageHeader className="plugins-page__header" title="插件管理" />
        <LoadingState title="加载插件中…" description="正在读取当前可用的文件插件与文本插件。" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="plugins-page">
        <PageHeader className="plugins-page__header" title="插件管理" />
        <ErrorState title="加载插件列表失败" description={error} />
      </div>
    );
  }

  return (
    <div className="plugins-page">
      <PageHeader className="plugins-page__header" title="插件管理" description={`查看和管理翻译插件，共 ${plugins.length} 个插件。`} />

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
          {filteredPlugins.length === 0 ? (
            <EmptyState
              title={typeFilter ? '当前筛选下没有插件' : '暂无插件'}
              description={typeFilter ? '试试切换到其他插件类型，或检查后端插件目录。' : '后端暂未返回任何插件信息。'}
            />
          ) : filteredPlugins.map((plugin) => (
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

