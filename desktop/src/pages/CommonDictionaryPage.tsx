import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useState } from 'react';
import { DictionaryManager } from '../components/DictionaryManager';
import type { DictionaryPreviewTarget } from '../components/ProjectPreviewContext';
import {
  createCommonDictionaryFile,
  deleteCommonDictionaryFile,
  fetchCommonDictionaryManager,
  saveCommonDictionaryFile,
  type CommonDictionaryManagerResponse,
  type DictionaryCategory } from '../lib/api';
import { normalizeError } from '../lib/errors';


/** `active` / `focusTarget` 供 Agent 的预览小窗使用：每次打开都重新加载，并定位到指定文件与词条。 */
export function CommonDictionaryPage({ active = true, focusTarget }: { active?: boolean; focusTarget?: DictionaryPreviewTarget } = {}) {
  useUiLanguage();
  const [data, setData] = useState<CommonDictionaryManagerResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useMessageState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchCommonDictionaryManager();
      setData(res);
    } catch (err) {
      setError(normalizeError(err, uiMessage("projects:commonDictionaryPage.loadData_normalizeError_loadDictionaryFailed")));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void loadData();
  }, [active, loadData]);

  return (
    <DictionaryManager
      title={translate("projects:commonDictionaryPage.commonDictionaryPage_title_dictionary")}
      description={translate("projects:commonDictionaryPage.commonDictionaryPage_description_directoryDictDictionaryFileEditTextEdit")}
      data={data}
      loading={loading}
      error={error}
      onReload={loadData}
      focusTarget={focusTarget}
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
