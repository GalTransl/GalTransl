import { UiError, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { DictionaryManager } from '../components/DictionaryManager';
import {
  type ProjectDictionaryManagerResponse,
  createProjectDictionaryFile,
  deleteProjectDictionaryFile,
  fetchProjectDictionaryManager,
  fetchProjectConfig,
  getSelectedBackendProfileJobPayload,
  saveProjectDictionaryFile,
  submitJob,
  type DictionaryCategory
} from '../lib/api';
import { summarizeBackendUsage } from '../lib/backendUsage';
import { normalizeError } from '../lib/errors';

const DICT_POLL_INTERVAL_MS = 3000;

function buildDictionarySnapshot(data: ProjectDictionaryManagerResponse | null): string {
  if (!data) return '';
  const collect = (category: 'pre' | 'gpt' | 'post', files: string[]) => [...files]
    .sort((a, b) => a.localeCompare(b))
    .map((file) => `${category}:${file}:${data.dict_contents[file]?.mtime ?? ''}`);

  return [
    ...collect('pre', data.pre_dict_files),
    ...collect('gpt', data.gpt_dict_files),
    ...collect('post', data.post_dict_files),
  ].join('|');
}

export function ProjectDictionaryPage({
  ctx,
  active = true,
}: {
  ctx: ProjectPageContext;
  active?: boolean;
}) {
  const uiLanguage = useUiLanguage();
  const { projectId, projectDir, configFileName } = ctx;
  const navigate = useNavigate();

  const [data, setData] = useState<ProjectDictionaryManagerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useMessageState<string | null>(null);
  const [isDocumentVisible, setIsDocumentVisible] = useState(() => document.visibilityState === 'visible');
  const [projectBackendConfig, setProjectBackendConfig] = useState<Record<string, unknown> | null>(null);
  const currentSnapshot = useMemo(() => buildDictionarySnapshot(data), [data]);

  // GenDic 用的是项目选择的那个后端（没单独指定就跟随全局默认），跟开始翻译同一套口径：
  // 二次确认里要如实写出来用的是哪个后端
  useEffect(() => {
    if (!projectId) {
      setProjectBackendConfig(null);
      return;
    }
    let cancelled = false;
    fetchProjectConfig(projectId, configFileName || 'config.yaml')
      .then((res) => {
        if (cancelled) return;
        const backendSpecific = res.config?.backendSpecific;
        setProjectBackendConfig(
          backendSpecific && typeof backendSpecific === 'object'
            ? backendSpecific as Record<string, unknown>
            : null,
        );
      })
      .catch(() => {
        if (!cancelled) setProjectBackendConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, configFileName]);

  const gendicBackend = useMemo(
    () => (projectDir ? summarizeBackendUsage(projectDir, projectBackendConfig) : null),
    [uiLanguage, projectDir, projectBackendConfig],
  );

  const loadData = useCallback(async (options?: { silent?: boolean }) => {
    if (!projectId) return;
    const silent = options?.silent === true;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const res = await fetchProjectDictionaryManager(projectId, configFileName);
      setData((prev) => {
        const prevSnapshot = buildDictionarySnapshot(prev);
        const nextSnapshot = buildDictionarySnapshot(res);
        return prevSnapshot === nextSnapshot ? prev : res;
      });
      if (silent) {
        setError((prev) => (prev ? null : prev));
      }
    } catch (err) {
      if (!silent) {
        setError(normalizeError(err, uiMessage("projects:projectDictionaryPage.loadData_normalizeError_loadProjectDictionaryFailed")));
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [projectId, configFileName]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      const visible = document.visibilityState === 'visible';
      setIsDocumentVisible(visible);
      if (visible && active) {
        void loadData({ silent: true });
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [active, loadData]);

  useEffect(() => {
    if (active && isDocumentVisible) {
      void loadData({ silent: true });
    }
  }, [active, isDocumentVisible, loadData]);

  useEffect(() => {
    if (!projectId || !isDocumentVisible || !active) return;
    let cancelled = false;
    let timerId = 0;

    const poll = async () => {
      if (cancelled) return;
      try {
        const res = await fetchProjectDictionaryManager(projectId, configFileName);
        if (cancelled) return;
        const nextSnapshot = buildDictionarySnapshot(res);
        if (nextSnapshot !== currentSnapshot) {
          setData(res);
          setError((prev) => (prev ? null : prev));
        }
      } catch {
      } finally {
        if (!cancelled) {
          timerId = window.setTimeout(() => {
            void poll();
          }, DICT_POLL_INTERVAL_MS);
        }
      }
    };

    timerId = window.setTimeout(() => {
      void poll();
    }, DICT_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
    };
  }, [active, projectId, configFileName, currentSnapshot, isDocumentVisible]);

  return (
    <DictionaryManager
      title={translate("projects:projectDictionaryPage.projectDictionaryPage_title_projectDictionary")}
      description={translate("projects:projectDictionaryPage.projectDictionaryPage_description_projectDirectoryDictionaryFileGPTDictionaryModel")}
      data={data}
      loading={loading}
      error={error}
      onReload={loadData}
      onCreateFile={async (category: DictionaryCategory, filename: string) => {
        if (!projectId) {
          throw new UiError(uiMessage('errors:projects.projectRequired'));
        }
        const result = await createProjectDictionaryFile(projectId, {
          config_file_name: configFileName,
          category,
          filename });
        return result.file_key;
      }}
      onSaveFile={async (fileKey: string, content: string) => {
        if (!projectId) return;
        await saveProjectDictionaryFile(projectId, {
          config_file_name: configFileName,
          file_key: fileKey,
          content });
      }}
      onDeleteFile={async (fileKey: string) => {
        if (!projectId) return;
        await deleteProjectDictionaryFile(projectId, {
          config_file_name: configFileName,
          file_key: fileKey,
          delete_file: true });
      }}
      gendicBackend={gendicBackend}
      // 条目行的「→」：拿着这个原文词去「浏览文本」搜它出现在哪。
      // 带一个 nonce（n）：同一个词连点两次时 URL 不变，缓存页拿不到新事件（那边按 n 去重）
      onOpenInCache={(sourceWord) => {
        if (!projectId || !sourceWord) return;
        navigate(`/project/${projectId}/cache?q=${encodeURIComponent(sourceWord)}&n=${Date.now()}`);
      }}
      onGenerateGptDict={async () => {
        if (!projectId || !projectDir) {
          throw new UiError(uiMessage("projects:projectDictionaryPage.projectDictionaryPage_message_projectUnableJob"));
        }
        await submitJob({
          config_file_name: configFileName || 'config.yaml',
          project_dir: projectDir,
          translator: 'GenDic',
          ...getSelectedBackendProfileJobPayload(projectDir),
        });
        navigate(`/project/${projectId}/translate`);
      }}
    />
  );
}

