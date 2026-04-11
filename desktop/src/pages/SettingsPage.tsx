import { ConnectionStatusCard } from '../features/connection/ConnectionStatusCard';
import { useConnection } from '../features/connection/ConnectionContext';

export function SettingsPage() {
  const {
    backendUrl,
    connectionPhase,
    connectionMessage,
    loadingInitialData,
    refreshingJobs,
    loadInitialData,
    translators,
  } = useConnection();

  return (
    <div className="settings-page">
      <div className="settings-page__header">
        <h1>设置</h1>
        <p>管理应用配置和后端连接。</p>
      </div>

      <div className="settings-page__content">
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
      </div>
    </div>
  );
}
