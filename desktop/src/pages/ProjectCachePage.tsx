import { useCallback, useEffect, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import { speakerStyle } from '../lib/speaker';
import {
  ApiError,
  type FileEntry,
  type CacheEntry,
  type CacheSearchResult,
  type CacheSearchField,
  type CacheReplaceField,
  type CacheReplaceFileDetail,
  fetchProjectCache,
  fetchCacheFile,
  saveCacheFile,
  searchCache,
  replaceCache,
} from '../lib/api';

/** 兼容读取缓存字段：优先新key，回退旧key */
function src(e: CacheEntry): string { return e.post_src || e.post_jp || ''; }
function dst(e: CacheEntry): string { return e.pre_dst || e.pre_zh || ''; }

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

type SidebarTab = 'files' | 'search';

/* ── Highlight helper ── */
function HighlightText({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const qLower = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let searchFrom = 0;
  while (searchFrom < lower.length) {
    const found = lower.indexOf(qLower, searchFrom);
    if (found === -1) break;
    if (found > lastIdx) parts.push(text.slice(lastIdx, found));
    parts.push(<mark key={found} className="search-highlight">{text.slice(found, found + query.length)}</mark>);
    lastIdx = found + query.length;
    searchFrom = lastIdx;
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return <>{parts}</>;
}

/* ── Cache Entry Card ── */
function CacheEntryCard({
  entry,
  filename,
  projectId,
  onEntryChange,
  onDelete,
  highlightQuery,
}: {
  entry: CacheEntry;
  filename: string;
  projectId: string;
  onEntryChange: (index: number, field: keyof CacheEntry, value: string) => void;
  onDelete: (index: number) => void;
  highlightQuery?: string;
}) {
  const hasProblem = !!entry.problem;
  const speaker = Array.isArray(entry.name) ? entry.name.join('/') : entry.name || '—';
  const [expanded, setExpanded] = useState(false);

  return (
    <article className={`cache-card ${hasProblem ? 'cache-card--problem' : ''}`} data-cache-index={entry.index}>
      <div className="cache-card__row">
        <span className="cache-card__field-label">#{entry.index}</span>
        {speaker !== '—' && (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(speaker)}>{speaker}</span>
        )}
        {hasProblem && (
          <span className="cache-card__pill cache-card__pill--problem">{entry.problem}</span>
        )}
        <div className="cache-card__spacer" />
        {entry.trans_by && (
          <span className="cache-card__pill cache-card__pill--engine">{entry.trans_by}</span>
        )}
        <button
          type="button"
          className="cache-card__expand"
          onClick={() => setExpanded(!expanded)}
          title={expanded ? '收起' : '展开详情'}
        >
          {expanded ? '▾' : '▸'}
        </button>
        <button
          type="button"
          className="cache-card__delete"
          onClick={() => onDelete(entry.index)}
          title="删除此条"
        >
          ✕
        </button>
      </div>

      <div className="cache-card__fields">
        {/* 折叠态：原文 + 译文 */}
        {!expanded && (
          <>
            <div className="cache-card__field">
              <span className="cache-card__field-label">原文</span>
              <div className="cache-card__input-wrap">
                <span className="cache-card__readonly-input">
                  {highlightQuery ? <HighlightText text={src(entry)} query={highlightQuery} /> : src(entry)}
                </span>
              </div>
            </div>
            <div className="cache-card__field">
              <span className="cache-card__field-label">译文</span>
              <div className="cache-card__input-wrap">
                <input
                  className="cache-card__input cache-card__input--zh"
                  value={dst(entry)}
                  onChange={(e) => onEntryChange(entry.index, 'pre_dst', e.target.value)}
                  placeholder="译文"
                />
                {highlightQuery && <span className="cache-card__input-overlay cache-card__input-overlay--zh"><HighlightText text={dst(entry)} query={highlightQuery} /></span>}
              </div>
            </div>
          </>
        )}
        {/* 展开态：五个字段 */}
        {expanded && (
          <>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">pre_src</span>
              <div className="cache-card__readonly-textarea">
                {(entry.pre_src || '').replace(/\n/g, '\\n')}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">post_src</span>
              <div className="cache-card__readonly-textarea">
                {highlightQuery ? <HighlightText text={src(entry)} query={highlightQuery} /> : src(entry)}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">pre_dst</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={entry.pre_dst || entry.pre_zh || ''}
                onChange={(e) => onEntryChange(entry.index, 'pre_dst', e.target.value)}
                placeholder="预翻译"
                rows={3}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">proofread</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={entry.proofread_dst || entry.proofread_zh || ''}
                onChange={(e) => onEntryChange(entry.index, 'proofread_dst', e.target.value)}
                placeholder="校对"
                rows={3}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">preview</span>
              <div className="cache-card__readonly-textarea">
                {entry.post_dst_preview || entry.post_zh_preview || ''}
              </div>
            </div>
          </>
        )}
      </div>
    </article>
  );
}

/* ── Search Result Card ── */
function SearchResultCard({
  result,
  query,
  onJumpToFile,
}: {
  result: CacheSearchResult;
  query: string;
  onJumpToFile: (filename: string, index: number) => void;
}) {
  const speaker = Array.isArray(result.speaker) ? result.speaker.join('/') : result.speaker || '—';

  return (
    <button
      type="button"
      className="search-result-card"
      onClick={() => onJumpToFile(result.filename, result.index)}
      title={`跳转到 ${result.filename} #${result.index}`}
    >
      <div className="search-result-card__header">
        <span className="search-result-card__file">{result.filename}</span>
        <span className="search-result-card__index">#{result.index}</span>
        {speaker !== '—' && (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(speaker)}>{speaker}</span>
        )}
        {result.match_src && <span className="search-result-card__badge search-result-card__badge--src">原文</span>}
        {result.match_dst && <span className="search-result-card__badge search-result-card__badge--dst">译文</span>}
        {result.problem && <span className="cache-card__pill cache-card__pill--problem">{result.problem}</span>}
      </div>
      {result.post_src && (
        <div className="search-result-card__line">
          <span className="search-result-card__label">原文</span>
          <span className="search-result-card__text"><HighlightText text={result.post_src} query={query} /></span>
        </div>
      )}
      {result.pre_dst && (
        <div className="search-result-card__line">
          <span className="search-result-card__label">译文</span>
          <span className="search-result-card__text search-result-card__text--dst"><HighlightText text={result.pre_dst} query={query} /></span>
        </div>
      )}
    </button>
  );
}

/* ── Main Page ── */
export function ProjectCachePage() {
  const { projectId, configFileName } = useOutletContext<OutletContext>();

  const [cacheFiles, setCacheFiles] = useState<FileEntry[]>([]);
  const [cacheDir, setCacheDir] = useState<string>('');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [entries, setEntries] = useState<CacheEntry[]>([]);
  /** 每个文件的条目缓存（含未保存修改） */
  const entriesMapRef = useRef<Map<string, CacheEntry[]>>(new Map());
  /** 有未保存修改的文件集合 */
  const [dirtyFiles, setDirtyFiles] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [refreshingFiles, setRefreshingFiles] = useState(false);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterProblems, setFilterProblems] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingAll, setSavingAll] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  // Tab state
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('files');

  // Global search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchField, setSearchField] = useState<CacheSearchField>('all');
  const [searchResults, setSearchResults] = useState<CacheSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchTotal, setSearchTotal] = useState(0);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Replace state
  const [replaceQuery, setReplaceQuery] = useState('');
  const [replaceWith, setReplaceWith] = useState('');
  const [replaceField, setReplaceField] = useState<CacheReplaceField>('dst');
  const [showReplace, setShowReplace] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [replacePreview, setReplacePreview] = useState<CacheReplaceFileDetail[] | null>(null);
  const [replacePreviewTotal, setReplacePreviewTotal] = useState(0);

  // Scroll-to-entry after clicking search result
  const [scrollToIndex, setScrollToIndex] = useState<number | null>(null);

  /** 当前文件是否dirty */
  const dirty = selectedFile != null && dirtyFiles.has(selectedFile);

  const loadCacheFiles = useCallback(
    async (showPageLoading = false) => {
      if (!projectId) return;
      if (showPageLoading) {
        setLoading(true);
      } else {
        setRefreshingFiles(true);
      }
      setError(null);
      try {
        const res = await fetchProjectCache(projectId);
        const files = res.files.filter((f) => f.is_file && f.name.endsWith('.json'));
        setCacheFiles(files);
        setCacheDir(res.cache_dir || '');
        setSelectedFile((prev) => (prev && files.some((file) => file.name === prev) ? prev : null));
      } catch (err) {
        setError(getErrorMessage(err, '加载缓存列表失败'));
      } finally {
        if (showPageLoading) {
          setLoading(false);
        } else {
          setRefreshingFiles(false);
        }
      }
    },
    [projectId],
  );

  useEffect(() => {
    void loadCacheFiles(true);
  }, [loadCacheFiles]);

  useEffect(() => {
    if (!projectId || !selectedFile) return;
    // 如果 entriesMap 中有缓存（含未保存修改），直接使用
    const cached = entriesMapRef.current.get(selectedFile);
    if (cached) {
      setEntries(cached);
      setLoadingEntries(false);
      return;
    }
    let cancelled = false;
    setLoadingEntries(true);
    fetchCacheFile(projectId, selectedFile)
      .then((res) => {
        if (!cancelled) {
          setEntries(res.entries);
          entriesMapRef.current.set(selectedFile, res.entries);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载缓存内容失败'));
      })
      .finally(() => {
        if (!cancelled) setLoadingEntries(false);
      });
    return () => { cancelled = true; };
  }, [projectId, selectedFile]);

  // Scroll to entry after jumping from search result
  useEffect(() => {
    if (scrollToIndex === null || loadingEntries) return;
    // Small delay to ensure DOM is rendered
    const timer = setTimeout(() => {
      const el = document.querySelector(`[data-cache-index="${scrollToIndex}"]`);
      if (el) {
        el.scrollIntoView({ behavior: 'instant', block: 'center' });
        el.classList.add('cache-card--highlight');
        setTimeout(() => el.classList.remove('cache-card--highlight'), 2000);
      }
      setScrollToIndex(null);
    }, 100);
    return () => clearTimeout(timer);
  }, [scrollToIndex, loadingEntries]);

  // Auto-search with debounce
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      setSearchTotal(0);
      return;
    }
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => {
      if (!projectId || !searchQuery.trim()) return;
      setSearching(true);
      searchCache(projectId, searchQuery.trim(), searchField)
        .then((res) => {
          setSearchResults(res.results);
          setSearchTotal(res.total);
        })
        .catch((err) => {
          setLocalError(getErrorMessage(err, '全局搜索失败'));
          setSearchResults([]);
          setSearchTotal(0);
        })
        .finally(() => setSearching(false));
    }, 400);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [projectId, searchQuery, searchField]);

  const filteredEntries = entries.filter((e) => {
    if (filterProblems && !e.problem) return false;
    if (searchTerm) {
      const term = searchTerm.toLowerCase();
      return (
        (src(e)?.toLowerCase().includes(term)) ||
        (dst(e)?.toLowerCase().includes(term))
      );
    }
    return true;
  });

  const total = entries.length;
  const translated = entries.filter((e) => dst(e)).length;
  const withProblems = entries.filter((e) => e.problem).length;

  const handleEntryChange = (index: number, field: keyof CacheEntry, value: string) => {
    setEntries((prev) => {
      const next = prev.map((e) => (e.index === index ? { ...e, [field]: value } : e));
      if (selectedFile) entriesMapRef.current.set(selectedFile, next);
      return next;
    });
    if (selectedFile) {
      setDirtyFiles((prev) => new Set(prev).add(selectedFile));
    }
    setInfo(null);
  };

  const handleDelete = (index: number) => {
    if (!selectedFile) return;
    setEntries((prev) => {
      const next = prev.filter((e) => e.index !== index);
      // Re-index remaining entries
      const reindexed = next.map((e, i) => ({ ...e, index: i }));
      entriesMapRef.current.set(selectedFile, reindexed);
      return reindexed;
    });
    setDirtyFiles((prev) => new Set(prev).add(selectedFile));
    setInfo(null);
  };

  const handleSave = async (filename?: string) => {
    const targetFile = filename || selectedFile;
    if (!targetFile) return;
    const targetEntries = entriesMapRef.current.get(targetFile);
    if (!targetEntries) return;
    setSaving(true);
    setLocalError(null);
    setInfo(null);
    try {
      const res = await saveCacheFile(projectId, targetFile, targetEntries, configFileName);
      const savedEntries = res.entries || targetEntries;
      entriesMapRef.current.set(targetFile, savedEntries);
      // 如果保存的是当前打开的文件，同步 entries 状态
      if (targetFile === selectedFile) {
        setEntries(savedEntries);
      }
      setDirtyFiles((prev) => {
        const next = new Set(prev);
        next.delete(targetFile);
        return next;
      });
      setInfo(targetFile === selectedFile ? '已保存并重建缓存' : `已保存 ${targetFile}`);
    } catch (err) {
      setLocalError(getErrorMessage(err, '保存缓存失败'));
    } finally {
      setSaving(false);
    }
  };

  /** 保存所有有修改的文件 */
  const handleSaveAll = async () => {
    const filesToSave = Array.from(dirtyFiles);
    if (filesToSave.length === 0) return;
    setSavingAll(true);
    setLocalError(null);
    setInfo(null);
    const savedFiles: string[] = [];
    let lastError: string | null = null;
    for (const file of filesToSave) {
      const fileEntries = entriesMapRef.current.get(file);
      if (!fileEntries) continue;
      try {
        const res = await saveCacheFile(projectId, file, fileEntries, configFileName);
        const savedEntries = res.entries || fileEntries;
        entriesMapRef.current.set(file, savedEntries);
        if (file === selectedFile) {
          setEntries(savedEntries);
        }
        savedFiles.push(file);
      } catch (err) {
        lastError = getErrorMessage(err, `保存 ${file} 失败`);
      }
    }
    // 清除成功保存的文件的 dirty 标记
    setDirtyFiles((prev) => {
      const next = new Set(prev);
      for (const f of savedFiles) next.delete(f);
      return next;
    });
    if (lastError) {
      setLocalError(lastError);
    } else {
      setInfo(`已保存 ${savedFiles.length} 个文件`);
    }
    setSavingAll(false);
  };

  const handleSelectFile = (file: string) => {
    // 先保存当前文件的修改到 entriesMap
    if (selectedFile) {
      entriesMapRef.current.set(selectedFile, entries);
    }
    setSelectedFile(file);
    setLocalError(null);
    setInfo(null);
  };

  // Jump from search result to file editor
  const handleJumpToFile = (filename: string, index: number) => {
    if (selectedFile) {
      entriesMapRef.current.set(selectedFile, entries);
    }
    setSelectedFile(filename);
    setScrollToIndex(index);
    setLocalError(null);
    setInfo(null);
  };

  // Replace preview (dry run)
  const handleReplacePreview = async () => {
    if (!replaceQuery.trim()) return;
    setReplacing(true);
    setLocalError(null);
    try {
      const res = await replaceCache(projectId, replaceQuery.trim(), replaceWith, replaceField, true);
      setReplacePreview(res.file_details);
      setReplacePreviewTotal(res.total_matches);
    } catch (err) {
      setLocalError(getErrorMessage(err, '替换预览失败'));
    } finally {
      setReplacing(false);
    }
  };

  // Replace execute
  const handleReplaceExecute = async () => {
    if (!replaceQuery.trim()) return;
    if (!confirm(`确定要在 ${replacePreviewTotal} 处将「${replaceQuery}」替换为「${replaceWith}」吗？此操作不可撤销。`)) {
      return;
    }
    setReplacing(true);
    setLocalError(null);
    try {
      const res = await replaceCache(projectId, replaceQuery.trim(), replaceWith, replaceField, false);
      setReplacePreview(null);
      setReplacePreviewTotal(0);
      setShowReplace(false);
      setReplaceQuery('');
      setReplaceWith('');
      setInfo(`已替换 ${res.total_matches} 处（涉及 ${res.total_files} 个文件）`);
      // Refresh search if query was set
      if (searchQuery.trim()) {
        const sr = await searchCache(projectId, searchQuery.trim(), searchField);
        setSearchResults(sr.results);
        setSearchTotal(sr.total);
      }
      // Refresh current file if open — 替换是全局操作，需从服务端刷新
      if (selectedFile) {
        const cf = await fetchCacheFile(projectId, selectedFile);
        setEntries(cf.entries);
        entriesMapRef.current.set(selectedFile, cf.entries);
        // 替换操作本身已保存到后端，但其他文件的本地修改可能被覆盖
        // 清除当前文件的 dirty，但不清其他文件
        setDirtyFiles((prev) => {
          const next = new Set(prev);
          next.delete(selectedFile);
          return next;
        });
      }
    } catch (err) {
      setLocalError(getErrorMessage(err, '全局替换失败'));
    } finally {
      setReplacing(false);
    }
  };

  if (loading) {
    return (
      <div className="project-cache-page">
        <div className="project-cache-page__header"><h1>缓存编辑</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  return (
    <div className="project-cache-page">
      <div className="project-cache-page__header">
        <div className="project-cache-page__header-row">
          <h1>缓存编辑</h1>
          {cacheDir && (
            <Button variant="secondary" onClick={() => void invoke('open_folder', { path: cacheDir })} title={cacheDir}>
              📂 打开缓存文件夹
            </Button>
          )}
        </div>
        <p>查看与编辑翻译缓存，对比原文与译文，筛选问题句。</p>
      </div>

      {error && <div className="inline-alert inline-alert--error" role="alert">{error}</div>}
      {localError && <div className="inline-alert inline-alert--error" role="alert">{localError}</div>}
      {info && <div className="inline-alert inline-alert--success" role="status">{info}</div>}

      <div className="cache-layout">
        <aside className="cache-layout__sidebar">
          {/* Tab bar */}
          <div className="cache-sidebar-tabs">
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'files' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => setSidebarTab('files')}
            >
              📁 文件列表
            </button>
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'search' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => setSidebarTab('search')}
            >
              🔍 全局搜索
            </button>
          </div>

          {/* Tab: Files */}
          {sidebarTab === 'files' && (
            <div className="cache-sidebar-tab-content">
              <div className="cache-layout__sidebar-header">
                <h3>缓存文件</h3>
                <div className="cache-layout__sidebar-header-actions">
                  {dirtyFiles.size > 0 && (
                    <Button
                      type="button"
                      variant="primary"
                      className="cache-file-save-all"
                      onClick={() => void handleSaveAll()}
                      disabled={savingAll}
                      title={`保存 ${dirtyFiles.size} 个有修改的文件`}
                    >
                      {savingAll ? '⏳' : `💾 全部保存 (${dirtyFiles.size})`}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="secondary"
                    className="cache-file-refresh"
                    onClick={() => void loadCacheFiles()}
                    disabled={refreshingFiles}
                    title="刷新缓存文件列表"
                    aria-label="刷新缓存文件列表"
                  >
                    {refreshingFiles ? '⏳' : '🔄'}
                  </Button>
                </div>
              </div>
              <div className="cache-file-list">
                {cacheFiles.map((file) => (
                  <button
                    type="button"
                    key={file.name}
                    className={`cache-file-item ${selectedFile === file.name ? 'cache-file-item--active' : ''} ${dirtyFiles.has(file.name) ? 'cache-file-item--dirty' : ''}`}
                    onClick={() => handleSelectFile(file.name)}
                  >
                    <span className="cache-file-item__name">
                      {dirtyFiles.has(file.name) && <span className="cache-file-item__dot" title="有未保存修改" />}
                      {file.name}
                    </span>
                    <span className="cache-file-item__size">{file.entry_count != null ? `${file.entry_count} 行` : formatSize(file.size)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Tab: Search */}
          {sidebarTab === 'search' && (
            <div className="cache-search-panel">
              <div className="cache-search-input-group">
                <input
                  type="text"
                  className="cache-search cache-search--global"
                  placeholder="搜索内容…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
                <select
                  className="cache-search-field"
                  value={searchField}
                  onChange={(e) => setSearchField(e.target.value as CacheSearchField)}
                >
                  <option value="all">全部</option>
                  <option value="src">仅原文</option>
                  <option value="dst">仅译文</option>
                </select>
              </div>

              {/* Replace toggle */}
              <div className="cache-replace-toggle">
                <button
                  type="button"
                  className="cache-replace-toggle__btn"
                  onClick={() => { setShowReplace(!showReplace); setReplaceQuery(searchQuery); }}
                  title={showReplace ? '隐藏替换' : '显示替换'}
                >
                  {showReplace ? '▾ 替换' : '▸ 替换'}
                </button>
              </div>

              {showReplace && (
                <div className="cache-replace-group">
                  <input
                    type="text"
                    className="cache-search cache-search--replace"
                    placeholder="搜索内容…"
                    value={replaceQuery}
                    onChange={(e) => setReplaceQuery(e.target.value)}
                  />
                  <input
                    type="text"
                    className="cache-search cache-search--replace"
                    placeholder="替换为…"
                    value={replaceWith}
                    onChange={(e) => setReplaceWith(e.target.value)}
                  />
                  <select
                    className="cache-search-field"
                    value={replaceField}
                    onChange={(e) => setReplaceField(e.target.value as CacheReplaceField)}
                  >
                    <option value="dst">译文</option>
                    <option value="src">原文</option>
                    <option value="all">全部</option>
                  </select>
                  <div className="cache-replace-actions">
                    <Button
                      variant="secondary"
                      disabled={replacing || !replaceQuery.trim()}
                      onClick={() => void handleReplacePreview()}
                    >
                      预览
                    </Button>
                    <Button
                      variant="primary"
                      disabled={replacing || !replaceQuery.trim() || replacePreviewTotal === 0}
                      onClick={() => void handleReplaceExecute()}
                    >
                      {replacing ? '替换中…' : '替换'}
                    </Button>
                  </div>
                  {replacePreview !== null && (
                    <div className="cache-replace-preview">
                      <div className="cache-replace-preview__summary">
                        共 {replacePreviewTotal} 处匹配，{replacePreview.length} 个文件
                      </div>
                      {replacePreview.map((fd) => (
                        <div key={fd.filename} className="cache-replace-preview__file">
                          <span className="cache-replace-preview__filename">{fd.filename}</span>
                          <span className="cache-replace-preview__count">{fd.matches} 处</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Search results summary */}
              {searching && <div className="cache-search-status">搜索中…</div>}
              {!searching && searchQuery.trim() && (
                <div className="cache-search-status">
                  {searchTotal > 0 ? `${searchTotal} 条结果` : '无匹配结果'}
                </div>
              )}

              {/* Search results list */}
              <div className="cache-search-results">
                {searchResults.map((r) => (
                  <SearchResultCard
                    key={`${r.filename}-${r.index}`}
                    result={r}
                    query={searchQuery.trim()}
                    onJumpToFile={handleJumpToFile}
                  />
                ))}
              </div>
            </div>
          )}
        </aside>

        <div className="cache-layout__main">
          {selectedFile ? (
            <Panel
              title={selectedFile}
              description={`${total} 句 · ${translated} 已翻译 · ${withProblems} 有问题`}
              actions={(
                <div className="cache-panel-actions">
                  <Button onClick={() => void handleSave()} disabled={saving || !dirty}>
                    {saving ? '保存中…' : '保存'}
                  </Button>
                </div>
              )}
            >
              <div className="cache-toolbar">
                <input
                  type="text"
                  placeholder="搜索原文或译文…"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="cache-search"
                />
                <label className="cache-filter">
                  <input
                    type="checkbox"
                    checked={filterProblems}
                    onChange={(e) => setFilterProblems(e.target.checked)}
                  />
                  只看问题句
                </label>
              </div>

              <div className="cache-card-list-wrapper">
                {loadingEntries && (
                  <div className="cache-card-list-loading">
                    <strong>加载中…</strong>
                  </div>
                )}
                <div className={`cache-card-list ${loadingEntries ? 'cache-card-list--loading' : ''}`} key={selectedFile}>
                  {filteredEntries.map((entry) => (
                    <CacheEntryCard
                      key={`${selectedFile}-${entry.index}`}
                      entry={entry}
                      filename={selectedFile}
                      projectId={projectId}
                      onEntryChange={handleEntryChange}
                      onDelete={handleDelete}
                      highlightQuery={searchTerm || searchQuery}
                    />
                  ))}
                  {filteredEntries.length === 0 && !loadingEntries && (
                    <div className="empty-state">
                      <strong>无匹配条目</strong>
                      <span>尝试更换搜索关键词。</span>
                    </div>
                  )}
                </div>
              </div>
            </Panel>
          ) : (
            <div className="empty-state">
              <strong>选择一个缓存文件</strong>
              <span>从左侧选择缓存文件查看翻译内容，或使用全局搜索。</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
