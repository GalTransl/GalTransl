import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ConnectionPhase, TranslatorOption } from '../../lib/api';
import { ensureDesktopBackendReady, fetchJobs, fetchTranslators, fetchVersion, fetchVersionCheck } from '../../lib/api';
import { normalizeError } from '../../lib/errors';
import { getCurrentWindow } from '@tauri-apps/api/window';

type ConnectionContextValue = {
  backendUrl: string;
  connectionPhase: ConnectionPhase;
  connectionMessage: string;
  /** 启动流程进行到第几步（1 起；0 = 未开始）。启动界面据此显示步骤清单。 */
  connectionStep: number;
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
  const [connectionStep, setConnectionStep] = useState(1);
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
    setConnectionStep(1);
    setConnectionMessage('正在准备本地翻译服务…');

    try {
      setConnectionMessage('正在启动并检查本地翻译服务…');
      await ensureDesktopBackendReady({ timeoutMs: 20_000 });

      setConnectionStep(2);
      setConnectionMessage('本地翻译服务已就绪，正在加载模板与版本信息…');
      // 两个请求互不依赖，并行发出去，省掉一次串行往返
      const [nextTranslators, version] = await Promise.all([fetchTranslators(), fetchVersion()]);
      setTranslators(nextTranslators);

      setConnectionStep(3);
      setConnectionMessage('正在准备主界面…');

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
      connectionStep,
      translators,
      loadingInitialData,
      refreshingJobs,
      loadInitialData,
      loadJobs,
    }),
    [
      backendUrl,
      connectionPhase,
      connectionMessage,
      connectionStep,
      translators,
      loadingInitialData,
      refreshingJobs,
      loadInitialData,
      loadJobs,
    ],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

