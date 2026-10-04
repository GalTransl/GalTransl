import { getUiLanguage } from "../i18n/core";
import { UiTrans, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import {
  type TranslationGuidelineFile,
  createTranslationGuideline,
  deleteTranslationGuideline,
  fetchTranslationGuidelineContent,
  fetchTranslationGuidelineManager,
  saveTranslationGuideline,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

/** 新建/空白规范时给个能照着改的骨架，比一句"请输入内容"有用（与项目规范那边同一份口径）。 */
const PLACEHOLDER = `示例（按需增删）：

## 术语与称呼
- 「お兄ちゃん」统一译作「哥哥」，不要用「老哥」

## 语气与文风
- 主角内心独白用书面语，不用网络用语
- 拟声词保留原文的声音感，不意译成"啪的一声"`;

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return translate("settings:commonGuidelinesPage.formatSize_message_file");
  if (bytes < 1024) return translate("settings:commonGuidelinesPage.formatSize_message_text", { bytes: bytes });
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  return new Date(seconds * 1000).toLocaleString(getUiLanguage());
}

/**
 * 通用翻译规范管理（设置 → 通用翻译规范管理）。
 *
 * 管的是程序根目录 `translation_guidelines/` 下的规范文件——各项目的
 * config `common.gpt.translation_guideline` 从这里选一份，翻译时拼在**项目规范之前**
 * （冲突以项目规范为准，项目规范在项目配置页维护）。
 *
 * 与项目规范同一套编辑口径：自带保存按钮（不走配置页那条 YAML 保存）、dirty 状态、
 * Ctrl/Cmd+S、字符数；改完**下一次启动翻译**才生效（翻译器只在初始化时读一次规范）。
 */
export function CommonGuidelinesPage() {
  const uiLanguage = useUiLanguage();
  const navigate = useNavigate();
  const [files, setFiles] = useState<TranslationGuidelineFile[]>([]);
  const [dir, setDir] = useState('');
  const [defaultName, setDefaultName] = useState('');
  const [selectedName, setSelectedName] = useState('');
  const [content, setContent] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
  const [feedback, setFeedback] = useMessageState<string | null>(null);
  const [newFileName, setNewFileName] = useState('');

  const selected = useMemo(
    () => files.find((file) => file.name === selectedName) ?? null,
    [files, selectedName],
  );
  const dirty = content !== savedContent;

  /** 拉一份规范正文；失败只报错、不动编辑器里已有的内容。 */
  const loadContent = useCallback(async (name: string) => {
    if (!name) {
      setContent('');
      setSavedContent('');
      return;
    }
    try {
      const res = await fetchTranslationGuidelineContent(name);
      setContent(res.content);
      setSavedContent(res.content);
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:commonGuidelinesPage.loadContent_normalizeError_readGuidelineFailed")));
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTranslationGuidelineManager();
      setFiles(res.files ?? []);
      setDir(res.dir ?? '');
      setDefaultName(res.default ?? '');
      // 当前选中的文件可能被别处删了：退回到第一份（没有就清空）
      const nextName = (res.files ?? []).some((file) => file.name === selectedName)
        ? selectedName
        : res.files?.[0]?.name ?? '';
      setSelectedName(nextName);
      await loadContent(nextName);
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:commonGuidelinesPage.load_normalizeError_loadTranslationGuidelineFailed")));
    } finally {
      setLoading(false);
    }
  }, [loadContent, selectedName]);

  useEffect(() => {
    void load();
    // 只在进页面时拉一次：之后的选择/保存都走本地状态，避免每次点选都重拉清单
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSelect = useCallback(
    (name: string) => {
      if (name === selectedName) return;
      if (dirty && !window.confirm(translate("settings:commonGuidelinesPage.handleSelect_confirm_currentGuidelineNotSaveChangeChange"))) {
        return;
      }
      setSelectedName(name);
      setFeedback(null);
      setError(null);
      void loadContent(name);
    },
    [dirty, loadContent, selectedName],
  );

  const handleSave = useCallback(async () => {
    if (!selectedName) return;
    setBusy(true);
    setError(null);
    setFeedback(null);
    try {
      await saveTranslationGuideline({ filename: selectedName, content });
      setSavedContent(content);
      // size 是磁盘字节数（后端按 stat 报），这里按 UTF-8 编码长度更新，别拿字符数糊上去
      const bytes = new TextEncoder().encode(content).length;
      setFiles((prev) =>
        prev.map((file) =>
          file.name === selectedName ? { ...file, size: bytes, mtime: Date.now() / 1000 } : file,
        ),
      );
      setFeedback(uiMessage("settings:commonGuidelinesPage.handleSave_setFeedback_doneSaveTranslationEffective"));
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:commonGuidelinesPage.handleSave_normalizeError_saveGuidelineFailed")));
    } finally {
      setBusy(false);
    }
  }, [content, selectedName]);

  const handleReload = useCallback(async () => {
    if (!selectedName) return;
    setFeedback(null);
    setError(null);
    await loadContent(selectedName);
  }, [loadContent, selectedName]);

  const handleCreate = useCallback(async () => {
    const name = newFileName.trim();
    if (!name) {
      setError(uiMessage("settings:commonGuidelinesPage.handleCreate_setError_guidelineFileMyStyleNotAutoMd"));
      return;
    }
    setBusy(true);
    setError(null);
    setFeedback(null);
    try {
      const res = await createTranslationGuideline({ filename: name });
      setNewFileName('');
      const created = res.filename;
      const listing = await fetchTranslationGuidelineManager();
      setFiles(listing.files ?? []);
      setDir(listing.dir ?? dir);
      setDefaultName(listing.default ?? defaultName);
      setSelectedName(created);
      setContent('');
      setSavedContent('');
      setFeedback(uiMessage("settings:commonGuidelinesPage.handleCreate_setFeedback_doneNewProjectConfigCommonGptTranslation", { created: created }));
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:commonGuidelinesPage.handleCreate_normalizeError_newGuidelineFailed")));
    } finally {
      setBusy(false);
    }
  }, [defaultName, dir, newFileName]);

  const handleDelete = useCallback(async () => {
    if (!selectedName) return;
    if (!window.confirm(translate("settings:commonGuidelinesPage.handleDelete_confirm_deleteGuidelineFile", { selectedName: selectedName }))) return;
    setBusy(true);
    setError(null);
    setFeedback(null);
    try {
      await deleteTranslationGuideline({ filename: selectedName });
      const listing = await fetchTranslationGuidelineManager();
      const remaining = listing.files ?? [];
      const nextName = remaining[0]?.name ?? '';
      setFiles(remaining);
      setSelectedName(nextName);
      await loadContent(nextName);
      setFeedback(
        uiMessage("settings:commonGuidelinesPage.handleDelete_setFeedback_doneDeleteProjectConfigProjectConfig", { selectedName: selectedName }),
      );
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:commonGuidelinesPage.handleDelete_normalizeError_deleteGuidelineFailed")));
    } finally {
      setBusy(false);
    }
  }, [loadContent, selectedName]);

  const actionsDisabled = busy || loading;

  return (
    <div className="common-guidelines-page">
      <PageHeader
        className="common-guidelines-page__header"
        title={translate("settings:commonGuidelinesPage.commonGuidelinesPage_title_translationGuideline")}
        description={translate("settings:commonGuidelinesPage.commonGuidelinesPage_description_translationGuidelineFileProjectConfigTranslationProject")}
      />

      <div className="common-guidelines-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>{translate("settings:commonGuidelinesPage.panelHeader_message_guidelineFile")}</h2>
              <p><UiTrans k="settings:commonGuidelinesPage.panelHeader_message_fileDirectory0TranslationGuidelines0Md" components={[<code />, <strong />]} /></p>
            </div>
          </header>

          {dir ? (
            <div className="common-guidelines-page__dir" title={dir}><UiTrans k="settings:commonGuidelinesPage.panel_message_directory00" values={{ dir: dir }} components={[<code />]} /></div>
          ) : null}

          {loading ? (
            <LoadingState title={translate("common:actions.loading")} description={translate("settings:commonGuidelinesPage.panel_description_pendingReadTranslationGuidelineDirectory")} />
          ) : error && files.length === 0 ? (
            <ErrorState title={translate("settings:commonGuidelinesPage.panel_title_loadFailed")} description={error} />
          ) : files.length === 0 ? (
            <EmptyState
              title={translate("settings:commonGuidelinesPage.panel_title_directoryEmptyGuidelineFile")}
              description={translate("settings:commonGuidelinesPage.panel_description_newMyStyleMdDoneGuidelineFileDirectory")}
            />
          ) : (
            <>
              <label className="settings-number-row">
                <span className="settings-number-row__label">{translate("settings:commonGuidelinesPage.settingsNumberRow_message_currentGuideline")}</span>
                <div className="settings-number-row__control common-guidelines-page__select">
                  <CustomSelect
                    value={selectedName}
                    onChange={(event) => handleSelect(event.target.value)}
                  >
                    {files.map((file) => (
                      <option key={file.name} value={file.name}>
                        {file.name}
                        {file.builtin ? translate("settings:commonGuidelinesPage.settingsNumberRowControlCommonGuidelinesPageSelect_message_text") : ''}
                      </option>
                    ))}
                  </CustomSelect>
                </div>
              </label>

              {selected ? (
                <div className="common-guidelines-page__meta">
                  <span>{formatSize(selected.size)}</span>
                  <span>{translate("settings:commonGuidelinesPage.commonGuidelinesPageMeta_message_change", { value: formatTime(selected.mtime) })}</span>
                  {selected.builtin ? (
                    <span>{translate("settings:commonGuidelinesPage.commonGuidelinesPageMeta_message_notConfiguredGuidelineProjectFileCannotDelete")}</span>
                  ) : null}
                </div>
              ) : null}

              <div className="common-guidelines-page__actions">
                <button
                  type="button"
                  className="button button--primary"
                  disabled={actionsDisabled || !dirty}
                  onClick={() => {
                    void handleSave();
                  }}
                >
                  {busy ? translate("settings:commonGuidelinesPage.buttonButtonPrimary_message_processing") : translate("settings:commonGuidelinesPage.buttonButtonPrimary_message_saveGuideline")}
                </button>
                <button
                  type="button"
                  className="button button--secondary"
                  disabled={actionsDisabled || !dirty}
                  title={dirty ? translate("settings:commonGuidelinesPage.buttonButtonSecondary_title_notSaveChangeFileRead") : translate("settings:commonGuidelinesPage.buttonButtonSecondary_title_currentEmptyNotSaveChange")}
                  onClick={() => {
                    void handleReload();
                  }}
                >{translate("settings:commonGuidelinesPage.commonGuidelinesPageActions_message_read")}</button>
                <button
                  type="button"
                  className="button"
                  disabled={actionsDisabled || !selected || selected.builtin}
                  title={selected?.builtin ? translate("settings:commonGuidelinesPage.button_title_fileCannotDelete") : translate("settings:commonGuidelinesPage.button_title_deleteCurrentGuidelineFile")}
                  onClick={() => {
                    void handleDelete();
                  }}
                >{translate("common:actions.delete")}</button>
                <button
                  type="button"
                  className="button"
                  disabled={busy}
                  onClick={() => navigate('/settings')}
                >{translate("settings:commonGuidelinesPage.commonGuidelinesPageActions_message_backSettings")}</button>
              </div>

              <textarea
                className="common-guidelines-page__editor"
                value={content}
                placeholder={PLACEHOLDER}
                spellCheck={false}
                onChange={(event) => {
                  setContent(event.target.value);
                  setFeedback(null);
                }}
                onKeyDown={(event) => {
                  // Ctrl/Cmd+S 直接保存：Markdown 编辑器里这是肌肉记忆
                  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
                    event.preventDefault();
                    if (dirty && !busy) void handleSave();
                  }
                }}
              />

              <div className="common-guidelines-page__foot">
                <span className={`common-guidelines-page__status${dirty ? ' is-dirty' : ''}`}>
                  {dirty ? translate("settings:commonGuidelinesPage.commonGuidelinesPageFoot_message_notSaveChange") : translate("settings:commonGuidelinesPage.commonGuidelinesPageFoot_message_done")}
                </span>
                <span>{translate("settings:commonGuidelinesPage.commonGuidelinesPageFoot_message_text", { count: content.length })}</span>
                <span>{translate("settings:commonGuidelinesPage.commonGuidelinesPageFoot_message_ctrlCmdSSave")}</span>
              </div>
            </>
          )}

          <div className="common-guidelines-page__create">
            <label className="common-guidelines-page__create-field">
              <span>{translate("settings:commonGuidelinesPage.commonGuidelinesPageCreateField_message_newGuideline")}</span>
              <input
                type="text"
                value={newFileName}
                placeholder={translate("settings:commonGuidelinesPage.commonGuidelinesPageCreateField_placeholder_myStyleAutoMd")}
                onChange={(event) => setNewFileName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !busy) void handleCreate();
                }}
              />
            </label>
            <button
              type="button"
              className="button button--secondary"
              disabled={busy || !newFileName.trim()}
              onClick={() => {
                void handleCreate();
              }}
            >{translate("settings:commonGuidelinesPage.commonGuidelinesPageCreate_message_new")}</button>
            <span className="common-guidelines-page__create-tip">{translate("settings:commonGuidelinesPage.commonGuidelinesPageCreate_message_newProjectConfigTranslationGuidelineProject")}</span>
          </div>

          {error && files.length > 0 ? (
            <div className="common-guidelines-page__error" role="alert">
              {error}
            </div>
          ) : null}
          {feedback ? <div className="common-guidelines-page__feedback">{feedback}</div> : null}
          {defaultName ? (
            <div className="common-guidelines-page__tip"><UiTrans k="settings:commonGuidelinesPage.panel_message_projectConfigTranslationGuideline00" values={{ defaultName: defaultName }} components={[<code />]} /></div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
