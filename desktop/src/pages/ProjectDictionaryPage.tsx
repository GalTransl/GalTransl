import { useCallback, useEffect, useState } from 'react';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { DictionaryManager } from '../components/DictionaryManager';
import {
  type ProjectDictionaryManagerResponse,
  createProjectDictionaryFile,
  deleteProjectDictionaryFile,
  fetchProjectDictionaryManager,
  saveProjectDictionaryFile,
  type DictionaryCategory } from '../lib/api';
import { normalizeError } from '../lib/errors';

export function ProjectDictionaryPage({ ctx }: { ctx: ProjectPageContext }) {
  const { projectId, configFileName } = ctx;

  const [data, setData] = useState<ProjectDictionaryManagerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const loadData = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchProjectDictionaryManager(projectId, configFileName);
      setData(res);
    } catch (err) {
      setError(normalizeError(err, '加载项目字典失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId, configFileName]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  return (
    <DictionaryManager
      title="项目字典"
      description="仅管理项目目录下的字典文件，支持卡片编辑与纯文本编辑。"
      data={data}
      loading={loading}
      error={error}
      onReload={loadData}
      onCreateFile={async (category: DictionaryCategory, filename: string) => {
        if (!projectId) {
          throw new Error('projectId is required');
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
    />
  );
}

