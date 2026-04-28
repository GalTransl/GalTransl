import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback, LoadingState } from '../components/page-state';
import { speakerStyle, speakerHue } from '../lib/speaker';
import { useNameDict, resolveSpeakerName } from '../lib/useNameDict';
import {
  CACHE_BROWSER_FONT_SIZE_CHANGE_EVENT,
  type FileEntry,
  type CacheEntry,
  type CacheSearchResult,
  type CacheSearchField,
  type CacheReplaceField,
  type CacheReplaceFileDetail,
  type ProblemEntry,
  fetchProjectCache,
  fetchCacheFile,
  saveCacheFile,
  deleteCacheFiles,
  searchCache,
  replaceCache,
  fetchProjectProblems,
  fetchProjectConfig,
  getCacheBrowserFontSizePreference,
  updateProjectConfig } from '../lib/api';
import { normalizeError } from '../lib/errors';

/** 兼容读取缓存字段：优先新key，回退旧key */
function src(e: CacheEntry): string { return e.post_src || e.post_jp || ''; }
function dst(e: CacheEntry): string { return e.pre_dst || e.pre_zh || ''; }
function escapeControlChars(text: string): string {
  return text.replace(/\r/g, '\\r').replace(/\n/g, '\\n');
}
function unescapeControlChars(text: string): string {
  return text.replace(/\\r/g, '\r').replace(/\\n/g, '\n');
}

