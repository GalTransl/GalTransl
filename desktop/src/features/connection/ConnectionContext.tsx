import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../../i18n";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ConnectionPhase, TranslatorOption, VersionCheckResponse } from '../../lib/api';
import { ensureDesktopBackendReady, fetchJobs, fetchTranslators, fetchVersion, fetchVersionCheck, getBackendBaseUrl } from '../../lib/api';
import { normalizeError } from '../../lib/errors';
import { getCurrentWindow } from '@tauri-apps/api/window';

type ConnectionContextValue = {
  backendUrl: string;
  connectionPhase: ConnectionPhase;
  connectionMessage: string;
  /** 启动流程进行到第几步（1 起；0 = 未开始）。启动界面据此显示步骤清单。 */
  connectionStep: number;
  /** 版本检查结果（检查未回来 / 失败时为 null）；更新提示弹窗据此判断。 */
  versionInfo: VersionCheckResponse | null;
  translators: TranslatorOption[];
  loadingInitialData: boolean;
  refreshingJobs: boolean;
  loadInitialData: () => Promise<void>;
  loadJobs: (silent?: boolean) => Promise<void>;
};

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

export function useConnection(): ConnectionContextValue {
  const value = useContext(ConnectionContext);
  if (!value) {
    throw new Error('useConnection must be used within a ConnectionProvider');
  }
  return value;
}

export function ConnectionProvider({ children }: { children: React.ReactNode }) {
  const uiLanguage = useUiLanguage();
  const [connectionPhase, setConnectionPhase] = useState<ConnectionPhase>('connecting');
  const [connectionMessage, setConnectionMessage] = useMessageState<string>(uiMessage("common:connectionContext.connectionMessageSetConnectionMessage_useState_pendingConnectionTranslationBackend"));
  const [connectionStep, setConnectionStep] = useState(1);
  const [versionInfo, setVersionInfo] = useState<VersionCheckResponse | null>(null);
  const [windowVersion, setWindowVersion] = useState('');
  const [translators, setTranslators] = useState<TranslatorOption[]>([]);
  const [loadingInitialData, setLoadingInitialData] = useState(true);
  const [refreshingJobs, setRefreshingJobs] = useState(false);

  const [backendUrl, setBackendUrl] = useState(getBackendBaseUrl);

  const loadJobs = useCallback(async (silent = false) => {
    if (!silent) {
      setRefreshingJobs(true);
    }

    try {
      await fetchJobs();
      setConnectionPhase('online');
      setConnectionMessage(uiMessage("common:connectionContext.loadJobs_setConnectionMessage_doneConnectionBackendJobStatusAuto"));
    } catch (error) {
      const message = normalizeError(error, uiMessage("common:connectionContext.message_normalizeError_readJobFailed"));
      setConnectionPhase('offline');
      setConnectionMessage(message);
    } finally {
      if (!silent) {
        setRefreshingJobs(false);
      }
    }
  }, []);

  const loadInitialData = useCallback(async () => {
    setLoadingInitialData(true);
    setConnectionPhase('connecting');
    setConnectionStep(1);
    setConnectionMessage(uiMessage("common:connectionContext.loadInitialData_setConnectionMessage_pendingTranslation"));

    try {
      setConnectionMessage(uiMessage("common:connectionContext.loadInitialData_setConnectionMessage_pendingCheckTranslation"));
      await ensureDesktopBackendReady({ timeoutMs: 20_000 });
      setBackendUrl(getBackendBaseUrl());

      setConnectionStep(2);
      setConnectionMessage(uiMessage("common:connectionContext.loadInitialData_setConnectionMessage_translationDonePendingLoadVersion"));
      // 两个请求互不依赖，并行发出去，省掉一次串行往返
      const [nextTranslators, version] = await Promise.all([fetchTranslators(), fetchVersion()]);
      setTranslators(nextTranslators);

      setConnectionStep(3);
      setConnectionMessage(uiMessage("common:connectionContext.loadInitialData_setConnectionMessage_pendingInterface"));

      setWindowVersion(version);

      // 更新检查不阻塞启动（外网请求可能慢）：结果存进 context，
      // 更新提示弹窗监听它，有新版本时自己弹出来。
      fetchVersionCheck()
        .then((result) => {
          setVersionInfo(result);
        })
        .catch(() => undefined);

      setConnectionPhase('online');
      setConnectionMessage(uiMessage("common:connectionContext.loadInitialData_setConnectionMessage_backendSubmitTranslationJob"));
    } catch (error) {
      const message = normalizeError(error, uiMessage("common:connectionContext.message_normalizeError_unableConnectionBackend"));
      setBackendUrl(getBackendBaseUrl());
      setTranslators([]);
      setConnectionPhase('offline');
      setConnectionMessage(message);
    } finally {
      setLoadingInitialData(false);
    }
  }, []);

  useEffect(() => {
    void loadInitialData();
  }, [loadInitialData]);

  useEffect(() => {
    const version = versionInfo?.version || windowVersion;
    if (!version) return;
    const title = translate(versionInfo?.update_available ? 'common:windowTitle.updateAvailable' : 'common:windowTitle.current', { version });
    document.title = title;
    if ('__TAURI_INTERNALS__' in window) void getCurrentWindow().setTitle(title).catch(() => undefined);
  }, [uiLanguage, windowVersion, versionInfo]);

  const value = useMemo<ConnectionContextValue>(
    () => ({
      backendUrl,
      connectionPhase,
      connectionMessage,
      connectionStep,
      versionInfo,
      translators,
      loadingInitialData,
      refreshingJobs,
      loadInitialData,
      loadJobs,
    }),
    [backendUrl,
      connectionPhase,
      connectionMessage,
      connectionStep,
      versionInfo,
      translators,
      loadingInitialData,
      refreshingJobs,
      loadInitialData,
      loadJobs,
    ],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

