import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { ConnectionPhase, TranslatorOption } from '../../lib/api';
import { fetchJobs, fetchTranslators, getHideBackendConsolePreference } from '../../lib/api';
import { normalizeError } from '../../lib/errors';

type EnsureBackendResult = {
  found: boolean;
  started: boolean;
  path: string;
};

function isLocalBackendUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.hostname === '::1';
  } catch {
    return false;
  }
}

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
  const backendAutoStartAttemptedRef = useRef(false);

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
    setConnectionMessage('正在连接本地翻译后端…');

    try {
      const nextTranslators = await fetchTranslators();
      setTranslators(nextTranslators);
      setConnectionPhase('online');
      setConnectionMessage('后端在线，可以立即提交本地翻译任务。');
    } catch (error) {
      if (!isLocalBackendUrl(backendUrl)) {
        const message = normalizeError(error, '无法连接到后端');
        setTranslators([]);
        setConnectionPhase('offline');
        setConnectionMessage(message);
      } else if (backendAutoStartAttemptedRef.current) {
        const message = normalizeError(error, '无法连接到本地后端');
        setTranslators([]);
        setConnectionPhase('offline');
        setConnectionMessage(message);
      } else {
        try {
          setConnectionMessage('未连接到后端，正在尝试自动拉起服务端…');
          const hideConsole = getHideBackendConsolePreference();
          const result = await invoke<EnsureBackendResult>('ensure_backend_running', {
            hideConsole,
          });
          backendAutoStartAttemptedRef.current = true;

          if (!result.found) {
            const message = normalizeError(error, '无法连接到本地后端');
            setTranslators([]);
            setConnectionPhase('offline');
            setConnectionMessage(`${message}（未检测到本地服务端可执行文件）`);
            return;
          }

          await new Promise((resolve) => {
            window.setTimeout(resolve, 900);
          });

          const nextTranslators = await fetchTranslators();
          setTranslators(nextTranslators);
          setConnectionPhase('online');
          setConnectionMessage('已自动拉起本地后端并完成连接。');
        } catch (autoStartError) {
          const message = normalizeError(autoStartError, '自动拉起本地后端失败');
          setTranslators([]);
          setConnectionPhase('offline');
          setConnectionMessage(message);
          backendAutoStartAttemptedRef.current = true;
        }
      }
    } finally {
      setLoadingInitialData(false);
    }
  }, [backendUrl]);

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