type SidebarTab = 'files' | 'search' | 'problems';
type CacheContextMenuState = {
  x: number;
  y: number;
  filenames: string[];
  showDelete: boolean;
};
const MIN_REFRESH_SPIN_MS = 420;
const REFRESH_SPIN_CYCLE_MS = 500;

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
  nameDict }: {
  entry: CacheEntry;
  filename: string;
  projectId: string;
  onEntryChange: (index: number, field: keyof CacheEntry, value: string) => void;
  onDelete: (index: number) => void;
  highlightQuery?: string;
  nameDict: Map<string, string>;
}) {
  const hasProblem = !!entry.problem;
  const rawSpeaker = Array.isArray(entry.name) ? entry.name.join('/') : entry.name || '—';
  const speaker = rawSpeaker !== '—'
    ? (Array.isArray(entry.name)
        ? entry.name.map((s) => resolveSpeakerName(s, nameDict)).join('/')
        : resolveSpeakerName(rawSpeaker, nameDict))
    : rawSpeaker;
  const [expanded, setExpanded] = useState(false);

  return (
    <article className={`cache-card ${hasProblem ? 'cache-card--problem' : ''}`} data-cache-index={entry.index}>
      <div className="cache-card__row">
        <span className="cache-card__field-label">#{entry.index}</span>
        {speaker !== '—' && (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(rawSpeaker)}>{speaker}</span>
        )}
        {hasProblem && (
          <div className="cache-card__problem-slot">
            <span className="cache-card__pill cache-card__pill--problem" title={entry.problem}>{entry.problem}</span>
          </div>
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
                <span className="cache-card__readonly-input" title={escapeControlChars(src(entry))}>
                  {highlightQuery
                    ? <HighlightText text={escapeControlChars(src(entry))} query={highlightQuery} />
                    : escapeControlChars(src(entry))}
                </span>
              </div>
            </div>
            <div className="cache-card__field">
              <span className="cache-card__field-label">译文</span>
              <div className="cache-card__input-wrap">
                <input
                  className="cache-card__input cache-card__input--zh"
                  value={escapeControlChars(dst(entry))}
                  onChange={(e) => onEntryChange(entry.index, 'pre_dst', unescapeControlChars(e.target.value))}
                  placeholder="译文"
                  title={escapeControlChars(dst(entry))}
                />
                {highlightQuery && (
                  <span className="cache-card__input-overlay cache-card__input-overlay--zh">
                    <HighlightText text={escapeControlChars(dst(entry))} query={highlightQuery} />
                  </span>
                )}
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
                {escapeControlChars(entry.pre_src || '')}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">post_src</span>
              <div className="cache-card__readonly-textarea">
                {highlightQuery
                  ? <HighlightText text={escapeControlChars(src(entry))} query={highlightQuery} />
                  : escapeControlChars(src(entry))}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">pre_dst</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={escapeControlChars(entry.pre_dst || entry.pre_zh || '')}
                onChange={(e) => onEntryChange(entry.index, 'pre_dst', unescapeControlChars(e.target.value))}
                placeholder="预翻译"
                rows={3}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">proofread</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={escapeControlChars(entry.proofread_dst || entry.proofread_zh || '')}
                onChange={(e) => onEntryChange(entry.index, 'proofread_dst', unescapeControlChars(e.target.value))}
                placeholder="校对"
                rows={3}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">preview</span>
              <div className="cache-card__readonly-textarea">
                {escapeControlChars(entry.post_dst_preview || entry.post_zh_preview || '')}
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
  nameDict,
  selected,
  onSelect,
  onContextMenu,
  idx }: {
  result: CacheSearchResult;
  query: string;
  onJumpToFile: (filename: string, index: number) => void;
  nameDict: Map<string, string>;
  selected: boolean;
  onSelect: () => void;
  onContextMenu: (event: React.MouseEvent<HTMLButtonElement>) => void;
  idx: number;
}) {
  const rawSpeaker = Array.isArray(result.speaker) ? result.speaker.join('/') : result.speaker || '—';
  const speaker = rawSpeaker !== '—'
    ? (Array.isArray(result.speaker)
        ? result.speaker.map((s) => resolveSpeakerName(s, nameDict)).join('/')
        : resolveSpeakerName(rawSpeaker, nameDict))
    : rawSpeaker;

  return (
    <button
      type="button"
      className={`search-result-card${selected ? ' search-result-card--selected' : ''}`}
      data-search-idx={idx}
      onClick={() => { onSelect(); onJumpToFile(result.filename, result.index); }}
      onContextMenu={(e) => {
        e.preventDefault();
        onSelect();
        onContextMenu(e);
      }}
      title={`跳转到 ${result.filename} #${result.index}`}
    >
      <div className="search-result-card__header">
        {(result.match_src || result.match_dst || result.match_problem) && (
          <span className="search-result-card__match-badges">
            {result.match_src && <span className="search-result-card__badge search-result-card__badge--src">原文</span>}
            {result.match_dst && <span className="search-result-card__badge search-result-card__badge--dst">译文</span>}
            {result.match_problem && <span className="search-result-card__badge search-result-card__badge--problem">问题</span>}
          </span>
        )}
        <span className="search-result-card__file">{result.filename}</span>
      </div>
      {(result.index !== undefined || speaker !== '—' || result.problem) && (
        <div className="search-result-card__tags">
          <span className="search-result-card__index">#{result.index}</span>
          {speaker !== '—' && (
            <span className="search-result-card__speaker" style={{ color: `hsl(${speakerHue(rawSpeaker)}, 55%, 32%)` }}>{speaker}</span>
          )}
          {result.problem && <span className="search-result-card__problem">{result.problem}</span>}
        </div>
      )}
      {result.post_src && (
        <div className="search-result-card__line">
          <span className="search-result-card__label">原文</span>
          <span className="search-result-card__text" title={escapeControlChars(result.post_src)}><HighlightText text={escapeControlChars(result.post_src)} query={query} /></span>
        </div>
      )}
      {result.pre_dst && (
        <div className="search-result-card__line">
          <span className="search-result-card__label">译文</span>
          <span className="search-result-card__text search-result-card__text--dst" title={escapeControlChars(result.pre_dst)}><HighlightText text={escapeControlChars(result.pre_dst)} query={query} /></span>
        </div>
      )}
    </button>
  );
}

/* ── Main Page ── */
export function ProjectCachePage({ ctx, active = true }: { ctx: ProjectPageContext; active?: boolean }) {
  const { projectId, configFileName } = ctx;
  const { nameDict } = useNameDict(projectId);
  const [cacheBrowserFontSize, setCacheBrowserFontSize] = useState(() => getCacheBrowserFontSizePreference());

  const [cacheFiles, setCacheFiles] = useState<FileEntry[]>([]);
  const [cacheDir, setCacheDir] = useState<string>('');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [entries, setEntries] = useState<CacheEntry[]>([]);
  /** 每个文件的条目缓存（含未保存修改），mount 后指向当前项目桶中的 Map */
  const entriesMapRef = useRef<Map<string, CacheEntry[]>>(new Map());
  /** 有未保存修改的文件集合 */
  const [dirtyFiles, setDirtyFiles] = useState<Set<string>>(new Set());

  /**
   * 按 projectId 分桶保存本页状态，跨项目切换时既不串数据、也不丢 dirty 编辑与选择。
   * 桶的内容会在 projectId 变化时先 snapshot 旧项目，再恢复或新建新项目的桶。
   */
  type ProjectBucket = {
    cacheFiles: FileEntry[];
    cacheDir: string;
    selectedFile: string | null;
    dirtyFiles: Set<string>;
    entries: Map<string, CacheEntry[]>;
    scrollPositions: Map<string, number>;
    sidebarTab: SidebarTab;
    searchQuery: string;
    searchField: CacheSearchField;
    searchResults: CacheSearchResult[];
    searchTotal: number;
    replaceQuery: string;
    replaceWith: string;
    replaceField: CacheReplaceField;
    showReplace: boolean;
  };

  useEffect(() => {
    const handleCacheBrowserFontSizeChange = () => {
      setCacheBrowserFontSize(getCacheBrowserFontSizePreference());
    };

    window.addEventListener(CACHE_BROWSER_FONT_SIZE_CHANGE_EVENT, handleCacheBrowserFontSizeChange as EventListener);
    return () => {
      window.removeEventListener(CACHE_BROWSER_FONT_SIZE_CHANGE_EVENT, handleCacheBrowserFontSizeChange as EventListener);
    };
  }, []);

  const cacheBrowserFontStyle = {
    '--cache-font-base': `${cacheBrowserFontSize}px`,
    '--cache-font-sm': `${Math.max(10, cacheBrowserFontSize - 1)}px`,
    '--cache-font-xs': `${Math.max(9, cacheBrowserFontSize - 2)}px`,
    '--cache-font-xxs': `${Math.max(8, cacheBrowserFontSize - 3)}px`,
  } as CSSProperties;
  const bucketsRef = useRef<Map<string, ProjectBucket>>(new Map());
  const lastProjectIdRef = useRef<string>('');
  const [loading, setLoading] = useState(true);
  const [refreshingFiles, setRefreshingFiles] = useState(false);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRef = useRef(active);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterProblems, setFilterProblems] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingAll, setSavingAll] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  // Tab state
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('files');

  // Sidebar width (draggable) with persistence
  const SIDEBAR_WIDTH_KEY = 'galtransl.cache.sidebarWidth';
  const SIDEBAR_MIN = 180;
  const SIDEBAR_MAX = 560;
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const v = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
      if (Number.isFinite(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX) return v;
    } catch {}
    return 240;
  });
  const [resizing, setResizing] = useState(false);
  const layoutRef = useRef<HTMLDivElement | null>(null);
  const handleResizerPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const layoutEl = layoutRef.current;
    if (!layoutEl) return;
    setResizing(true);
    const onMove = (ev: PointerEvent) => {
      const rect = layoutEl.getBoundingClientRect();
      const next = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, ev.clientX - rect.left));
      setSidebarWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }, []);
  useEffect(() => {
    try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth)); } catch {}
  }, [sidebarWidth]);

  // Global search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchField, setSearchField] = useState<CacheSearchField>('all');
  const [searchResults, setSearchResults] = useState<CacheSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchTotal, setSearchTotal] = useState(0);
  const [selectedSearchIdx, setSelectedSearchIdx] = useState(-1);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchResultsRef = useRef<HTMLDivElement>(null);

  // Problems tab state
  const [problems, setProblems] = useState<ProblemEntry[]>([]);
  const [loadingProblems, setLoadingProblems] = useState(false);
  // Retransl keyword popover editor
  const [retranslEditor, setRetranslEditor] = useState<{
    type: string;
    draft: string;
    anchor: { top: number; left: number };
  } | null>(null);
  const retranslPopoverRef = useRef<HTMLDivElement | null>(null);
  const retranslInputRef = useRef<HTMLInputElement | null>(null);

  // File multi-select state
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  const [contextMenu, setContextMenu] = useState<CacheContextMenuState | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);

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
  const listRef = useRef<HTMLDivElement | null>(null);
  const scrollPositionsRef = useRef<Map<string, number>>(new Map());
  const pendingScrollRestoreRef = useRef<{ file: string; top: number } | null>(null);

  // Post-load enter animation for cache list
  const [listEntering, setListEntering] = useState(false);
  const listEnterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingListEnterRef = useRef(false);

  /** 当前文件是否dirty */
  const dirty = selectedFile != null && dirtyFiles.has(selectedFile);

  const rememberCurrentScrollPosition = useCallback(() => {
    if (!selectedFile || !listRef.current) return;
    scrollPositionsRef.current.set(selectedFile, listRef.current.scrollTop);
  }, [selectedFile]);

  const prepareFileSwitch = useCallback((file: string) => {
    rememberCurrentScrollPosition();
    pendingScrollRestoreRef.current = {
      file,
      top: scrollPositionsRef.current.get(file) ?? 0 };
  }, [rememberCurrentScrollPosition]);

  const loadCacheFiles = useCallback(
    async (showPageLoading = false) => {
      if (!projectId) return;
      const startedAt = Date.now();
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
        setError(normalizeError(err, '加载缓存列表失败'));
      } finally {
        if (showPageLoading) {
          setLoading(false);
        } else {
          const elapsedMs = Date.now() - startedAt;
          const minReachedMs = Math.max(elapsedMs, MIN_REFRESH_SPIN_MS);
          const remainToFullCycleMs = (REFRESH_SPIN_CYCLE_MS - (minReachedMs % REFRESH_SPIN_CYCLE_MS)) % REFRESH_SPIN_CYCLE_MS;
          const remainMs = Math.max(0, MIN_REFRESH_SPIN_MS - elapsedMs) + remainToFullCycleMs;
          if (remainMs > 0) {
            await new Promise<void>((resolve) => window.setTimeout(resolve, remainMs));
          }
          setRefreshingFiles(false);
        }
      }
    },
    [projectId],
  );

  // 按 projectId 切换状态桶：先 snapshot 旧项目，再恢复或新建新项目的桶。
  // 该 effect 是本页跨项目状态保留的核心入口。
  useEffect(() => {
    if (!projectId) return;
    const prev = lastProjectIdRef.current;
    if (prev && prev !== projectId) {
      // snapshot 旧项目（此时 state 闭包仍是旧项目的数据，刚好用于写回）
      const prevBucket: ProjectBucket = bucketsRef.current.get(prev) ?? {
        cacheFiles: [],
        cacheDir: '',
        selectedFile: null,
        dirtyFiles: new Set(),
        entries: new Map(),
        scrollPositions: new Map(),
        sidebarTab: 'files',
        searchQuery: '',
        searchField: 'all',
        searchResults: [],
        searchTotal: 0,
        replaceQuery: '',
        replaceWith: '',
        replaceField: 'dst',
        showReplace: false,
      };
      prevBucket.cacheFiles = cacheFiles;
      prevBucket.cacheDir = cacheDir;
      prevBucket.selectedFile = selectedFile;
      prevBucket.dirtyFiles = dirtyFiles;
      prevBucket.entries = entriesMapRef.current;
      prevBucket.scrollPositions = scrollPositionsRef.current;
      prevBucket.sidebarTab = sidebarTab;
      prevBucket.searchQuery = searchQuery;
      prevBucket.searchField = searchField;
      prevBucket.searchResults = searchResults;
      prevBucket.searchTotal = searchTotal;
      prevBucket.replaceQuery = replaceQuery;
      prevBucket.replaceWith = replaceWith;
      prevBucket.replaceField = replaceField;
      prevBucket.showReplace = showReplace;
      bucketsRef.current.set(prev, prevBucket);
    }
    lastProjectIdRef.current = projectId;

    const existing = bucketsRef.current.get(projectId);
    if (existing) {
      // 恢复：ref 指向桶内共享 Map，state 恢复至桶内快照
      entriesMapRef.current = existing.entries;
      scrollPositionsRef.current = existing.scrollPositions;
      setCacheFiles(existing.cacheFiles);
      setCacheDir(existing.cacheDir);
      setSelectedFile(existing.selectedFile);
      setDirtyFiles(existing.dirtyFiles);
      setSidebarTab(existing.sidebarTab);
      setSearchQuery(existing.searchQuery);
      setSearchField(existing.searchField);
      setSearchResults(existing.searchResults);
      setSearchTotal(existing.searchTotal);
      setSelectedSearchIdx(-1);
      setReplaceQuery(existing.replaceQuery);
      setReplaceWith(existing.replaceWith);
      setReplaceField(existing.replaceField);
      setShowReplace(existing.showReplace);
      setReplacePreview(null);
      setReplacePreviewTotal(0);
      // 若当前选中文件在缓存 Map 中有值，切换 selectedFile 的 effect 会同步 entries
      if (!existing.selectedFile) setEntries([]);
      setLoading(false);
    } else {
      // 全新项目：重置 refs 与可见 state，然后拉取文件列表
      entriesMapRef.current = new Map();
      scrollPositionsRef.current = new Map();
      setCacheFiles([]);
      setCacheDir('');
      setSelectedFile(null);
      setDirtyFiles(new Set());
      setEntries([]);
      setSidebarTab('files');
      setSearchQuery('');
      setSearchField('all');
      setSearchResults([]);
      setSearchTotal(0);
      setSelectedSearchIdx(-1);
      setReplaceQuery('');
      setReplaceWith('');
      setReplaceField('dst');
      setShowReplace(false);
      setReplacePreview(null);
      setReplacePreviewTotal(0);
      void loadCacheFiles(true);
    }
    // 仅在 projectId 变化时运行；state 的 stale closure 正是我们需要快照的"旧值"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

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
        if (!cancelled) setError(normalizeError(err, '加载缓存内容失败'));
      })
      .finally(() => {
        if (!cancelled) setLoadingEntries(false);
      });
    return () => { cancelled = true; };
  }, [projectId, selectedFile]);

  const runGlobalSearch = useCallback(async () => {
    if (!projectId || !searchQuery.trim()) {
      setSearchResults([]);
      setSearchTotal(0);
      setSelectedSearchIdx(-1);
      return;
    }
    setSearching(true);
    try {
      const res = await searchCache(projectId, searchQuery.trim(), searchField);
      setSearchResults(res.results);
      setSearchTotal(res.total);
      setSelectedSearchIdx(-1);
    } catch (err) {
      setLocalError(normalizeError(err, '全局搜索失败'));
      setSearchResults([]);
      setSearchTotal(0);
    } finally {
      setSearching(false);
    }
  }, [projectId, searchField, searchQuery]);

  const refreshCurrentFile = useCallback(async () => {
    if (!projectId || !selectedFile || dirtyFiles.has(selectedFile)) return;
    setLoadingEntries(true);
    try {
      const res = await fetchCacheFile(projectId, selectedFile);
      entriesMapRef.current.set(selectedFile, res.entries);
      setEntries(res.entries);
    } catch (err) {
      setLocalError(normalizeError(err, '刷新缓存内容失败'));
    } finally {
      setLoadingEntries(false);
    }
  }, [dirtyFiles, projectId, selectedFile]);

  useEffect(() => {
    if (!selectedFile) {
      pendingScrollRestoreRef.current = null;
      return;
    }
    if (loadingEntries) return;
    const pendingRestore = pendingScrollRestoreRef.current;
    if (!pendingRestore || pendingRestore.file !== selectedFile) return;
    const frame = requestAnimationFrame(() => {
      if (!listRef.current) return;
      listRef.current.scrollTop = pendingRestore.top;
      scrollPositionsRef.current.set(selectedFile, pendingRestore.top);
      if (pendingScrollRestoreRef.current?.file === selectedFile) {
        pendingScrollRestoreRef.current = null;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedFile, loadingEntries, entries]);

  useEffect(() => {
    if (!selectedFile) {
      pendingListEnterRef.current = false;
      setListEntering(false);
      if (listEnterTimerRef.current) {
        clearTimeout(listEnterTimerRef.current);
        listEnterTimerRef.current = null;
      }
      return;
    }
    if (loadingEntries || !pendingListEnterRef.current) return;
    if (listEnterTimerRef.current) {
      clearTimeout(listEnterTimerRef.current);
    }
    pendingListEnterRef.current = false;
    setListEntering(true);
    listEnterTimerRef.current = setTimeout(() => {
      setListEntering(false);
      listEnterTimerRef.current = null;
    }, 300);
    return () => {
      if (listEnterTimerRef.current) {
        clearTimeout(listEnterTimerRef.current);
        listEnterTimerRef.current = null;
      }
    };
  }, [selectedFile, loadingEntries]);

  // Scroll to entry after jumping from search result
  useEffect(() => {
    if (scrollToIndex === null || loadingEntries) return;
    // Small delay to ensure DOM is rendered
    const timer = setTimeout(() => {
      const scope = listRef.current ?? document;
      const el = scope.querySelector(`[data-cache-index="${scrollToIndex}"]`);
      if (el) {
        el.scrollIntoView({ behavior: 'instant', block: 'center' });
        if (selectedFile && listRef.current) {
          scrollPositionsRef.current.set(selectedFile, listRef.current.scrollTop);
        }
        el.classList.add('cache-card--highlight');
        setTimeout(() => el.classList.remove('cache-card--highlight'), 2000);
      }
      setScrollToIndex(null);
    }, 100);
    return () => clearTimeout(timer);
  }, [scrollToIndex, loadingEntries, selectedFile]);

  // Auto-search with debounce
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      setSearchTotal(0);
      setSelectedSearchIdx(-1);
      return;
    }
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => {
      void runGlobalSearch();
    }, 400);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [runGlobalSearch, searchQuery]);

  // Scroll selected search result into view
  useEffect(() => {
    if (selectedSearchIdx < 0 || !searchResultsRef.current) return;
    const el = searchResultsRef.current.querySelector(`[data-search-idx="${selectedSearchIdx}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedSearchIdx]);

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
      setLocalError(normalizeError(err, '保存缓存失败'));
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
        lastError = normalizeError(err, `保存 ${file} 失败`);
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

  // Load problems when switching to problems tab
  const loadProblems = useCallback(async () => {
    if (!projectId) return;
    setLoadingProblems(true);
    try {
      const res = await fetchProjectProblems(projectId);
      setProblems(res.problems);
    } catch (err) {
      setLocalError(normalizeError(err, '加载问题列表失败'));
    } finally {
      setLoadingProblems(false);
    }
  }, [projectId]);

  const refreshVisibleData = useCallback(async () => {
    if (!projectId) return;
    await Promise.allSettled([
      loadCacheFiles(),
      runGlobalSearch(),
      loadProblems(),
      refreshCurrentFile(),
    ]);
  }, [loadCacheFiles, loadProblems, projectId, refreshCurrentFile, runGlobalSearch]);

  useEffect(() => {
    const wasActive = activeRef.current;
    activeRef.current = active;
    if (!active || wasActive === active) return;
    void refreshVisibleData();
  }, [active, refreshVisibleData]);

  // Close retransl popover on outside click / Escape
  useEffect(() => {
    if (!retranslEditor) return;
    const onPointerDown = (e: MouseEvent) => {
      const pop = retranslPopoverRef.current;
      if (!pop) return;
      if (pop.contains(e.target as Node)) return;
      // Ignore the +/toggle button so it can toggle the popover itself
      if ((e.target as HTMLElement).closest?.('.cache-problems-group__retransl')) return;
      setRetranslEditor(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setRetranslEditor(null);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [retranslEditor]);

  // Problems grouped by type
  const problemStats = useMemo(() => {
    const stats: Record<string, ProblemEntry[]> = {};
    for (const p of problems) {
      // 一个句子的 problem 字段可能包含多个以 ", " 分隔的问题，需分别计入各自类型
      const rawProblems = String(p.problem || '')
        .split(/,\s*/)
        .map((s) => s.trim())
        .filter(Boolean);
      const types = new Set<string>();
      for (const item of rawProblems) {
        const type = item.split('：')[0].trim();
        if (type) types.add(type);
      }
      if (types.size === 0) continue;
      for (const type of types) {
        if (!stats[type]) stats[type] = [];
        stats[type].push(p);
      }
    }
    return Object.entries(stats).sort((a, b) => b[1].length - a[1].length) as [string, ProblemEntry[]][];
  }, [problems]);

  // Jump from problem to search tab
  const handleProblemClick = useCallback((problemType: string) => {
    setSidebarTab('search');
    setSearchField('problem');
    setSearchQuery(problemType);
  }, []);

  // Add problem keyword to retranslKey config
  const handleAddToRetranslKey = useCallback(async (keyword: string) => {
    if (!projectId || !configFileName) return;
    try {
      const res = await fetchProjectConfig(projectId, configFileName);
      const config = res.config;
      const common = (config.common as Record<string, unknown>) || {};
      const existingKeys: string[] = Array.isArray(common.retranslKey) ? common.retranslKey : [];
      if (existingKeys.includes(keyword)) {
        setInfo(`「${keyword}」已在重翻关键字列表中`);
        return;
      }
      common.retranslKey = [...existingKeys, keyword];
      config.common = common;
      await updateProjectConfig(projectId, { config, config_file_name: configFileName });
      setInfo(`已将「${keyword}」加入重翻关键字`);
    } catch (err) {
      setLocalError(normalizeError(err, '添加重翻关键字失败'));
    }
  }, [projectId, configFileName]);

  const handleSelectFile = (file: string) => {
    if (file === selectedFile) return;
    // 先保存当前文件的修改到 entriesMap
    if (selectedFile) {
      entriesMapRef.current.set(selectedFile, entries);
    }
    prepareFileSwitch(file);
    const cachedEntries = entriesMapRef.current.get(file);
    pendingListEnterRef.current = true;
    setListEntering(false);
    if (listEnterTimerRef.current) {
      clearTimeout(listEnterTimerRef.current);
      listEnterTimerRef.current = null;
    }
    setEntries(cachedEntries ?? []);
    setLoadingEntries(!cachedEntries);
    setSelectedFile(file);
    setLocalError(null);
    setInfo(null);
  };

  /** 删除选中的缓存文件 */
  const handleDeleteSelectedFiles = useCallback(async (filenames: string[]) => {
    if (!projectId || filenames.length === 0) return;
    const msg = filenames.length === 1
      ? `确定要删除缓存文件「${filenames[0]}」吗？此操作不可撤销。`
      : `确定要删除 ${filenames.length} 个缓存文件吗？此操作不可撤销。`;
    if (!confirm(msg)) return;
    try {
      const res = await deleteCacheFiles(projectId, filenames);
      // 清除已删除文件的 entriesMap 和 dirtyFiles
      for (const f of res.deleted_files) {
        entriesMapRef.current.delete(f);
      }
      setDirtyFiles((prev) => {
        const next = new Set(prev);
        for (const f of res.deleted_files) next.delete(f);
        return next;
      });
      // 如果当前打开的文件被删除，清空编辑区
      if (selectedFile && res.deleted_files.includes(selectedFile)) {
        setSelectedFile(null);
        setEntries([]);
      }
      setSelectedFiles((prev) => {
        const next = new Set(prev);
        for (const f of res.deleted_files) next.delete(f);
        return next;
      });
      setInfo(`已删除 ${res.deleted_files.length} 个缓存文件`);
      // 刷新文件列表
      void loadCacheFiles();
    } catch (err) {
      setLocalError(normalizeError(err, '删除缓存文件失败'));
    }
  }, [projectId, selectedFile, loadCacheFiles]);

  // Close context menu on outside click / Escape
  useEffect(() => {
    if (!contextMenu) return;
    const onClick = (e: MouseEvent) => {
      const menuEl = contextMenuRef.current;
      if (menuEl && menuEl.contains(e.target as Node)) return;
      setContextMenu(null);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setContextMenu(null); };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [contextMenu]);

  // Ctrl+S: save current file; Ctrl+Shift+S: save all dirty files
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 's') return;
      e.preventDefault();
      if (e.shiftKey) {
        if (!savingAll && dirtyFiles.size > 0) {
          void handleSaveAll();
        }
        return;
      }
      if (!saving && selectedFile && dirtyFiles.has(selectedFile)) {
        void handleSave();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [saving, savingAll, selectedFile, dirtyFiles, handleSave, handleSaveAll]);

  // Ctrl+A handler for file list
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (sidebarTab !== 'files') return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        // Only intercept if the file list area is focused / active
        const active = document.activeElement;
        const fileListEl = document.querySelector('.cache-file-list');
        if (!fileListEl) return;
        if (!fileListEl.contains(active) && active !== fileListEl) return;
        e.preventDefault();
        setSelectedFiles(new Set(cacheFiles.map((f) => f.name)));
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [sidebarTab, cacheFiles]);

  // Jump from search result to file editor
  const handleJumpToFile = (filename: string, index: number) => {
    if (filename === selectedFile) {
      setScrollToIndex(index);
      return;
    }
    if (selectedFile) {
      entriesMapRef.current.set(selectedFile, entries);
    }
    prepareFileSwitch(filename);
    const cachedEntries = entriesMapRef.current.get(filename);
    pendingListEnterRef.current = true;
    setListEntering(false);
    if (listEnterTimerRef.current) {
      clearTimeout(listEnterTimerRef.current);
      listEnterTimerRef.current = null;
    }
    setEntries(cachedEntries ?? []);
    setLoadingEntries(!cachedEntries);
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
      setLocalError(normalizeError(err, '替换预览失败'));
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
      setInfo(`已替换 ${res.total_matches} 处（涉及 ${res.total_files} 个文件），请保存后生效`);
      // 将后端返回的修改后 entries 存入 entriesMap，并标记所有受影响文件为 dirty
      const affectedFiles: string[] = [];
      for (const fd of res.file_details) {
        if (fd.entries) {
          entriesMapRef.current.set(fd.filename, fd.entries);
          affectedFiles.push(fd.filename);
        }
      }
      if (affectedFiles.length > 0) {
        setDirtyFiles((prev) => {
          const next = new Set(prev);
          for (const f of affectedFiles) next.add(f);
          return next;
        });
      }
      // 如果当前文件被替换，刷新显示
      if (selectedFile && affectedFiles.includes(selectedFile)) {
        const modifiedEntries = entriesMapRef.current.get(selectedFile);
        if (modifiedEntries) setEntries(modifiedEntries);
      }
      // Refresh search if query was set
      if (searchQuery.trim()) {
        await runGlobalSearch();
      }
    } catch (err) {
      setLocalError(normalizeError(err, '全局替换失败'));
    } finally {
      setReplacing(false);
    }
  };

  if (loading && cacheFiles.length === 0) {
    return (
      <div className="project-cache-page" style={cacheBrowserFontStyle}>
        <PageHeader className="project-cache-page__header" title="缓存与问题" />
        <LoadingState title="加载缓存列表中…" description="正在读取项目缓存文件。" />
      </div>
    );
  }
  return (
    <div className="project-cache-page" style={cacheBrowserFontStyle}>
      <PageHeader
        className="project-cache-page__header"
        title="缓存与问题"
        description="在这里可以浏览翻译问题、手动润色，或通过删除缓存句触发部分重翻。最终结果将基于这些缓存来构建。"
        actions={cacheDir ? (
          <Button variant="secondary" onClick={() => void invoke('open_folder', { path: cacheDir })} title={cacheDir}>
            📂 打开缓存文件夹
          </Button>
        ) : null}
        status={
          <>
            {error && <InlineFeedback tone="error" title="加载缓存失败" description={error} />}
            {localError && <InlineFeedback tone="error" title="操作失败" description={localError} />}
            {info && <InlineFeedback className="inline-alert--floating" tone="success" title="操作成功" description={info} onDismiss={() => setInfo(null)} />}
          </>
        }
      />

      <div
        className={`cache-layout${resizing ? ' cache-layout--resizing' : ''}`}
        ref={layoutRef}
      >
        <aside className="cache-layout__sidebar" style={{ width: sidebarWidth }}>
          {/* Tab bar */}
          <div className="cache-sidebar-tabs">
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'files' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => setSidebarTab('files')}
            >
              文件
            </button>
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'search' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => setSidebarTab('search')}
            >
              搜索
            </button>
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'problems' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => { setSidebarTab('problems'); void loadProblems(); }}
            >
              问题{problems.length > 0 ? <span className="cache-sidebar-tab__badge">{problems.length}</span> : ''}
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
                  <button
                    type="button"
                    className={`icon-btn icon-btn--refresh${refreshingFiles ? ' icon-btn--spinning' : ''}`}
                    onClick={() => void loadCacheFiles()}
                    disabled={refreshingFiles}
                    title="刷新缓存文件列表"
                    aria-label="刷新缓存文件列表"
                  >
                    <svg viewBox="0 0 16 16" width="15" height="15" fill="none">
                      <path d="M13.5 8a5.5 5.5 0 11-1.4-3.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      <path d="M12 2v3.5H8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                </div>
              </div>
              {selectedFiles.size > 0 && (
                <div className="cache-file-list__selection-bar">
                  <span className="cache-file-list__selection-count">已选择 {selectedFiles.size} 个文件</span>
                  <div className="cache-file-list__selection-actions">
                    <button
                      type="button"
                      className="cache-file-list__selection-delete"
                      onClick={() => void handleDeleteSelectedFiles(Array.from(selectedFiles))}
                    >
                      删除
                    </button>
                    <button
                      type="button"
                      className="cache-file-list__selection-clear"
                      onClick={() => setSelectedFiles(new Set())}
                    >
                      取消选择
                    </button>
                  </div>
                </div>
              )}
              <div
                className="cache-file-list"
                tabIndex={0}
                onKeyDown={(e) => {
                  if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
                    e.preventDefault();
                    setSelectedFiles(new Set(cacheFiles.map((f) => f.name)));
                  }
                }}
              >
                {cacheFiles.map((file) => {
                  const isSelected = selectedFiles.has(file.name);
                  return (
                    <button
                      type="button"
                      key={file.name}
                      className={`cache-file-item ${selectedFile === file.name ? 'cache-file-item--active' : ''} ${dirtyFiles.has(file.name) ? 'cache-file-item--dirty' : ''} ${isSelected ? 'cache-file-item--selected' : ''}`}
                      onClick={(e) => {
                        if (e.ctrlKey || e.metaKey) {
                          // Ctrl+click: toggle selection
                          setSelectedFiles((prev) => {
                            const next = new Set(prev);
                            if (next.has(file.name)) next.delete(file.name);
                            else next.add(file.name);
                            return next;
                          });
                        } else if (e.shiftKey && selectedFile) {
                          // Shift+click: range select from active file
                          const activeIdx = cacheFiles.findIndex((f) => f.name === selectedFile);
                          const clickIdx = cacheFiles.findIndex((f) => f.name === file.name);
                          if (activeIdx >= 0 && clickIdx >= 0) {
                            const [from, to] = activeIdx < clickIdx ? [activeIdx, clickIdx] : [clickIdx, activeIdx];
                            const rangeNames = cacheFiles.slice(from, to + 1).map((f) => f.name);
                            setSelectedFiles(new Set(rangeNames));
                          }
                        } else {
                          // Normal click: select file for editing, clear multi-select
                          setSelectedFiles(new Set());
                          handleSelectFile(file.name);
                        }
                      }}
                      onContextMenu={(e) => {
                        e.preventDefault();
                        // 右键仅决定本次菜单作用目标，避免触发选择栏插入导致菜单视觉错位
                        const targetFiles = isSelected ? Array.from(selectedFiles) : [file.name];
                        setContextMenu({
                          x: e.clientX,
                          y: e.clientY,
                          filenames: targetFiles,
                          showDelete: true,
                        });
                      }}
                    >
                      <span className="cache-file-item__name">
                        {dirtyFiles.has(file.name) && <span className="cache-file-item__dot" title="有未保存修改" />}
                        {file.name}
                      </span>
                      <span className="cache-file-item__size">{file.entry_count != null ? `${file.entry_count} 行` : formatSize(file.size)}</span>
                    </button>
                  );
                })}
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
                <CustomSelect
                  className="cache-search-field"
                  value={searchField}
                  onChange={(e) => setSearchField(e.target.value as CacheSearchField)}
                >
                  <option value="all">全部</option>
                  <option value="src">仅原文</option>
                  <option value="dst">仅译文</option>
                  <option value="problem">仅问题</option>
                </CustomSelect>
              </div>

              {/* Replace toggle + Search results summary */}
              <div className="cache-search-meta">
                <button
                  type="button"
                  className="cache-replace-toggle__btn"
                  onClick={() => { setShowReplace(!showReplace); setReplaceQuery(searchQuery); }}
                  title={showReplace ? '隐藏替换' : '显示替换'}
                >
                  {showReplace ? '▾ 替换' : '▸ 替换'}
                </button>
                {searching && <span className="cache-search-status">搜索中…</span>}
                {!searching && searchQuery.trim() && (
                  <span className="cache-search-status">
                    {searchTotal > 0 ? `${searchTotal} 条结果` : '无匹配结果'}
                  </span>
                )}
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
                  <CustomSelect
                    className="cache-search-field"
                    value={replaceField}
                    onChange={(e) => setReplaceField(e.target.value as CacheReplaceField)}
                  >
                    <option value="dst">译文</option>
                    <option value="src">原文</option>
                    <option value="all">全部</option>
                  </CustomSelect>
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

              {/* Search results list */}
              <div
                className="cache-search-results"
                ref={searchResultsRef}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    setSelectedSearchIdx((i) => {
                      const next = Math.min(i + 1, searchResults.length - 1);
                      if (next >= 0 && next !== i) {
                        handleJumpToFile(searchResults[next].filename, searchResults[next].index);
                      }
                      return next;
                    });
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    setSelectedSearchIdx((i) => {
                      const next = Math.max(i - 1, 0);
                      if (next !== i) {
                        handleJumpToFile(searchResults[next].filename, searchResults[next].index);
                      }
                      return next;
                    });
                  }
                }}
              >
                {searchResults.map((r, idx) => (
                  <SearchResultCard
                    key={`${r.filename}-${r.index}`}
                    result={r}
                    query={searchQuery.trim()}
                    onJumpToFile={handleJumpToFile}
                    nameDict={nameDict}
                    selected={idx === selectedSearchIdx}
                    onSelect={() => setSelectedSearchIdx(idx)}
                    onContextMenu={(e) => {
                      setContextMenu({
                        x: e.clientX,
                        y: e.clientY,
                        filenames: [r.filename],
                        showDelete: false,
                      });
                    }}
                    idx={idx}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Tab: Problems */}
          {sidebarTab === 'problems' && (
            <div className="cache-problems-panel">
              <div className="cache-problems-hint">点击+号加入重翻关键字</div>
              {loadingProblems ? (
                <div className="cache-problems-loading">加载问题中…</div>
              ) : problems.length === 0 ? (
                <div className="cache-problems-empty">暂未发现问题</div>
              ) : (
                <div className="cache-problems-groups">
                  {problemStats.map(([type, items]) => (
                    <div key={type} className="cache-problems-group">
                      <div
                        className="cache-problems-group__header"
                        onClick={() => handleProblemClick(type)}
                        title={`点击搜索「${type}」类型问题`}
                      >
                        <span className="cache-problems-group__summary">
                          <span className="cache-problems-group__type">{type}</span>
                        </span>
                        <span className="cache-problems-group__count">{items.length}</span>
                        <button
                          type="button"
                          className={`cache-problems-group__retransl${retranslEditor?.type === type ? ' cache-problems-group__retransl--active' : ''}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                            const anchor = {
                              top: rect.top + rect.height / 2,
                              left: rect.right + 10 };
                            setRetranslEditor((cur) => (cur && cur.type === type ? null : { type, draft: type, anchor }));
                          }}
                          title={`编辑并加入重翻关键字`}
                          aria-label={`编辑并加入「${type}」到重翻关键字`}
                          aria-expanded={retranslEditor?.type === type}
                        >
                          +
                        </button>
                        {retranslEditor?.type === type && createPortal((
                          <div
                            ref={retranslPopoverRef}
                            className="retransl-popover"
                            role="dialog"
                            aria-label="编辑重翻关键字"
                            onClick={(e) => e.stopPropagation()}
                            style={{ top: retranslEditor.anchor.top, left: retranslEditor.anchor.left }}
                          >
                            <div className="retransl-popover__arrow" aria-hidden="true" />
                            <label className="retransl-popover__label">加入重翻关键字</label>
                            <input
                              ref={retranslInputRef}
                              type="text"
                              className="retransl-popover__input"
                              value={retranslEditor.draft}
                              onChange={(e) => setRetranslEditor((cur) => (cur ? { ...cur, draft: e.target.value } : cur))}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                  e.preventDefault();
                                  const kw = retranslEditor.draft.trim();
                                  if (!kw) return;
                                  setRetranslEditor(null);
                                  void handleAddToRetranslKey(kw);
                                } else if (e.key === 'Escape') {
                                  e.preventDefault();
                                  setRetranslEditor(null);
                                }
                              }}
                              placeholder="关键字"
                              autoFocus
                            />
                            <div className="retransl-popover__actions">
                              <button
                                type="button"
                                className="retransl-popover__btn retransl-popover__btn--ghost"
                                onClick={() => setRetranslEditor(null)}
                              >
                                取消
                              </button>
                              <button
                                type="button"
                                className="retransl-popover__btn retransl-popover__btn--primary"
                                disabled={!retranslEditor.draft.trim()}
                                onClick={() => {
                                  const kw = retranslEditor.draft.trim();
                                  if (!kw) return;
                                  setRetranslEditor(null);
                                  void handleAddToRetranslKey(kw);
                                }}
                              >
                                加入
                              </button>
                            </div>
                          </div>
                        ), document.body)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </aside>

        <div
          className={`cache-layout__resizer${resizing ? ' cache-layout__resizer--dragging' : ''}`}
          onPointerDown={handleResizerPointerDown}
          role="separator"
          aria-orientation="vertical"
          aria-label="调整文件列表宽度"
        />

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
                <div
                  ref={listRef}
                  onScroll={rememberCurrentScrollPosition}
                  className={`cache-card-list ${loadingEntries ? 'cache-card-list--loading' : ''} ${listEntering ? 'cache-card-list--entering' : ''}`}
                >
                  {filteredEntries.map((entry) => (
                    <CacheEntryCard
                      key={`${selectedFile}-${entry.index}`}
                      entry={entry}
                      filename={selectedFile}
                      projectId={projectId}
                      onEntryChange={handleEntryChange}
                      onDelete={handleDelete}
                      highlightQuery={searchTerm || searchQuery}
                      nameDict={nameDict}
                    />
                  ))}
                  {filteredEntries.length === 0 && !loadingEntries && (
                    <EmptyState title="无匹配条目" description="尝试更换搜索关键词。" />
                  )}
                </div>
              </div>
            </Panel>
          ) : (
            <EmptyState className="cache-layout__empty" title="选择一个缓存文件" description="从左侧选择缓存文件查看翻译内容，或使用全局搜索。" />
          )}
        </div>
      </div>
      {contextMenu && createPortal(
        <div
          ref={contextMenuRef}
          className="cache-context-menu"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            className="cache-context-menu__item"
            onClick={() => {
              const filenames = contextMenu.filenames;
              setContextMenu(null);
              for (const f of filenames) {
                const fullPath = cacheDir ? `${cacheDir}/${f}` : f;
                void invoke('reveal_file', { path: fullPath });
              }
            }}
          >
            <span className="cache-context-menu__icon" aria-hidden="true">📂</span>
            <span className="cache-context-menu__label">在文件管理器中浏览</span>
          </button>
          {contextMenu.showDelete && (
            <button
              type="button"
              className="cache-context-menu__item cache-context-menu__item--danger"
              onClick={() => {
                const files = contextMenu.filenames;
                setContextMenu(null);
                void handleDeleteSelectedFiles(files);
              }}
            >
              <span className="cache-context-menu__icon" aria-hidden="true">🗑</span>
              <span className="cache-context-menu__label">
                删除{contextMenu.filenames.length > 1 ? ` (${contextMenu.filenames.length} 个文件)` : ''}
              </span>
            </button>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

