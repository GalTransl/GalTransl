import { useCallback, useEffect, useState } from 'react';
import { ConnectionStatusCard } from '../features/connection/ConnectionStatusCard';
import { useConnection } from '../features/connection/ConnectionContext';
import { ApiError, fetchAppSettings, updateAppSettings } from '../lib/api';

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

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

  const [printTranslationLogInTerminal, setPrintTranslationLogInTerminal] = useState(true);
  const [loadingAppSettings, setLoadingAppSettings] = useState(true);
  const [savingAppSettings, setSavingAppSettings] = useState(false);
  const [appSettingsError, setAppSettingsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadingAppSettings(true);
    setAppSettingsError(null);
    fetchAppSettings()
      .then((data) => {
        if (!cancelled) {
          setPrintTranslationLogInTerminal(Boolean(data.printTranslationLogInTerminal));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setAppSettingsError(getErrorMessage(error, '加载程序设置失败'));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoadingAppSettings(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleToggleTerminalLogs = useCallback(async (nextValue: boolean) => {
    const previousValue = printTranslationLogInTerminal;
    setPrintTranslationLogInTerminal(nextValue);
    setSavingAppSettings(true);
    setAppSettingsError(null);
    try {
      const saved = await updateAppSettings({ printTranslationLogInTerminal: nextValue });
      setPrintTranslationLogInTerminal(Boolean(saved.printTranslationLogInTerminal));
    } catch (error) {
      setPrintTranslationLogInTerminal(previousValue);
      setAppSettingsError(getErrorMessage(error, '保存程序设置失败'));
    } finally {
      setSavingAppSettings(false);
    }
  }, [printTranslationLogInTerminal]);

  return (
    <div className="settings-page">
      <div className="settings-page__header">
        <h1>设置</h1>
        <p>管理应用配置和后端连接。</p>
      </div>

      <div className="settings-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>翻译输出</h2>
              <p>控制翻译运行时终端输出行为。关闭后仅保留 except 异常信息。</p>
            </div>
          </header>

          <label className="settings-toggle-row">
            <span className="settings-toggle-row__label">终端打印翻译日志</span>
            <div className="settings-toggle-row__control">
              <label className="toggle-switch">
                <input
                  type="checkbox"
                  checked={printTranslationLogInTerminal}
                  disabled={loadingAppSettings || savingAppSettings}
                  onChange={(event) => {
                    void handleToggleTerminalLogs(event.target.checked);
                  }}
                />
                <span className="toggle-switch__slider" />
              </label>
            </div>
          </label>

          <div className="settings-toggle-row__desc">
            {loadingAppSettings ? '正在加载程序设置…' : '关闭后将静默进度条、翻译结果及普通错误输出，仅保留 except 异常信息。'}
          </div>
          {appSettingsError ? (
            <div className="inline-alert inline-alert--error" role="alert">{appSettingsError}</div>
          ) : null}
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
      </div>
    </div>
  );
}
