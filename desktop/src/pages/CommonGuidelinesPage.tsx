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
  if (!Number.isFinite(bytes) || bytes <= 0) return '空文件';
  if (bytes < 1024) return `${bytes} 字节`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  return new Date(seconds * 1000).toLocaleString();
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
  const navigate = useNavigate();
  const [files, setFiles] = useState<TranslationGuidelineFile[]>([]);
  const [dir, setDir] = useState('');
  const [defaultName, setDefaultName] = useState('');
  const [selectedName, setSelectedName] = useState('');
  const [content, setContent] = useState('');
  const [savedContent, setSavedContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
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
      setError(normalizeError(err, '读取规范失败'));
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
      setError(normalizeError(err, '加载通用翻译规范失败'));
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
      if (dirty && !window.confirm('当前规范有未保存的修改，切换会丢掉这些修改，是否继续？')) {
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
      setFeedback('已保存。改完的内容在下一次启动翻译时生效。');
    } catch (err) {
      setError(normalizeError(err, '保存规范失败'));
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
      setError('请先填写新规范的文件名（如 MyStyle，未写后缀会自动补 .md）。');
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
      setFeedback(`已新建「${created}」。在项目配置里把 common.gpt.translation_guideline 选成它即可生效。`);
    } catch (err) {
      setError(normalizeError(err, '新建规范失败'));
    } finally {
      setBusy(false);
    }
  }, [defaultName, dir, newFileName]);

  const handleDelete = useCallback(async () => {
    if (!selectedName) return;
    if (!window.confirm(`确定删除规范文件「${selectedName}」？此操作不可撤销。`)) return;
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
        `已删除「${selectedName}」。若有项目配置里正选着它，记得去那些项目的配置里换一份。`,
      );
    } catch (err) {
      setError(normalizeError(err, '删除规范失败'));
    } finally {
      setBusy(false);
    }
  }, [loadContent, selectedName]);

  const actionsDisabled = busy || loading;

  return (
    <div className="common-guidelines-page">
      <PageHeader
        className="common-guidelines-page__header"
        title="通用翻译规范"
        description="管理全局翻译规范文件。项目在配置里选用其中一份，翻译时它拼在项目规范之前；项目专属要求请到该项目的配置页维护。"
      />

      <div className="common-guidelines-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>规范文件</h2>
              <p>
                文件位于程序根目录 <code>translation_guidelines/</code>，只支持 .md / .txt；
                改完保存后，<strong>下一次启动翻译</strong>才生效。
              </p>
            </div>
          </header>

          {dir ? (
            <div className="common-guidelines-page__dir" title={dir}>
              目录：<code>{dir}</code>
            </div>
          ) : null}

          {loading ? (
            <LoadingState title="加载中…" description="正在读取通用翻译规范目录。" />
          ) : error && files.length === 0 ? (
            <ErrorState title="加载失败" description={error} />
          ) : files.length === 0 ? (
            <EmptyState
              title="目录里还没有规范文件"
              description="用下面的输入框新建一份（如 MyStyle.md），或把已有的规范文件放进该目录。"
            />
          ) : (
            <>
              <label className="settings-number-row">
                <span className="settings-number-row__label">当前规范</span>
                <div className="settings-number-row__control common-guidelines-page__select">
                  <CustomSelect
                    value={selectedName}
                    onChange={(event) => handleSelect(event.target.value)}
                  >
                    {files.map((file) => (
                      <option key={file.name} value={file.name}>
                        {file.name}
                        {file.builtin ? ' · 兜底' : ''}
                      </option>
                    ))}
                  </CustomSelect>
                </div>
              </label>

              {selected ? (
                <div className="common-guidelines-page__meta">
                  <span>{formatSize(selected.size)}</span>
                  <span>最后修改：{formatTime(selected.mtime)}</span>
                  {selected.builtin ? (
                    <span>未配置规范的项目的兜底文件，不能删除</span>
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
                  {busy ? '处理中…' : '保存规范'}
                </button>
                <button
                  type="button"
                  className="button button--secondary"
                  disabled={actionsDisabled || !dirty}
                  title={dirty ? '放弃未保存的修改，从文件重新读取' : '当前没有未保存的修改'}
                  onClick={() => {
                    void handleReload();
                  }}
                >
                  重新读取
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={actionsDisabled || !selected || selected.builtin}
                  title={selected?.builtin ? '兜底文件不能删除' : '删除当前规范文件'}
                  onClick={() => {
                    void handleDelete();
                  }}
                >
                  删除
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={busy}
                  onClick={() => navigate('/settings')}
                >
                  返回设置
                </button>
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
                  {dirty ? '有未保存的修改' : '已是最新'}
                </span>
                <span>{content.length} 字符</span>
                <span>Ctrl / Cmd + S 保存</span>
              </div>
            </>
          )}

          <div className="common-guidelines-page__create">
            <label className="common-guidelines-page__create-field">
              <span>新建规范</span>
              <input
                type="text"
                value={newFileName}
                placeholder="如 MyStyle（自动补 .md）"
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
            >
              新建
            </button>
            <span className="common-guidelines-page__create-tip">
              新建后到项目配置里把「翻译规范」选成它，该项目才会用上。
            </span>
          </div>

          {error && files.length > 0 ? (
            <div className="common-guidelines-page__error" role="alert">
              {error}
            </div>
          ) : null}
          {feedback ? <div className="common-guidelines-page__feedback">{feedback}</div> : null}
          {defaultName ? (
            <div className="common-guidelines-page__tip">
              项目没配置「翻译规范」时会兜底使用 <code>{defaultName}</code>。
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
