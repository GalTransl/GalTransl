import { useCallback, useEffect, useState } from 'react';
import { DictionaryManager } from '../components/DictionaryManager';
import {
  createCommonDictionaryFile,
  deleteCommonDictionaryFile,
  fetchCommonDictionaryManager,
  saveCommonDictionaryFile,
  type CommonDictionaryManagerResponse,
  type DictionaryCategory } from '../lib/api';
import { normalizeError } from '../lib/errors';


export function CommonDictionaryPage() {
  const [data, setData] = useState<CommonDictionaryManagerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchCommonDictionaryManager();
      setData(res);
    } catch (err) {
      setError(normalizeError(err, '加载通用字典失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  return (
    <DictionaryManager
      title="通用字典管理"
      description="仅管理程序根目录 Dict 下的通用字典文件，支持卡片编辑与纯文本编辑。"
      data={data}
      loading={loading}
      error={error}
      onReload={loadData}
      onCreateFile={async (category: DictionaryCategory, filename: string) => {
        const result = await createCommonDictionaryFile({ category, filename });
        return result.filename;
      }}
      onSaveFile={async (fileKey: string, content: string) => {
        await saveCommonDictionaryFile({ filename: fileKey, content });
      }}
      onDeleteFile={async (fileKey: string) => {
        await deleteCommonDictionaryFile({ filename: fileKey });
      }}
    />
  );
}
