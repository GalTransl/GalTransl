import { useCallback, useEffect, useState } from 'react';
import { PageHeader } from '../components/PageHeader';
import { ConnectionStatusCard } from '../features/connection/ConnectionStatusCard';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import { useConnection } from '../features/connection/ConnectionContext';
import {
  type PluginInfo,
  fetchPlugins,
  getHomeHistoryRetentionLimit,
  getHomeJobRetentionLimit,
  HOME_LIST_LIMIT_MAX,
  HOME_LIST_LIMIT_MIN,
  setHomeHistoryRetentionLimit,
  setHomeJobRetentionLimit,
} from '../lib/api';
import { normalizeError } from '../lib/errors';


function PluginListSection() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
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

  return (
    <section className="panel">
      <header className="panel__header">
        <div>
          <h2>插件清单</h2>
          <p>查看当前可用的翻译插件。</p>
        </div>
      </header>

      {loading ? (
        <LoadingState title="加载插件中…" description="正在读取当前可用的文件插件与文本插件。" />
      ) : error ? (
        <ErrorState title="加载插件列表失败" description={error} />
      ) : (
        <>
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
        </>
      )}
    </section>
  );
}


export function SettingsPage() {
  const {
    backendUrl,
    connectionPhase,
    connectionMessage,
    loadingInitialData,
    refreshingJobs,
    loadInitialData,
    translators } = useConnection();

  const [homeHistoryLimitInput, setHomeHistoryLimitInput] = useState(() => String(getHomeHistoryRetentionLimit()));
  const [homeJobLimitInput, setHomeJobLimitInput] = useState(() => String(getHomeJobRetentionLimit()));

  const applyHomeHistoryLimit = useCallback((rawValue: string) => {
    const next = setHomeHistoryRetentionLimit(rawValue.trim() === '' ? Number.NaN : Number(rawValue));
    setHomeHistoryLimitInput(String(next));
  }, []);

  const applyHomeJobLimit = useCallback((rawValue: string) => {
    const next = setHomeJobRetentionLimit(rawValue.trim() === '' ? Number.NaN : Number(rawValue));
    setHomeJobLimitInput(String(next));
  }, []);

  return (
    <div className="settings-page">
      <PageHeader className="settings-page__header" title="设置" description="管理应用配置和后端连接。" />

      <div className="settings-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>首页记忆保留</h2>
              <p>控制首页历史项目与翻译任务列表保留条数。</p>
            </div>
          </header>

          <label className="settings-number-row">
            <span className="settings-number-row__label">历史项目保留条数</span>
            <div className="settings-number-row__control">
              <input
                type="number"
                min={HOME_LIST_LIMIT_MIN}
                max={HOME_LIST_LIMIT_MAX}
                value={homeHistoryLimitInput}
                onChange={(event) => {
                  setHomeHistoryLimitInput(event.target.value);
                }}
                onBlur={() => {
                  applyHomeHistoryLimit(homeHistoryLimitInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <label className="settings-number-row">
            <span className="settings-number-row__label">翻译任务保留条数</span>
            <div className="settings-number-row__control">
              <input
                type="number"
                min={HOME_LIST_LIMIT_MIN}
                max={HOME_LIST_LIMIT_MAX}
                value={homeJobLimitInput}
                onChange={(event) => {
                  setHomeJobLimitInput(event.target.value);
                }}
                onBlur={() => {
                  applyHomeJobLimit(homeJobLimitInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <div className="settings-toggle-row__desc">
            取值范围 {HOME_LIST_LIMIT_MIN}-{HOME_LIST_LIMIT_MAX}。超出范围会自动修正。
          </div>
        </section>

        <ConnectionStatusCard
          backendUrl={backendUrl}
          connectionMessage={connectionMessage}
          connectionPhase={connectionPhase}
          isRefreshing={loadingInitialData || refreshingJobs}
          onRefresh={() => {
            void loadInitialData();
          }}
          translatorCount={translators.length}
        />

        <PluginListSection />
      </div>
    </div>
  );
}
