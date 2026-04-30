import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ConnectionPhase, TranslatorOption } from '../../lib/api';
import { ensureDesktopBackendReady, fetchJobs, fetchTranslators, fetchVersion, fetchVersionCheck } from '../../lib/api';
import { normalizeError } from '../../lib/errors';
import { getCurrentWindow } from '@tauri-apps/api/window';

type ConnectionContextValue = {
  backendUrl: string;
  connectionPhase: ConnectionPhase;
  connectionMessage: string;
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
  const [connectionPhase, setConnectionPhase] = useState<ConnectionPhase>('connecting');
  const [connectionMessage, setConnectionMessage] = useState('正在连接本地翻译后端…');
  const [translators, setTranslators] = useState<TranslatorOption[]>([]);
  const [loadingInitialData, setLoadingInitialData] = useState(true);
  const [refreshingJobs, setRefreshingJobs] = useState(false);

  const backendUrl = useMemo(() => {
    const configured = import.meta.env.VITE_BACKEND_URL?.trim();
    return configured ? configured.replace(/\/$/, '') : 'http://127.0.0.1:12333';
  }, []);

  const loadJobs = useCallback(async (silent = false) => {
    if (!silent) {
      setRefreshingJobs(true);
    }

    try {
      await fetchJobs();
      setConnectionPhase('online');
      setConnectionMessage('已连接到本地后端，任务状态会自动轮询刷新。');
    } catch (error) {
      const message = normalizeError(error, '读取任务列表失败');
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
    setConnectionMessage('正在准备本地翻译服务…');

    try {
      setConnectionMessage('正在启动并检查本地翻译服务…');
      await ensureDesktopBackendReady({ timeoutMs: 20_000 });
      setConnectionMessage('本地翻译服务已就绪，正在加载能力信息…');
      const nextTranslators = await fetchTranslators();
      setTranslators(nextTranslators);

      const version = await fetchVersion();
      const applyWindowTitle = async (title: string) => {
        if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
          try {
            await getCurrentWindow().setTitle(title);
          } catch {
            // ignore window title errors
          }
        } else {
          document.title = title;
        }
      };

      await applyWindowTitle(`GalTransl Desktop - v${version}`);

      fetchVersionCheck()
        .then(async (result) => {
          if (!result.update_available) {
            return;
          }
          await applyWindowTitle(`GalTransl Desktop - v${result.version}（有新版本）`);
        })
        .catch(() => undefined);

      setConnectionPhase('online');
      setConnectionMessage('后端在线，可以立即提交本地翻译任务。');
    } catch (error) {
      const message = normalizeError(error, '无法连接到本地后端');
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

  const value = useMemo<ConnectionContextValue>(
    () => ({
      backendUrl,
      connectionPhase,
      connectionMessage,
      translators,
      loadingInitialData,
      refreshingJobs,
      loadInitialData,
      loadJobs,
    }),
    [backendUrl, connectionPhase, connectionMessage, translators, loadingInitialData, refreshingJobs, loadInitialData, loadJobs],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

