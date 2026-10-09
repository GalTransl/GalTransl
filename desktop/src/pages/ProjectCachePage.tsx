import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useRetainPage } from '../components/PageActivity';
import { createPortal } from 'react-dom';
import { useSearchParams } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import { Icon } from '../components/Icon';
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
  type CacheSearchOptions,
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
import type { LocalizedText } from '../i18n/core';
import { escapeProblemFilterPattern, filterProblemText, normalizeKeywordList, splitProblemItems, splitProblemTypes } from '../lib/problemFilter';
import { joinPath } from '../lib/paths';

/** 兼容读取缓存字段：优先新key，回退旧key */
function src(e: CacheEntry): string { return e.post_src || e.post_jp || ''; }
function dst(e: CacheEntry): string { return e.pre_dst || e.pre_zh || ''; }
function cloneEntries(entries: CacheEntry[]): CacheEntry[] {
  return entries.map((entry) => ({
    ...entry,
    ...(Array.isArray(entry.name) ? { name: [...entry.name] } : {}),
  }));
}
function entriesMatch(left: CacheEntry[], right: CacheEntry[]): boolean {
  const normalize = (items: CacheEntry[]) => items.map((entry) => ({ ...entry, deleted: !!entry.deleted }));
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
}
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
  const uiLanguage = useUiLanguage();
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
  onAddProblemFilter,
  highlightQuery,
  nameDict,
  readOnly = false }: {
  entry: CacheEntry;
  filename: string;
  projectId: string;
  onEntryChange: (index: number, field: keyof CacheEntry, value: string | boolean) => void;
  onDelete: (deleteMode: boolean, index: number) => void;
  onAddProblemFilter: (keyword: string) => void;
  highlightQuery?: string;
  nameDict: Map<string, string>;
  /** 没有缓存文件（读的是原文）时只读：改了也没地方存，后端保存时会报缓存文件不存在 */
  readOnly?: boolean;
}) {
  const uiLanguage = useUiLanguage();
  const hasProblem = !!entry.problem;
  const rawSpeaker = Array.isArray(entry.name) ? entry.name.join('/') : entry.name || '—';
  const speaker = rawSpeaker !== '—'
    ? (Array.isArray(entry.name)
        ? entry.name.map((s) => resolveSpeakerName(s, nameDict)).join('/')
        : resolveSpeakerName(rawSpeaker, nameDict))
    : rawSpeaker;
  const [expanded, setExpanded] = useState(false);

  return (
    <article className={`cache-card ${hasProblem ? 'cache-card--problem' : ''} ${entry.deleted ? 'cache-card--pre-deleted' : ''}`} data-cache-index={entry.index}>
      <div className="cache-card__row">
        <span className="cache-card__field-label">#{entry.index}</span>
        {speaker !== '—' && (
          <span className="cache-card__pill cache-card__pill--speaker" style={speakerStyle(rawSpeaker)}>{speaker}</span>
        )}
        {hasProblem && (
          <div className="cache-card__problem-slot">
            {splitProblemItems(entry.problem).map((problemItem, itemIndex) => (
              <span key={`${problemItem}-${itemIndex}`} className="cache-card__problem-item">
                <span className="cache-card__pill cache-card__pill--problem" title={problemItem}>{problemItem}</span>
                <button
                  type="button"
                  className="cache-card__problem-filter"
                  title={translate("projects:projectCachePage.cacheCardProblemFilter_title_filter", { problemItem: problemItem })}
                  aria-label={translate("projects:projectCachePage.cacheCardProblemFilter_ariaLabel_filter", { problemItem: problemItem })}
                  onClick={(event) => {
                    event.stopPropagation();
                    // 过滤项是正则：这一条按字面过滤，先转义（否则 ( ) . * 这些会被当元字符）
                    onAddProblemFilter(escapeProblemFilterPattern(problemItem));
                  }}
                >
                  -
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="cache-card__spacer" />
        {entry.skip_check && (
          <span className="cache-card__pill cache-card__pill--skip-check" title={translate("projects:projectCachePage.cacheCardPillCacheCardPillSkipCheck_title_doneProblemCheck")}>⏭</span>
        )}
        {entry.trans_by && (
          <span className="cache-card__pill cache-card__pill--engine">{entry.trans_by}</span>
        )}
        <button
          type="button"
          className="cache-card__expand"
          onClick={() => setExpanded(!expanded)}
          title={expanded ? translate("projects:projectCachePage.cacheCardExpand_title_text") : translate("projects:projectCachePage.cacheCardExpand_title_textVariant2")}
        >
          {expanded ? <Icon name="chevron-down" /> : <Icon name="chevron-right" />}
        </button>
        <button
          type="button"
          className="cache-card__delete"
          onClick={() => onDelete(!entry.deleted, entry.index)}
          disabled={readOnly}
          title={readOnly ? translate("projects:projectCachePage.cacheCardDelete_title_emptyCacheFileCannotDeleteEntry") : (entry.deleted ? translate("projects:projectCachePage.cacheCardDelete_title_delete") : translate("projects:projectCachePage.cacheCardDelete_title_deleteEntry"))}
        >
          {entry.deleted ? <Icon name="undo" /> : <Icon name="close" />}
        </button>
      </div>

      <div className="cache-card__fields">
        {/* 折叠态：原文 + 译文 */}
        {!expanded && (
          <>
            <div className="cache-card__field">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardField_message_source")}</span>
              <div className="cache-card__input-wrap">
                <span className="cache-card__readonly-input" title={escapeControlChars(src(entry))}>
                  {highlightQuery
                    ? <HighlightText text={escapeControlChars(src(entry))} query={highlightQuery} />
                    : escapeControlChars(src(entry))}
                </span>
              </div>
            </div>
            <div className="cache-card__field">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardField_message_translationText")}</span>
              <div className="cache-card__input-wrap">
                <input
                  className="cache-card__input cache-card__input--zh"
                  value={escapeControlChars(dst(entry))}
                  onChange={(e) => onEntryChange(entry.index, 'pre_dst', unescapeControlChars(e.target.value))}
                  placeholder={readOnly ? translate("projects:projectCachePage.cacheCardInputWrap_placeholder_notTranslation") : translate("projects:projectCachePage.cacheCardInputWrap_placeholder_translationText")}
                  title={readOnly ? translate("projects:projectCachePage.cacheCardInputWrap_title_emptyCacheFileSource") : escapeControlChars(dst(entry))}
                  disabled={readOnly}
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
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_message_preSrc")}</span>
              <div className="cache-card__readonly-textarea">
                {escapeControlChars(entry.pre_src || '')}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_message_postSrc")}</span>
              <div className="cache-card__readonly-textarea">
                {highlightQuery
                  ? <HighlightText text={escapeControlChars(src(entry))} query={highlightQuery} />
                  : escapeControlChars(src(entry))}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_message_preDst")}</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={escapeControlChars(entry.pre_dst || entry.pre_zh || '')}
                onChange={(e) => onEntryChange(entry.index, 'pre_dst', unescapeControlChars(e.target.value))}
                placeholder={translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_placeholder_translation")}
                rows={3}
                disabled={readOnly}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_message_proofread")}</span>
              <textarea
                className="cache-card__textarea cache-card__textarea--zh"
                value={escapeControlChars(entry.proofread_dst || entry.proofread_zh || '')}
                onChange={(e) => onEntryChange(entry.index, 'proofread_dst', unescapeControlChars(e.target.value))}
                placeholder={translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_placeholder_text")}
                rows={3}
                disabled={readOnly}
              />
            </div>
            <div className="cache-card__field cache-card__field--textarea">
              <span className="cache-card__field-label">{translate("projects:projectCachePage.cacheCardFieldCacheCardFieldTextarea_message_preview")}</span>
              <div className="cache-card__readonly-textarea">
                {escapeControlChars(entry.post_dst_preview || entry.post_zh_preview || '')}
              </div>
            </div>
            <div className="cache-card__field cache-card__field--skip-check">
              <label className="cache-card__checkbox-label">
                <input
                  type="checkbox"
                  checked={!!entry.skip_check}
                  onChange={(e) => onEntryChange(entry.index, 'skip_check', e.target.checked)}
                  disabled={readOnly}
                />
                <span>{translate("projects:projectCachePage.cacheCardCheckboxLabel_message_checkSkipCheck")}</span>
              </label>
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
  const uiLanguage = useUiLanguage();
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
      title={translate("projects:projectCachePage.searchResultCard_title_text", { filename: result.filename, index: result.index })}
    >
      <div className="search-result-card__header">
        {(result.match_src || result.match_dst || result.match_problem) && (
          <span className="search-result-card__match-badges">
            {result.match_src && <span className="search-result-card__badge search-result-card__badge--src">{translate("projects:projectCachePage.searchResultCardMatchBadges_message_source")}</span>}
            {result.match_dst && <span className="search-result-card__badge search-result-card__badge--dst">{translate("projects:projectCachePage.searchResultCardMatchBadges_message_translationText")}</span>}
            {result.match_problem && <span className="search-result-card__badge search-result-card__badge--problem">{translate("projects:projectCachePage.searchResultCardMatchBadges_message_problem")}</span>}
          </span>
        )}
        <span className="search-result-card__file">{result.filename}</span>
        {result.has_cache === false ? (
          <span className="search-result-card__badge search-result-card__badge--uncached" title={translate("projects:projectCachePage.searchResultCardBadgeSearchResultCardBadgeUncached_title_countFileTranslationSource")}>{translate("projects:projectCachePage.searchResultCardHeader_message_notTranslation")}</span>
        ) : null}
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
          <span className="search-result-card__label">{translate("projects:projectCachePage.searchResultCardLine_message_source")}</span>
          <span className="search-result-card__text" title={escapeControlChars(result.post_src)}><HighlightText text={escapeControlChars(result.post_src)} query={query} /></span>
        </div>
      )}
      {result.pre_dst && (
        <div className="search-result-card__line">
          <span className="search-result-card__label">{translate("projects:projectCachePage.searchResultCardLine_message_translationText")}</span>
          <span className="search-result-card__text search-result-card__text--dst" title={escapeControlChars(result.pre_dst)}><HighlightText text={escapeControlChars(result.pre_dst)} query={query} /></span>
        </div>
      )}
    </button>
  );
}

/* ── Main Page ── */
export function ProjectCachePage({ ctx, active = true }: { ctx: ProjectPageContext; active?: boolean }) {
  const uiLanguage = useUiLanguage();
  const { projectId, configFileName } = ctx;
  const { nameDict } = useNameDict(projectId);
  const [cacheBrowserFontSize, setCacheBrowserFontSize] = useState(() => getCacheBrowserFontSizePreference());

  const [cacheFiles, setCacheFiles] = useState<FileEntry[]>([]);
  const [cacheDir, setCacheDir] = useState<string>('');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  /**
   * 还没有缓存的输入文件（名单里的名字是它们「本该有」的缓存键）。
   * 这些文件在后端是按原文回落的：只有 pre_src/post_src、译文为空，所以界面里不让编辑。
   */
  const [uncachedFiles, setUncachedFiles] = useState<Set<string>>(new Set());
  const [entries, setEntries] = useState<CacheEntry[]>([]);
  /** 每个文件的条目缓存（含未保存修改），mount 后指向当前项目桶中的 Map */
  const entriesMapRef = useRef<Map<string, CacheEntry[]>>(new Map());
  /** 每个文件最近一次从后端读取或保存后的干净快照，用于准确维护 dirty 状态 */
  const cleanEntriesMapRef = useRef<Map<string, CacheEntry[]>>(new Map());
  /** 有未保存修改的文件集合 */
  const [dirtyFiles, setDirtyFiles] = useState<Set<string>>(new Set());

  /**
   * 按 projectId 分桶保存本页状态，跨项目切换时既不串数据、也不丢 dirty 编辑与选择。
   * 桶的内容会在 projectId 变化时先 snapshot 旧项目，再恢复或新建新项目的桶。
   */
  type ProjectBucket = {
    cacheFiles: FileEntry[];
    uncachedFiles: Set<string>;
    cacheDir: string;
    selectedFile: string | null;
    dirtyFiles: Set<string>;
    entries: Map<string, CacheEntry[]>;
    cleanEntries: Map<string, CacheEntry[]>;
    scrollPositions: Map<string, number>;
    sidebarTab: SidebarTab;
    searchQuery: string;
    searchField: CacheSearchField;
    searchOptions: CacheSearchOptions;
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
  /**
   * 「现在在看哪个项目」的同步镜像，切项目时由下面的状态桶 effect 更新。
   *
   * 本页是 keep-alive 的（切项目不重新挂载）：一个请求发出去之后用户可能已经切到别的项目了，
   * 而这时 state 与 entriesMapRef 都换成了新项目的。所以每个 await 回来、要写项目相关数据的
   * 地方都先问一句「还在看这个项目吗」，不在就整段丢掉——否则上个项目的文件列表会被写进当前
   * 项目（B 的文件出现在 A 里），条目更会串进 A 的 entriesMap。
   */
  const viewingProjectIdRef = useRef(projectId);
  /**
   * 切项目时的「选中项接力」。
   *
   * state 里的 selectedFile 要等下一拍才换成新项目的，而读条目的 effect 在切项目这一拍就带着
   * 新 projectId 跑了：直接用 state 那份，就是拿「新项目 + 上一个项目的文件名」去读文件——
   * 别的项目里没有同名文件就是 404（控制台里那条），同名则读到别人的内容。
   * 桶 effect 先于它运行，把新项目真正的选中项放这里交给它。
   */
  const pendingSelectionRef = useRef<{ projectId: string; file: string | null } | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshingFiles, setRefreshingFiles] = useState(false);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const entriesRequestRef = useRef(0);
  const viewingFileRef = useRef(selectedFile);
  viewingFileRef.current = selectedFile;
  const [error, setError] = useMessageState<string | null>(null);
  const activeRef = useRef(active);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterProblems, setFilterProblems] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingAll, setSavingAll] = useState(false);
  const [localError, setLocalError] = useMessageState<string | null>(null);
  const [info, setInfo] = useMessageState<string | null>(null);

  const handleRevealCacheFiles = useCallback(async (filenames: string[]) => {
    if (!cacheDir || filenames.length === 0) return;
    setLocalError(null);

    try {
      for (const filename of filenames) {
        await invoke('reveal_file', { path: joinPath(cacheDir, filename) });
      }
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleRevealCacheFiles_normalizeError_fileFailed")));
    }
  }, [cacheDir]);

  // Tab state
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>('files');
  /** ?q=xxx 带进来的搜索词（GPT 字典条目行的「→」），已经应用过的那次靠 ref 去重 */
  const [searchParams] = useSearchParams();
  const appliedCacheSearchRef = useRef('');

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
  const [searchOptions, setSearchOptions] = useState<CacheSearchOptions>({
    re: false
  });
  const [searchResults, setSearchResults] = useState<CacheSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchTotal, setSearchTotal] = useState(0);
  const [selectedSearchIdx, setSelectedSearchIdx] = useState(-1);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchResultsRef = useRef<HTMLDivElement>(null);

  // Problems tab state
  const [problems, setProblems] = useState<ProblemEntry[]>([]);
  const [loadingProblems, setLoadingProblems] = useState(false);
  const [problemFilterKeys, setProblemFilterKeys] = useState<string[]>([]);
  const [savingKeyword, setSavingKeyword] = useState(false);
  const problemRequestRef = useRef(0);
  // Retransl keyword popover editor
  const [retranslEditor, setRetranslEditor] = useState<{
    type: string;
    draft: string;
    action: 'retransl' | 'filter';
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
  useRetainPage(dirtyFiles.size > 0 || saving || savingAll || replacing);
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
  /** 当前文件还没有缓存：后端给的是原文（只有 pre_src/post_src），只能看不能改 */
  const selectedHasNoCache = selectedFile != null && uncachedFiles.has(selectedFile);

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
        const res = await fetchProjectCache(projectId, configFileName);
        // 已经切到别的项目：回来的是上一个项目的列表，写下去就是「B 的文件出现在 A 里」
        if (viewingProjectIdRef.current !== projectId) return;
        const cached = res.files.filter((f) => f.is_file && f.name.endsWith('.json'));
        // 还没翻译的文件（Cache/ 里没有对应缓存）也列进来：后端打开这类文件时回落读原文，
        // 不然刚建好项目的人在缓存页上什么都看不到。
        // 同名要去重：界面按 name 做 key，两个列表一旦重叠就是一串重复 key（后端也拦了，
        // 这里是最后一道）
        const cachedNames = new Set(cached.map((f) => f.name));
        const uncached = (res.uncached_files ?? []).filter(
          (f) => f.is_file && !cachedNames.has(f.name),
        );
        const files = [...cached, ...uncached].sort((a, b) => a.name.localeCompare(b.name));
        setCacheFiles(files);
        setUncachedFiles(new Set(uncached.map((f) => f.name)));
        setCacheDir(res.cache_dir || '');
        setSelectedFile((prev) => (prev && files.some((file) => file.name === prev) ? prev : null));
      } catch (err) {
        if (viewingProjectIdRef.current === projectId) {
          setError(normalizeError(err, uiMessage("projects:projectCachePage.loadCacheFiles_normalizeError_loadCacheFailed")));
        }
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
    [projectId, configFileName],
  );

  /**
   * 记录某个文件到底有没有缓存文件。
   * 列表接口给的是当时的快照，而缓存可能被 Agent（或别的工具）在页面打开期间删掉/建出来，
   * 所以每次读到文件内容都以后端这次实际给的结果为准。
   */
  const noteCachePresence = useCallback((filename: string, hasCache: boolean) => {
    setUncachedFiles((prev) => {
      if (prev.has(filename) === !hasCache) return prev;
      const next = new Set(prev);
      if (hasCache) next.delete(filename);
      else next.add(filename);
      return next;
    });
  }, []);

  // 按 projectId 切换状态桶：先 snapshot 旧项目，再恢复或新建新项目的桶。
  // 该 effect 是本页跨项目状态保留的核心入口。
  useEffect(() => {
    if (!projectId) return;
    // 先记下"现在在看哪个项目"：下面所有按项目发的请求都拿它判断自己是否已经过期
    viewingProjectIdRef.current = projectId;
    // 多选与右键菜单是「屏幕上这些文件」的临时状态，不跟着项目走：切项目后若留着上个项目
    // 的文件名，「删除」就会拿这些名字去删新项目里的同名缓存
    setSelectedFiles(new Set());
    setContextMenu(null);
    const prev = lastProjectIdRef.current;
    if (prev && prev !== projectId) {
      // snapshot 旧项目（此时 state 闭包仍是旧项目的数据，刚好用于写回）
      const prevBucket: ProjectBucket = bucketsRef.current.get(prev) ?? {
        cacheFiles: [],
        uncachedFiles: new Set(),
        cacheDir: '',
        selectedFile: null,
        dirtyFiles: new Set(),
        entries: new Map(),
        cleanEntries: new Map(),
        scrollPositions: new Map(),
        sidebarTab: 'files',
        searchQuery: '',
        searchField: 'all',
        searchOptions: { re: false },
        searchResults: [],
        searchTotal: 0,
        replaceQuery: '',
        replaceWith: '',
        replaceField: 'dst',
        showReplace: false,
      };
      prevBucket.cacheFiles = cacheFiles;
      prevBucket.uncachedFiles = uncachedFiles;
      prevBucket.cacheDir = cacheDir;
      prevBucket.selectedFile = selectedFile;
      prevBucket.dirtyFiles = dirtyFiles;
      prevBucket.entries = entriesMapRef.current;
      prevBucket.cleanEntries = cleanEntriesMapRef.current;
      prevBucket.scrollPositions = scrollPositionsRef.current;
      prevBucket.sidebarTab = sidebarTab;
      prevBucket.searchQuery = searchQuery;
      prevBucket.searchField = searchField;
      prevBucket.searchOptions = searchOptions;
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
      cleanEntriesMapRef.current = existing.cleanEntries;
      scrollPositionsRef.current = existing.scrollPositions;
      setCacheFiles(existing.cacheFiles);
      setUncachedFiles(existing.uncachedFiles);
      setCacheDir(existing.cacheDir);
      setSelectedFile(existing.selectedFile);
      setDirtyFiles(existing.dirtyFiles);
      setSidebarTab(existing.sidebarTab);
      setSearchQuery(existing.searchQuery);
      setSearchField(existing.searchField);
      setSearchOptions(existing.searchOptions);
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
      cleanEntriesMapRef.current = new Map();
      scrollPositionsRef.current = new Map();
      setCacheFiles([]);
      setUncachedFiles(new Set());
      setCacheDir('');
      setSelectedFile(null);
      setDirtyFiles(new Set());
      setEntries([]);
      setSidebarTab('files');
      setSearchQuery('');
      setSearchField('all');
      setSearchOptions({ re: false });
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
    // 交给下面的读条目 effect：它这一拍读到的 state 还是上一个项目的选中项
    pendingSelectionRef.current = {
      projectId,
      file: bucketsRef.current.get(projectId)?.selectedFile ?? null,
    };
    // 仅在 projectId 变化时运行；state 的 stale closure 正是我们需要快照的"旧值"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  /**
   * 从别处带搜索词跳进来（GPT 字典条目行的「→」）：切到搜索 tab 并预填那个词。
   *
   * 这个页面是 keep-alive 的（切走再回来不重新挂载），所以不能用 state 初值接参数；
   * 而且这个 effect 必须排在上面「按 projectId 切状态桶」之后——否则会被桶里恢复的
   * 旧 tab / 旧查询词盖掉。同一个词连点两次时 URL 不变，靠 nonce（n）分辨是新的一次点击。
   */
  useEffect(() => {
    const query = searchParams.get('q');
    if (query === null) return;
    const signature = `${searchParams.get('n') ?? ''}|${query}`;
    if (appliedCacheSearchRef.current === signature) return;
    appliedCacheSearchRef.current = signature;
    setSelectedFiles(new Set());
    setSidebarTab('search');
    setSearchQuery(query);
    // 词来自字典的原文列，但要不要只看原文列交给上面的下拉框；正则开关一并复位，
    // 免得上次留下的 re:true 把这个词当正则去解
    setSearchField('all');
    setSearchOptions({ re: false });
    // 不用手动搜：搜索自己的 debounce effect 盯着 searchQuery，改完会自动搜一次
  }, [searchParams]);

  useEffect(() => {
    if (!projectId) return;
    // 切项目这一拍用桶 effect 交接过来的选中项（state 里那份还停在旧项目，见 pendingSelectionRef）
    const request = ++entriesRequestRef.current;
    const handoff = pendingSelectionRef.current;
    pendingSelectionRef.current = null;
    const file = handoff && handoff.projectId === projectId ? handoff.file : selectedFile;
    if (!file) {
      setLoadingEntries(false);
      return;
    }
    // 如果 entriesMap 中有缓存（含未保存修改），直接使用
    const cached = entriesMapRef.current.get(file);
    if (cached) {
      setEntries(cached);
      setLoadingEntries(false);
      return;
    }
    let cancelled = false;
    setLoadingEntries(true);
    fetchCacheFile(projectId, file, configFileName)
      .then((res) => {
        if (!cancelled && request === entriesRequestRef.current) {
          noteCachePresence(file, res.has_cache !== false);
          setEntries(res.entries);
          entriesMapRef.current.set(file, res.entries);
          cleanEntriesMapRef.current.set(file, cloneEntries(res.entries));
        }
      })
      .catch((err) => {
        if (!cancelled && request === entriesRequestRef.current) setError(normalizeError(err, uiMessage("projects:projectCachePage.projectCachePage_normalizeError_loadCacheFailed")));
      })
      .finally(() => {
        if (!cancelled && request === entriesRequestRef.current) setLoadingEntries(false);
      });
    return () => { cancelled = true; };
  }, [projectId, selectedFile, configFileName, noteCachePresence]);

  const runGlobalSearch = useCallback(async () => {
    if (!projectId || !searchQuery.trim()) {
      setSearchResults([]);
      setSearchTotal(0);
      setSelectedSearchIdx(-1);
      return;
    }
    setSearching(true);
    try {
      const res = await searchCache(projectId, searchQuery.trim(), searchField, searchOptions, 500, configFileName);
      // 搜索期间切了项目：这一份结果是上一个项目的，别摆到新项目的搜索 tab 里
      if (viewingProjectIdRef.current !== projectId) return;
      setSearchResults(res.results);
      setSearchTotal(res.total);
      setSelectedSearchIdx(-1);
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.runGlobalSearch_normalizeError_searchFailed")));
      setSearchResults([]);
      setSearchTotal(0);
    } finally {
      setSearching(false);
    }
  }, [projectId, configFileName, searchField, searchQuery, searchOptions]);

  const refreshCurrentFile = useCallback(async () => {
    if (!projectId || !selectedFile || dirtyFiles.has(selectedFile)) return;
    const request = ++entriesRequestRef.current;
    const isCurrent = () => viewingProjectIdRef.current === projectId
      && viewingFileRef.current === selectedFile && request === entriesRequestRef.current;
    entriesMapRef.current.delete(selectedFile);
    cleanEntriesMapRef.current.delete(selectedFile);
    setLoadingEntries(true);
    setLocalError(null);
    try {
      const res = await fetchCacheFile(projectId, selectedFile, configFileName);
      // entriesMapRef 早换成新项目的表了：这时候写进去等于把 B 的条目塞进 A
      if (!isCurrent()) return;
      noteCachePresence(selectedFile, res.has_cache !== false);
      entriesMapRef.current.set(selectedFile, res.entries);
      cleanEntriesMapRef.current.set(selectedFile, cloneEntries(res.entries));
      setEntries(res.entries);
      setError(null);
    } catch (err) {
      if (isCurrent()) setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.refreshCurrentFile_normalizeError_cacheFailed")));
    } finally {
      if (isCurrent()) {
        setLoadingEntries(false);
      }
    }
  }, [dirtyFiles, projectId, configFileName, selectedFile, noteCachePresence]);

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

  // Immediate search (on search options change)
  useEffect(() => {
    if (!searchQuery.trim()) return;
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    void runGlobalSearch();
  }, [runGlobalSearch, searchOptions]);

  // Scroll selected search result into view
  useEffect(() => {
    if (selectedSearchIdx < 0 || !searchResultsRef.current) return;
    const el = searchResultsRef.current.querySelector(`[data-search-idx="${selectedSearchIdx}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedSearchIdx]);

  const visibleEntries = entries.map((entry) => ({
    ...entry, problem: filterProblemText(entry.problem, problemFilterKeys),
  }));
  const filteredEntries = visibleEntries.filter((e) => {
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
  const withProblems = visibleEntries.filter((e) => e.problem).length;

  const handleEntryChange = (index: number, field: keyof CacheEntry, value: string | boolean) => {
    const next = entries.map((e) => {
      if (e.index !== index) return e;
      const updated: CacheEntry = { ...e, [field]: value, deleted: false };
      // 勾选跳过检查时同步清除问题标记
      if (field === 'skip_check' && value === true) {
        updated.problem = '';
      }
      return updated;
    });
    setEntries(next);
    if (selectedFile) entriesMapRef.current.set(selectedFile, next);
    if (selectedFile) {
      const clean = cleanEntriesMapRef.current.get(selectedFile);
      setDirtyFiles((current) => {
        const updated = new Set(current);
        if (clean && entriesMatch(next, clean)) updated.delete(selectedFile);
        else updated.add(selectedFile);
        return updated;
      });
    }
    setInfo(null);
  };

  // 不实际删除，处理删除和恢复
  const handleDeleteAndRecover = (deleteMode: boolean, index: number) => {
    if (!selectedFile) return;
    const next = entries.map((e) => e.index === index ? { ...e, deleted: deleteMode } : e);
    setEntries(next);
    entriesMapRef.current.set(selectedFile, next);
    const clean = cleanEntriesMapRef.current.get(selectedFile);
    setDirtyFiles((current) => {
      const updated = new Set(current);
      if (clean && entriesMatch(next, clean)) updated.delete(selectedFile);
      else updated.add(selectedFile);
      return updated;
    });
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
      // 处理脏条目并重新索引
      const entriesToSave = targetEntries
        .filter(e => !e.deleted)
        .map(e => {
          const { deleted, ...rest } = e;
          return rest;
        });

      const res = await saveCacheFile(projectId, targetFile, entriesToSave, configFileName);
      // 保存期间切了项目：写下去会串进新项目的条目表与 dirty 标记，直接收手
      //（后端已经存好了，之后回到这个项目再存一次即可，不会丢内容）
      if (viewingProjectIdRef.current !== projectId) return;
      const savedEntries = res.entries || entriesToSave;

      entriesMapRef.current.set(targetFile, savedEntries);
      cleanEntriesMapRef.current.set(targetFile, cloneEntries(savedEntries));
      // 如果保存的是当前打开的文件，同步 entries 状态
      if (targetFile === selectedFile) {
        setEntries(savedEntries);
      }
      setDirtyFiles((prev) => {
        const next = new Set(prev);
        next.delete(targetFile);
        return next;
      });
      setInfo(targetFile === selectedFile ? uiMessage("projects:projectCachePage.handleSave_setInfo_doneSaveCache") : uiMessage("projects:projectCachePage.handleSave_setInfo_doneSave", { targetFile: targetFile }));
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleSave_normalizeError_saveCacheFailed")));
    } finally {
      setSaving(false);
    }
  };

  // 撤销修改，即读取最近文件覆盖当前缓存，并清除 dirty 标记
  const handleRecover = async (filename?: string) => {
    const targetFile = filename || selectedFile;
    if (!targetFile || !dirtyFiles.has(targetFile)) return;

    setLoadingEntries(true);
    setLocalError(null);
    setInfo(null);

    try {
      const res = await fetchCacheFile(projectId, targetFile, configFileName);
      if (viewingProjectIdRef.current !== projectId) return;
      const recoveredEntries = res.entries;
      noteCachePresence(targetFile, res.has_cache !== false);

      entriesMapRef.current.set(targetFile, recoveredEntries);
      cleanEntriesMapRef.current.set(targetFile, cloneEntries(recoveredEntries));

      if (targetFile === selectedFile) {
        setEntries(recoveredEntries);
      }

      setDirtyFiles((prev) => {
        const next = new Set(prev);
        next.delete(targetFile);
        return next;
      });

      setInfo(uiMessage("projects:projectCachePage.handleRecover_setInfo_doneChange", { targetFile: targetFile }));
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleRecover_normalizeError_fileFailed")));
    } finally {
      setLoadingEntries(false);
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
    let lastError: LocalizedText | null = null;
    for (const file of filesToSave) {
      const fileEntries = entriesMapRef.current.get(file);
      if (!fileEntries) continue;
      try {
        const entriesToSave = fileEntries
        .filter(e => !e.deleted)
        .map(e => {
          const { deleted, ...rest } = e;
          return rest;
        });

        const res = await saveCacheFile(projectId, file, entriesToSave, configFileName);
        if (viewingProjectIdRef.current !== projectId) {
          // 保存期间切了项目：别再往新项目的条目表/dirty 标记里写，只把转圈关掉
          setSavingAll(false);
          return;
        }
        const savedEntries = res.entries || entriesToSave;

        entriesMapRef.current.set(file, savedEntries);
        cleanEntriesMapRef.current.set(file, cloneEntries(savedEntries));
        if (file === selectedFile) {
          setEntries(savedEntries);
        }
        savedFiles.push(file);
      } catch (err) {
        lastError = normalizeError(err, uiMessage("projects:projectCachePage.handleSaveAll_normalizeError_saveFailed", { file: file }));
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
      setInfo(uiMessage("projects:projectCachePage.handleSaveAll_setInfo_doneSaveCountFile", { count: savedFiles.length }));
    }
    setSavingAll(false);
  };

  // Load problems when switching to problems tab
  const loadProblems = useCallback(async () => {
    if (!projectId) return;
    const request = ++problemRequestRef.current;
    setLoadingProblems(true);
    try {
      const res = await fetchProjectProblems(projectId, configFileName);
      if (request !== problemRequestRef.current) return;
      setProblems(res.problems);
      setProblemFilterKeys(res.filter_keys || []);
    } catch (err) {
      if (request === problemRequestRef.current) setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.loadProblems_normalizeError_loadProblemFailed")));
    } finally {
      if (request === problemRequestRef.current) setLoadingProblems(false);
    }
  }, [projectId, configFileName]);

  useEffect(() => {
    setProblemFilterKeys([]);
    setProblems([]);
    void loadProblems();
    return () => { problemRequestRef.current += 1; };
  }, [loadProblems]);

  const refreshVisibleData = useCallback(async () => {
    if (!projectId) return;
    // 配置可能刚被修改：丢弃页面内的旧快照，下次打开其他文件也要重新读取。
    // 未保存的译文继续保留在内存里。
    for (const filename of entriesMapRef.current.keys()) {
      if (!dirtyFiles.has(filename)) {
        entriesMapRef.current.delete(filename);
        cleanEntriesMapRef.current.delete(filename);
      }
    }
    await Promise.allSettled([
      loadCacheFiles(),
      runGlobalSearch(),
      loadProblems(),
      refreshCurrentFile(),
    ]);
  }, [dirtyFiles, loadCacheFiles, loadProblems, projectId, refreshCurrentFile, runGlobalSearch]);

  useEffect(() => {
    const wasActive = activeRef.current;
    activeRef.current = active;
    if (!active || wasActive === active) return;
    void refreshVisibleData();
  }, [active, refreshVisibleData]);

  // Close retransl popover on outside click / Escape
  useEffect(() => {
    if (!active || !retranslEditor) return;
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
  }, [active, retranslEditor]);

  // Problems grouped by type
  const problemStats = useMemo(() => {
    const stats: Record<string, ProblemEntry[]> = {};
    for (const p of problems) {
      const types = splitProblemTypes(p.problem);
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

  const handleAddProblemKeyword = useCallback(async (keyword: string, field: 'retranslKey' | 'problemFilterKey') => {
    if (!projectId || !configFileName || savingKeyword) return;
    const label = field === 'problemFilterKey' ? translate("projects:projectCachePage.label_message_problemFilter") : translate("projects:projectCachePage.label_message_retranslate");
    setSavingKeyword(true);
    setLocalError(null);
    try {
      const res = await fetchProjectConfig(projectId, configFileName);
      const config = res.config;
      const common = (config.common as Record<string, unknown>) || {};
      const existingKeys = normalizeKeywordList(common[field]);
      if (existingKeys.includes(keyword)) {
        setInfo(uiMessage("projects:projectCachePage.handleAddProblemKeyword_setInfo_done", { keyword: keyword, label: label }));
        return;
      }
      common[field] = [...existingKeys, keyword];
      config.common = common;
      await updateProjectConfig(projectId, { config, config_file_name: configFileName });
      if (field === 'problemFilterKey') {
        setProblemFilterKeys([...existingKeys, keyword]);
        await Promise.allSettled([loadProblems(), runGlobalSearch()]);
      }
      setInfo(uiMessage("projects:projectCachePage.handleAddProblemKeyword_setInfo_doneVariant2", { keyword: keyword, label: label }));
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleAddProblemKeyword_normalizeError_addFailed", { label: label })));
    } finally {
      setSavingKeyword(false);
    }
  }, [projectId, configFileName, savingKeyword, loadProblems, runGlobalSearch]);

  const submitProblemKeywordEditor = useCallback((editor: NonNullable<typeof retranslEditor>) => {
    const keyword = editor.draft.trim();
    if (!keyword) return;
    setRetranslEditor(null);
    void handleAddProblemKeyword(keyword, editor.action === 'filter' ? 'problemFilterKey' : 'retranslKey');
  }, [handleAddProblemKeyword]);

  const handleSelectFile = (file: string) => {
    if (file === selectedFile) return;
    // 先保存当前文件的修改到 entriesMap
    if (selectedFile && dirtyFiles.has(selectedFile)) {
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
    // 还没有缓存的输入文件没什么可删的（后端会回 not_found），先滤掉
    const targets = filenames.filter((name) => !uncachedFiles.has(name));
    if (!projectId || targets.length === 0) return;
    const msg = targets.length === 1
      ? translate("projects:projectCachePage.msg_message_deleteCacheFile", { value: targets[0] })
      : translate("projects:projectCachePage.msg_message_deleteCountCacheFile", { count: targets.length });
    if (!confirm(msg)) return;
    try {
      const res = await deleteCacheFiles(projectId, targets);
      // 删除期间切了项目：别在新项目的表里删同名条目、也别清它的 dirty 标记
      if (viewingProjectIdRef.current !== projectId) return;
      // 清除已删除文件的 entriesMap 和 dirtyFiles
      for (const f of res.deleted_files) {
        entriesMapRef.current.delete(f);
        cleanEntriesMapRef.current.delete(f);
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
      setInfo(uiMessage("projects:projectCachePage.handleDeleteSelectedFiles_setInfo_doneDeleteCountCacheFile", { count: res.deleted_files.length }));
      // 刷新文件列表
      void loadCacheFiles();
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleDeleteSelectedFiles_normalizeError_deleteCacheFileFailed")));
    }
  }, [projectId, selectedFile, loadCacheFiles, uncachedFiles]);

  // Close context menu on outside click / Escape
  useEffect(() => {
    if (!active || !contextMenu) return;
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
  }, [active, contextMenu]);

  // Ctrl+S: save current file; Ctrl+Shift+S: save all dirty files
  useEffect(() => {
    if (!active) return;
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
  }, [active, saving, savingAll, selectedFile, dirtyFiles, handleSave, handleSaveAll]);

  // Ctrl+A handler for file list
  useEffect(() => {
    if (!active) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (sidebarTab !== 'files') return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        // Only intercept if the file list area is focused / active
        const active = document.activeElement;
        const fileListEl = document.activeElement?.closest('.cache-file-list');
        if (!fileListEl) return;
        if (!fileListEl.contains(active) && active !== fileListEl) return;
        e.preventDefault();
        setSelectedFiles(new Set(cacheFiles.map((f) => f.name)));
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [active, sidebarTab, cacheFiles]);

  // Jump from search result to file editor
  const handleJumpToFile = (filename: string, index: number) => {
    if (filename === selectedFile) {
      setScrollToIndex(index);
      return;
    }
    if (selectedFile && dirtyFiles.has(selectedFile)) {
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
      // 预览期间切了项目：新项目的替换面板不该摆着上一个项目的预览
      if (viewingProjectIdRef.current !== projectId) return;
      setReplacePreview(res.file_details);
      setReplacePreviewTotal(res.total_matches);
    } catch (err) {
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleReplacePreview_normalizeError_replacePreviewFailed")));
    } finally {
      setReplacing(false);
    }
  };

  // Replace execute
  const handleReplaceExecute = async () => {
    if (!replaceQuery.trim()) return;
    if (!confirm(translate("projects:projectCachePage.handleReplaceExecute_confirm_replace", { replacePreviewTotal: replacePreviewTotal, replaceQuery: replaceQuery, replaceWith: replaceWith }))) {
      return;
    }
    setReplacing(true);
    setLocalError(null);
    try {
      const res = await replaceCache(projectId, replaceQuery.trim(), replaceWith, replaceField, false);
      // 替换期间切了项目：不能把上一个项目的替换结果写进新项目的条目表（后端已经改好了）
      if (viewingProjectIdRef.current !== projectId) return;
      setReplacePreview(null);
      setReplacePreviewTotal(0);
      setShowReplace(false);
      setReplaceQuery('');
      setReplaceWith('');
      setInfo(uiMessage("projects:projectCachePage.handleReplaceExecute_setInfo_doneReplaceCountFileSaveEffective", { total_matches: res.total_matches, total_files: res.total_files }));
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
      setLocalError(normalizeError(err, uiMessage("projects:projectCachePage.handleReplaceExecute_normalizeError_replaceFailed")));
    } finally {
      setReplacing(false);
    }
  };

  if (loading && cacheFiles.length === 0) {
    return (
      <div className="project-cache-page" style={cacheBrowserFontStyle}>
        <PageHeader className="project-cache-page__header" title={translate("projects:projectCachePage.projectCachePage_title_text")} />
        <LoadingState title={translate("projects:projectCachePage.projectCachePage_title_loadFile")} description={translate("projects:projectCachePage.projectCachePage_description_pendingReadProjectFile")} />
      </div>
    );
  }
  return (
    <div className="project-cache-page" style={cacheBrowserFontStyle}>
      <PageHeader
        className="project-cache-page__header"
        title={translate("projects:projectCachePage.projectCachePage_title_text")}
        description={translate("projects:projectCachePage.projectCachePage_description_translationProblemDeleteCacheSentenceRetranslateCache")}
        actions={cacheDir ? (
          <Button variant="secondary" onClick={() => void invoke('open_folder', { path: cacheDir })} title={cacheDir}>
            <Icon name="folder-open" />{translate("projects:projectCachePage.projectCachePage_button_openCacheFile")}</Button>
        ) : null}
        status={
          <>
            {error && <InlineFeedback tone="error" title={translate("projects:projectCachePage.projectCachePage_title_loadCacheFailed")} description={error} />}
            {localError && <InlineFeedback tone="error" title={translate("projects:projectCachePage.projectCachePage_title_failed")} description={localError} />}
            {info && <InlineFeedback className="inline-alert--floating" tone="success" title={translate("projects:projectCachePage.projectCachePage_title_success")} description={info} onDismiss={() => setInfo(null)} />}
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
            >{translate("projects:projectCachePage.cacheSidebarTabs_button_file")}{dirtyFiles.size > 0 ? <span className="cache-sidebar-tab__badge">{dirtyFiles.size}</span> : ''}
            </button>
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'search' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => setSidebarTab('search')}
            >{translate("common:actions.search")}</button>
            <button
              type="button"
              className={`cache-sidebar-tab ${sidebarTab === 'problems' ? 'cache-sidebar-tab--active' : ''}`}
              onClick={() => { setSidebarTab('problems'); void loadProblems(); }}
            >{translate("projects:projectCachePage.cacheSidebarTabs_button_problem")}{problems.length > 0 ? <span className="cache-sidebar-tab__badge">{problems.length}</span> : ''}
            </button>
          </div>

          {/* Tab: Files */}
          {sidebarTab === 'files' && (
            <div className="cache-sidebar-tab-content">
              <div className="cache-layout__sidebar-header">
                <h3>{translate("projects:projectCachePage.cacheLayoutSidebarHeader_message_file")}</h3>
                <div className="cache-layout__sidebar-header-actions">
                  {dirtyFiles.size > 0 && (
                    <Button
                      type="button"
                      variant="primary"
                      className="cache-file-save-all"
                      onClick={() => void handleSaveAll()}
                      disabled={savingAll}
                      title={translate("projects:projectCachePage.cacheFileSaveAll_title_saveCountChangeFile", { count: dirtyFiles.size })}
                    >
                      {savingAll ? <Icon name="hourglass" /> : <><Icon name="save" />{translate("projects:projectCachePage.cacheFileSaveAll_text_allSave")}{dirtyFiles.size})</>}
                    </Button>
                  )}
                  <button
                    type="button"
                    className={`icon-btn icon-btn--refresh${refreshingFiles ? ' icon-btn--spinning' : ''}`}
                    onClick={() => void refreshVisibleData()}
                    disabled={refreshingFiles || loadingEntries}
                    title={translate("projects:projectCachePage.cacheLayoutSidebarHeaderActions_title_file")}
                    aria-label={translate("projects:projectCachePage.cacheLayoutSidebarHeaderActions_ariaLabel_file")}
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
                  <span className="cache-file-list__selection-count">{translate("projects:projectCachePage.cacheFileListSelectionBar_message_doneSelectCountFile", { count: selectedFiles.size })}</span>
                  <div className="cache-file-list__selection-actions">
                    <button
                      type="button"
                      className="cache-file-list__selection-delete"
                      onClick={() => void handleDeleteSelectedFiles(Array.from(selectedFiles))}
                    >{translate("common:actions.delete")}</button>
                    <button
                      type="button"
                      className="cache-file-list__selection-clear"
                      onClick={() => setSelectedFiles(new Set())}
                    >{translate("projects:projectCachePage.cacheFileListSelectionActions_message_cancelSelect")}</button>
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
                          // 没有缓存文件的条目没什么可删的（后端会报 not_found）
                          showDelete: file.has_cache !== false,
                        });
                      }}
                      title={file.input_name ? translate("projects:projectCachePage.cacheFileList_title_source", { input_name: file.input_name }) : undefined}
                    >
                      <span className="cache-file-item__name">
                        {dirtyFiles.has(file.name) && <span className="cache-file-item__dot" title={translate("projects:projectCachePage.cacheFileItemName_title_notSaveChange")} />}
                        {file.name}
                      </span>
                      {file.has_cache === false ? (
                        <span className="cache-file-item__size cache-file-item__size--uncached" title={translate("projects:projectCachePage.cacheFileItemSizeCacheFileItemSizeUncached_title_emptyCacheFileOpenSource")}>{translate("projects:projectCachePage.cacheFileList_message_notTranslation")}</span>
                      ) : (
                        <span className="cache-file-item__size">{file.entry_count != null ? translate("projects:projectCachePage.cacheFileItemSize_message_text", { entry_count: file.entry_count }) : formatSize(file.size)}</span>
                      )}
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
                  placeholder={translate("projects:projectCachePage.cacheSearchInputGroup_placeholder_search")}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
                <CustomSelect
                  className="cache-search-field"
                  value={searchField}
                  onChange={(e) => setSearchField(e.target.value as CacheSearchField)}
                >
                  <option value="all">{translate("projects:projectCachePage.cacheSearchField_message_all")}</option>
                  <option value="src">{translate("projects:projectCachePage.cacheSearchField_message_source")}</option>
                  <option value="dst">{translate("projects:projectCachePage.cacheSearchField_message_translationText")}</option>
                  <option value="problem">{translate("projects:projectCachePage.cacheSearchField_message_problem")}</option>
                </CustomSelect>
                <label className="cache-search-regex-toggle" title={translate("projects:projectCachePage.cacheSearchRegexToggle_title_regexSearch")}>
                  <input
                    type="checkbox"
                    checked={searchOptions.re}
                    onChange={(e) => setSearchOptions((current) => ({ ...current, re: e.target.checked }))}
                    disabled={searching}
                  />
                  <span className="cache-search-regex-toggle__track" aria-hidden="true" />
                  <span className="cache-search-regex-toggle__label">{translate("projects:projectCachePage.cacheSearchRegexToggle_message_regex")}</span>
                </label>
              </div>

              {/* Replace toggle + Search results summary */}
              <div className="cache-search-meta">
                <button
                  type="button"
                  className="cache-replace-toggle__btn"
                  onClick={() => { setShowReplace(!showReplace); setReplaceQuery(searchQuery); }}
                  title={showReplace ? translate("projects:projectCachePage.cacheReplaceToggleBtn_title_replace") : translate("projects:projectCachePage.cacheReplaceToggleBtn_title_replaceVariant2")}
                >
                  {showReplace ? <><Icon name="chevron-down" />{translate("projects:projectCachePage.cacheReplaceToggleBtn_text_replace")}</> : <><Icon name="chevron-right" />{translate("projects:projectCachePage.cacheReplaceToggleBtn_text_replace")}</>}
                </button>
                {searching && <span className="cache-search-status">{translate("projects:projectCachePage.cacheSearchMeta_message_search")}</span>}
                {!searching && searchQuery.trim() && (
                  <span className="cache-search-status">
                    {searchTotal > 0 ? translate("projects:projectCachePage.cacheSearchStatus_message_entry", { searchTotal: searchTotal }) : translate("projects:projectCachePage.cacheSearchStatus_message_match")}
                  </span>
                )}
              </div>
              {showReplace && (
                <div className="cache-replace-group">
                  <input
                    type="text"
                    className="cache-search cache-search--replace"
                    placeholder={translate("projects:projectCachePage.cacheReplaceGroup_placeholder_search")}
                    value={replaceQuery}
                    onChange={(e) => setReplaceQuery(e.target.value)}
                  />
                  <input
                    type="text"
                    className="cache-search cache-search--replace"
                    placeholder={translate("projects:projectCachePage.cacheReplaceGroup_placeholder_replace")}
                    value={replaceWith}
                    onChange={(e) => setReplaceWith(e.target.value)}
                  />
                  <CustomSelect
                    className="cache-search-field"
                    value={replaceField}
                    onChange={(e) => setReplaceField(e.target.value as CacheReplaceField)}
                  >
                    <option value="dst">{translate("projects:projectCachePage.cacheSearchField_message_translationTextVariant2")}</option>
                    <option value="src">{translate("projects:projectCachePage.cacheSearchField_message_sourceVariant2")}</option>
                    <option value="all">{translate("projects:projectCachePage.cacheSearchField_message_all")}</option>
                  </CustomSelect>
                  <div className="cache-replace-actions">
                    <Button
                      variant="secondary"
                      disabled={replacing || !replaceQuery.trim()}
                      onClick={() => void handleReplacePreview()}
                    >{translate("projects:projectCachePage.cacheReplaceActions_message_preview")}</Button>
                    <Button
                      variant="primary"
                      disabled={replacing || !replaceQuery.trim() || replacePreviewTotal === 0}
                      onClick={() => void handleReplaceExecute()}
                    >
                      {replacing ? translate("projects:projectCachePage.cacheReplaceActions_message_replace") : translate("projects:projectCachePage.cacheReplaceActions_message_replaceVariant2")}
                    </Button>
                  </div>
                  {replacePreview !== null && (
                    <div className="cache-replace-preview">
                      <div className="cache-replace-preview__summary">{translate("projects:projectCachePage.cacheReplacePreview_message_matchCountFile", { replacePreviewTotal: replacePreviewTotal, count: replacePreview.length })}</div>
                      {replacePreview.map((fd) => (
                        <div key={fd.filename} className="cache-replace-preview__file">
                          <span className="cache-replace-preview__filename">{fd.filename}</span>
                          <span className="cache-replace-preview__count">{translate("projects:projectCachePage.cacheReplacePreviewFile_message_text", { matches: fd.matches })}</span>
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
              <div className="cache-problems-hint">{translate("projects:projectCachePage.cacheProblemsPanel_message_retranslateEntryFilterProblemEntryRegexMatch")}</div>
              {loadingProblems ? (
                <div className="cache-problems-loading">{translate("projects:projectCachePage.cacheProblemsPanel_message_loadProblem")}</div>
              ) : problems.length === 0 ? (
                <div className="cache-problems-empty">{translate("projects:projectCachePage.cacheProblemsPanel_message_notProblem")}</div>
              ) : (
                <div className="cache-problems-groups">
                  {problemStats.map(([type, items]) => (
                    <div key={type} className="cache-problems-group">
                      <div
                        className="cache-problems-group__header"
                        onClick={() => handleProblemClick(type)}
                        title={translate("projects:projectCachePage.cacheProblemsGroupHeader_title_searchProblem", { type: type })}
                      >
                        <span className="cache-problems-group__summary">
                          <span className="cache-problems-group__type">{type}</span>
                        </span>
                        <span className="cache-problems-group__count">{items.length}</span>
                        <button
                          type="button"
                          className={`cache-problems-group__retransl${retranslEditor?.type === type && retranslEditor.action === 'retransl' ? ' cache-problems-group__retransl--active' : ''}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                            const anchor = {
                              top: rect.top + rect.height / 2,
                              left: rect.right + 10 };
                            setRetranslEditor((cur) => (
                              cur && cur.type === type && cur.action === 'retransl'
                                ? null
                                : { type, draft: type, action: "retransl", anchor }
                            ));
                          }}
                          title={translate("projects:projectCachePage.cacheProblemsGroupHeader_title_editRetranslate")}
                          aria-label={translate("projects:projectCachePage.cacheProblemsGroupHeader_ariaLabel_editRetranslate", { type: type })}
                          aria-expanded={retranslEditor?.type === type && retranslEditor.action === 'retransl'}
                          disabled={savingKeyword}
                        >
                          +
                        </button>
                        <button
                          type="button"
                          className={`cache-problems-group__retransl cache-problems-group__filter${retranslEditor?.type === type && retranslEditor.action === 'filter' ? ' cache-problems-group__filter--active' : ''}`}
                          disabled={savingKeyword}
                          onClick={(e) => {
                            e.stopPropagation();
                            const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                            const anchor = {
                              top: rect.top + rect.height / 2,
                              left: rect.right + 10 };
                            setRetranslEditor((cur) => (
                              cur && cur.type === type && cur.action === 'filter'
                                ? null
                                : { type, draft: '', action: "filter", anchor }
                            ));
                          }}
                          title={translate("projects:projectCachePage.cacheProblemsGroupHeader_title_entryFilterProblemEntryProblemItem", { type: type })}
                          aria-label={translate("projects:projectCachePage.cacheProblemsGroupHeader_ariaLabel_entryProblemItemProblemFilter", { type: type })}
                          aria-expanded={retranslEditor?.type === type && retranslEditor.action === 'filter'}
                        >
                          -
                        </button>
                        {active && retranslEditor?.type === type && createPortal((
                          <div
                            ref={retranslPopoverRef}
                            className="retransl-popover"
                            role="dialog"
                            aria-label={retranslEditor.action === 'filter' ? translate("projects:projectCachePage.retranslPopover_ariaLabel_editProblemFilter") : translate("projects:projectCachePage.retranslPopover_ariaLabel_editRetranslate")}
                            onClick={(e) => e.stopPropagation()}
                            style={{ top: retranslEditor.anchor.top, left: retranslEditor.anchor.left }}
                          >
                            <div className="retransl-popover__arrow" aria-hidden="true" />
                            <label className="retransl-popover__label">
                              {retranslEditor.action === 'filter' ? translate("projects:projectCachePage.retranslPopoverLabel_message_problemFilterRegex") : translate("projects:projectCachePage.retranslPopoverLabel_message_retranslate")}
                            </label>
                            <input
                              ref={retranslInputRef}
                              type="text"
                              className="retransl-popover__input"
                              value={retranslEditor.draft}
                              onChange={(e) => setRetranslEditor((cur) => (cur ? { ...cur, draft: e.target.value } : cur))}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                  e.preventDefault();
                                  submitProblemKeywordEditor(retranslEditor);
                                } else if (e.key === 'Escape') {
                                  e.preventDefault();
                                  setRetranslEditor(null);
                                }
                              }}
                              placeholder={retranslEditor.action === 'filter' ? translate("projects:projectCachePage.retranslPopover_placeholder_regexJapaneseJapanese") : translate("projects:projectCachePage.retranslPopover_placeholder_text")}
                              autoFocus
                            />
                            <div className="retransl-popover__actions">
                              <button
                                type="button"
                                className="retransl-popover__btn retransl-popover__btn--ghost"
                                onClick={() => setRetranslEditor(null)}
                              >{translate("common:actions.cancel")}</button>
                              <button
                                type="button"
                                className="retransl-popover__btn retransl-popover__btn--primary"
                                 disabled={savingKeyword || !retranslEditor.draft.trim()}
                                 onClick={() => {
                                  submitProblemKeywordEditor(retranslEditor);
                                 }}
                              >{translate("projects:projectCachePage.retranslPopoverActions_message_text")}</button>
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
          aria-label={translate("projects:projectCachePage.projectCachePage_ariaLabel_file")}
        />

        <div className="cache-layout__main">
          {selectedFile ? (
            <Panel
              title={selectedFile}
              description={translate("projects:projectCachePage.cacheLayoutMain_description_sentenceDoneTranslationProblem", { total: total, translated: translated, withProblems: withProblems })}
              actions={(
                <div className="cache-panel-actions">
                  <Button
                    onClick={() => void handleRecover()}
                    disabled={loadingEntries || !dirty || selectedHasNoCache}
                    title={selectedHasNoCache ? translate("projects:projectCachePage.cachePanelActions_title_emptyCacheFileEmptyChange") : undefined}
                  >
                    {loadingEntries ? translate("projects:projectCachePage.cachePanelActions_message_text") : translate("projects:projectCachePage.cachePanelActions_message_textVariant2")}
                  </Button>
                  <Button
                    onClick={() => void handleSave()}
                    disabled={saving || !dirty || selectedHasNoCache}
                    title={selectedHasNoCache ? translate("projects:projectCachePage.cachePanelActions_title_emptyCacheFileTranslation") : undefined}
                  >
                    {saving ? translate("common:actions.saving") : translate("common:actions.save")}
                  </Button>
                </div>
              )}
            >
              <div className="cache-toolbar">
                <input
                  type="text"
                  placeholder={translate("projects:projectCachePage.cacheToolbar_placeholder_searchSourceTranslationText")}
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="cache-search"
                />
                <label className="cache-filter">
                  <input
                    type="checkbox"
                    checked={filterProblems}
                    onChange={(e) => setFilterProblems(e.target.checked)}
                  />{translate("projects:projectCachePage.cacheFilter_label_problemSentence")}</label>
              </div>

              <div className="cache-card-list-wrapper">
                {loadingEntries && (
                  <div className="cache-card-list-loading">
                    <strong>{translate("common:actions.loading")}</strong>
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
                      onDelete={handleDeleteAndRecover}
                      onAddProblemFilter={(keyword) => { void handleAddProblemKeyword(keyword, 'problemFilterKey'); }}
                      highlightQuery={searchTerm || searchQuery}
                      nameDict={nameDict}
                      readOnly={selectedHasNoCache}
                    />
                  ))}
                  {filteredEntries.length === 0 && !loadingEntries && (
                    <EmptyState title={translate("projects:projectCachePage.cacheCardListWrapper_title_matchEntry")} description={translate("projects:projectCachePage.cacheCardListWrapper_description_search")} />
                  )}
                </div>
              </div>
            </Panel>
          ) : (
            <EmptyState className="cache-layout__empty" title={translate("projects:projectCachePage.cacheLayoutMain_title_selectCountFile")} description={translate("projects:projectCachePage.cacheLayoutMain_description_selectFileSourceTranslationTextSearch")} />
          )}
        </div>
      </div>
      {active && contextMenu && createPortal(
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
              void handleRevealCacheFiles(filenames);
            }}
          >
            <span className="cache-context-menu__icon" aria-hidden="true"><Icon name="folder-open" /></span>
            <span className="cache-context-menu__label">{translate("projects:projectCachePage.cacheContextMenuItem_message_file")}</span>
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
              <span className="cache-context-menu__icon" aria-hidden="true"><Icon name="trash" /></span>
              <span className="cache-context-menu__label">{translate("common:actions.delete")}{contextMenu.filenames.length > 1 ? translate("projects:projectCachePage.cacheContextMenuLabel_message_countFile", { count: contextMenu.filenames.length }) : ''}
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
