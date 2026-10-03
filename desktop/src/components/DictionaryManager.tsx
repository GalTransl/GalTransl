import { UiTrans, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from './Button';
import { Icon } from './Icon';
import { Panel } from './Panel';
import { EmptyState, ErrorState, InlineFeedback, LoadingState } from './page-state';
import { formatBackendUsage, type BackendUsageSummary } from '../lib/backendUsage';
import type { DictFileContent, DictionaryCategory } from '../lib/api';

type DictTab = DictionaryCategory;
type DictRowType = 'normal' | 'conditional' | 'situation' | 'gpt' | 'comment' | 'blank';

type DictRow = {
  type: DictRowType;
  values: string[];
  raw: string;
};

type DictRowWithIndex = {
  row: DictRow;
  rowIndex: number;
};

type DictRowGroup = {
  type: DictRowType;
  items: DictRowWithIndex[];
};

type DictionaryManagerData = {
  pre_dict_files: string[];
  gpt_dict_files: string[];
  post_dict_files: string[];
  dict_contents: Record<string, DictFileContent>;
};

type DictionaryManagerProps = {
  title: string;
  description: string;
  data: DictionaryManagerData | null;
  loading: boolean;
  error: string | null;
  onReload: () => Promise<void>;
  onCreateFile: (category: DictTab, filename: string) => Promise<string>;
  onSaveFile: (fileKey: string, content: string) => Promise<void>;
  onDeleteFile: (fileKey: string) => Promise<void>;
  onGenerateGptDict?: () => Promise<void>;
  /** 「AI 生成 GPT 字典」二次确认里要说明用哪个后端（与开始翻译同一口径） */
  gendicBackend?: BackendUsageSummary | null;
  /**
   * 条目行上的「→」：拿着这一行的日文词（GPT 取原文列、普通/条件/场景条目取「搜索」列）
   * 跳到「浏览文本」里搜一下，看它实际出现在哪。
   * 不传就不显示这个按钮（全局字典页没有所属项目，传不了）。
   */
  onOpenInCache?: (sourceWord: string) => void;
};

type DictContextMenuState = {
  x: number;
  y: number;
  file: string;
};

const PROJECT_DIR_MARKER = '(project_dir)';
const REFRESH_SPIN_CYCLE_MS = 500;

/** Strip the "(project_dir)" prefix for display purposes */
function stripProjectDirMarker(name: string): string {
  return name.replace(PROJECT_DIR_MARKER, '').trim();
}

function getFilesByTab(data: DictionaryManagerData | null, tab: DictTab): string[] {
  if (!data) return [];
  const files = tab === 'pre' ? data.pre_dict_files : tab === 'gpt' ? data.gpt_dict_files : data.post_dict_files;
  return [...files].sort((a, b) => {
    const aMtime = data.dict_contents[a]?.mtime ?? -1;
    const bMtime = data.dict_contents[b]?.mtime ?? -1;
    if (aMtime !== bMtime) {
      return bMtime - aMtime;
    }
    return stripProjectDirMarker(a).localeCompare(stripProjectDirMarker(b));
  });
}

// GenDic 生成的 GPT 字典按类目分区：----------↓人名↓----------
const SECTION_LINE_RE = /^-{3,}↓(.+?)↓-{3,}\s*$/;
// 第一个分区标题之前的词条（旧格式、手加的）归到这一类
const UNSECTIONED_LABEL = '\0unsectioned';

function parseRows(text: string, tab: DictTab): DictRow[] {
  const lines = text.split('\n');
  return lines.map((line) => {
    if (!line.trim() && !line.includes('\t')) return { type: 'blank', values: [], raw: line };
    // GenDic 生成的字典用 ----------↓人名↓---------- 这样的行分区，也当注释显示
    if (line.startsWith('//') || line.startsWith('#') || line.startsWith('\\\\') || SECTION_LINE_RE.test(line)) {
      return { type: 'comment', values: [line], raw: line };
    }
    const parts = line.split('\t');
    if (tab === 'gpt') {
      const [src = '', dst = '', ...notes] = parts;
      return { type: 'gpt', values: [src, dst, notes.join('\t')], raw: line };
    }
    if (
      parts.length >= 4
      && ['pre_jp', 'post_jp', 'pre_zh', 'post_zh', 'pre_src', 'post_src', 'pre_dst', 'post_dst'].includes(parts[0])
    ) {
      const [target = '', cond = '', search = '', replace = '', ...rest] = parts;
      return { type: 'conditional', values: [target, cond, search, replace, rest.join('\t')], raw: line };
    }
    if (parts.length >= 3 && ['diag', 'mono'].includes(parts[0])) {
      const [scene = '', search = '', ...replace] = parts;
      return { type: 'situation', values: [scene, search, replace.join('\t')], raw: line };
    }
    const [search = '', replace = '', ...rest] = parts;
    return { type: 'normal', values: [search, replace, rest.join('\t')], raw: line };
  });
}

function rowsToText(rows: DictRow[]): string {
  return rows.map((row) => {
    if (row.type === 'blank') return '';
    if (row.type === 'comment') return row.values[0] ?? row.raw;
    return row.values.join('\t');
  }).join('\n');
}

/** Column labels by tab & row type for the card's header pills */
function getTypeLabel(type: DictRowType, tab: DictTab): string {
  if (type === 'comment') return translate("projects:dictionaryManager.getTypeLabel_message_text");
  if (type === 'blank') return translate("projects:dictionaryManager.getTypeLabel_message_textVariant2");
  if (type === 'gpt') return 'GPT';
  if (type === 'normal') return translate("projects:dictionaryManager.getTypeLabel_message_textVariant3");
  if (type === 'conditional') return translate("projects:dictionaryManager.getTypeLabel_message_entry");
  if (type === 'situation') return translate("projects:dictionaryManager.getTypeLabel_message_textVariant4");
  return type;
}

/** Field labels for each row type */
function getFieldLabels(type: DictRowType, _tab: DictTab): string[] {
  if (type === 'gpt') return [translate("projects:dictionaryManager.getFieldLabels_message_source"), translate("projects:dictionaryManager.getFieldLabels_message_translationText"), translate("projects:dictionaryManager.getFieldLabels_message_text")];
  if (type === 'normal') return [translate("common:actions.search"), translate("projects:dictionaryManager.getFieldLabels_message_replace"), translate("projects:dictionaryManager.getFieldLabels_message_textVariant2")];
  if (type === 'conditional') return [translate("projects:dictionaryManager.getFieldLabels_message_target"), translate("projects:dictionaryManager.getFieldLabels_message_entry"), translate("common:actions.search"), translate("projects:dictionaryManager.getFieldLabels_message_replace"), translate("projects:dictionaryManager.getFieldLabels_message_textVariant2")];
  if (type === 'situation') return [translate("projects:dictionaryManager.getFieldLabels_message_textVariant3"), translate("common:actions.search"), translate("projects:dictionaryManager.getFieldLabels_message_replace")];
  if (type === 'comment') return [translate("projects:dictionaryManager.getFieldLabels_message_textVariant4")];
  return [];
}

/**
 * 这一行里「要拿去搜的日文词」在第几列（条目行上的「→」用它跳到缓存搜索）。
 * 目标/条件这类前置列不是词本身，所以按类型点名：条件条目的搜索词在第 3 列、场景条目在第 2 列。
 * 注释行没有词，返回 -1（不显示箭头）。
 */
function getSourceCellIndex(type: DictRowType): number {
  if (type === 'gpt') return 0;         // 原文
  if (type === 'normal') return 0;      // 搜索
  if (type === 'conditional') return 2; // 目标 / 条件 / 搜索 / 替换 / 备注
  if (type === 'situation') return 1;   // 场景 / 搜索 / 替换
  return -1;
}

/* ── Grouped dict entries card ── */
function DictEntryGroupCard({
  group,
  tab,
  headerExtra,
  onCellChange,
  onDelete,
  onAddRow,
  onOpenInCache,
}: {
  group: DictRowGroup;
  tab: DictTab;
  /** 挂在卡片头部、跟「GPT 216条」同一行的额外内容（类目筛选胶囊） */
  headerExtra?: ReactNode;
  onCellChange: (rowIndex: number, cellIndex: number, value: string) => void;
  onDelete: (rowIndex: number) => void;
  onAddRow: (rowType: DictRowType, insertAfterRowIndex: number) => void;
  onOpenInCache?: (sourceWord: string) => void;
}) {
  const uiLanguage = useUiLanguage();
  const labels = getFieldLabels(group.type, tab);
  const tableStyle = { '--dict-column-count': labels.length } as CSSProperties;

  return (
    <article className={`dict-card dict-card--${group.type} dict-card--grouped`}>
      <div className="dict-card__header">
        <div className="dict-card__badges">
          <span className={`dict-card__pill dict-card__pill--${group.type}`}>
            {getTypeLabel(group.type, tab)}
          </span>
          <span className="dict-card__pill dict-card__pill--index">{translate("projects:dictionaryManager.dictCardBadges_message_entry", { count: group.items.length })}</span>
        </div>
        {headerExtra}
      </div>

      <div className="dict-card__table" style={tableStyle}>
        <div className="dict-card__table-head">
          <div className="dict-card__head-cell dict-card__head-cell--index">{translate("projects:dictionaryManager.dictCardTableHead_message_iD")}</div>
          {labels.map((label, ci) => (
            <div key={ci} className="dict-card__head-cell">{label || translate("projects:dictionaryManager.dictCardHeadCell_message_text", { value: ci + 1 })}</div>
          ))}
        </div>

        {group.items.map(({ row, rowIndex }) => {
          // 「→」搜的是这一行的日文词：GPT 条目取原文列，其他类型取「搜索」列
          const sourceCellIndex = getSourceCellIndex(group.type);
          const sourceWord = sourceCellIndex >= 0 ? String(row.values[sourceCellIndex] ?? '').trim() : '';
          return (
            <div key={`${rowIndex}`} className="dict-card__table-row">
              <div className="dict-card__cell dict-card__cell--index">#{rowIndex + 1}</div>
              {labels.map((label, ci) => (
                <div key={ci} className="dict-card__cell">
                  <input
                    className="dict-card__input"
                    value={row.values[ci] ?? ''}
                    onChange={(e) => onCellChange(rowIndex, ci, e.target.value)}
                    placeholder={label || translate("projects:dictionaryManager.dictCardCell_placeholder_text", { value: ci + 1 })}
                  />
                </div>
              ))}
              {/* 拿这一行的日文词去「浏览文本」搜它出现在哪（注释行没有词，不显示） */}
              {onOpenInCache && sourceCellIndex >= 0 ? (
                <button
                  type="button"
                  className="dict-card__row-open-cache"
                  onClick={() => onOpenInCache(sourceWord)}
                  disabled={!sourceWord}
                  title={translate("projects:dictionaryManager.dictCardRowOpenCache_title_textSearchCount")}
                >
                  <Icon name="arrow-right" />
                </button>
              ) : null}
              <button
                type="button"
                className="dict-card__row-delete"
                onClick={() => onDelete(rowIndex)}
                title={translate("projects:dictionaryManager.dictCardRowDelete_title_deleteEntry")}
              >
                <Icon name="close" />
              </button>
            </div>
          );
        })}

        <div className="dict-card__table-add-row">
          <button
            type="button"
            className="dict-card__add-row-btn"
            onClick={() => onAddRow(group.type, group.items[group.items.length - 1]?.rowIndex ?? -1)}
            title={translate("projects:dictionaryManager.dictCardAddRowBtn_title_entry")}
          >
            +
          </button>
        </div>
      </div>
    </article>
  );
}

/* ── Main component ── */
export function DictionaryManager(props: DictionaryManagerProps) {
  const uiLanguage = useUiLanguage();
  const {
    data,
    loading,
    error,
    onReload,
    onCreateFile,
    onSaveFile,
    onDeleteFile,
    onGenerateGptDict,
    gendicBackend,
    onOpenInCache,
    title,
    description,
  } = props;

  const [activeTab, setActiveTab] = useState<DictTab>('gpt');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [sectionFilter, setSectionFilter] = useState('');
  const [mode, setMode] = useState<'card' | 'text'>('card');
  const [draftText, setDraftText] = useState<string>('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [generatingGptDict, setGeneratingGptDict] = useState(false);
  const [showGenerateConfirm, setShowGenerateConfirm] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [newFilename, setNewFilename] = useState('');
  const [localError, setLocalError] = useMessageState<string | null>(null);
  const [info, setInfo] = useMessageState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<DictContextMenuState | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);

  const activeFiles = useMemo(() => getFilesByTab(data, activeTab), [data, activeTab]);

  const selectedContent = useMemo(() => {
    if (!data || !selectedFile) return null;
    return data.dict_contents[selectedFile] ?? null;
  }, [data, selectedFile]);

  const parsedRows = useMemo(() => parseRows(draftText, activeTab), [draftText, activeTab]);

  // 每行所属的类目（最近的分区标题），以及各类目的条目数；文件没有分区时 sections 为空，不显示类目筛选
  const { rowSections, sections } = useMemo(() => {
    const owner: string[] = [];
    const counts = new Map<string, number>();
    let current = UNSECTIONED_LABEL;
    let hasSection = false;
    for (const row of parsedRows) {
      const match = SECTION_LINE_RE.exec(row.raw);
      if (match) {
        current = match[1];
        hasSection = true;
        if (!counts.has(current)) counts.set(current, 0);
      } else if (row.raw.includes('\t') && row.type !== 'comment' && row.type !== 'blank') {
        counts.set(current, (counts.get(current) ?? 0) + 1);
      }
      owner.push(current);
    }
    if (!hasSection) return { rowSections: owner, sections: [] as { name: string; count: number }[] };
    const list = [...counts.entries()]
      .filter(([name, count]) => name !== UNSECTIONED_LABEL || count > 0)
      .map(([name, count]) => ({ name, count }));
    return { rowSections: owner, sections: list };
  }, [parsedRows]);

  // 选中的类目在当前文件里不存在（换了文件、分区被删）时视为「全部」
  const activeSection = sections.some((s) => s.name === sectionFilter) ? sectionFilter : '';

  const filteredRows = useMemo(() => {
    const visible = parsedRows
      .map((row, rowIndex) => ({ row, rowIndex }))
      .filter(({ row, rowIndex }) => {
        // 过滤掉注释行（// 或 # 开头）
        if (row.type === 'comment') return false;
        // 过滤掉空行
        if (row.type === 'blank') return false;
        // 过滤掉少于 1 个 tab 分隔的行（即没有 tab 的行）
        if (!row.raw.includes('\t')) return false;
        if (activeSection && rowSections[rowIndex] !== activeSection) return false;
        return true;
      });
    if (!searchTerm.trim()) return visible;
    const needle = searchTerm.toLowerCase();
    return visible.filter(({ row }) => row.values.join('\t').toLowerCase().includes(needle));
  }, [parsedRows, searchTerm, activeSection, rowSections]);

  const groupedRows = useMemo(() => {
    const groups: DictRowGroup[] = [];
    for (const item of filteredRows) {
      const lastGroup = groups[groups.length - 1];
      if (lastGroup && lastGroup.type === item.row.type) {
        lastGroup.items.push(item);
      } else {
        groups.push({ type: item.row.type, items: [item] });
      }
    }
    return groups;
  }, [filteredRows]);

  // 类目筛选：一排胶囊挂在卡片头部（跟「GPT 216条」同一行），点当前项退回全部
  const sectionPills = sections.length > 0 ? (
    <div className="dict-section-pills" role="group" aria-label={translate("projects:dictionaryManager.dictSectionPills_ariaLabel_filter")}>
      <button
        type="button"
        className={`dict-section-pill${activeSection ? '' : ' dict-section-pill--active'}`}
        onClick={() => setSectionFilter('')}
        title={translate("projects:dictionaryManager.dictSectionPills_title_all")}
      >{translate("projects:dictionaryManager.dictSectionPills_button_all")}<span className="dict-section-pill__count">{sections.reduce((sum, item) => sum + item.count, 0)}</span>
      </button>
      {sections.map((section) => {
        const isActive = activeSection === section.name;
        const label = section.name === UNSECTIONED_LABEL ? translate("projects:dictionaryManager.uNSECTIONEDLABEL_message_not") : section.name;
        return (
          <button
            key={section.name}
            type="button"
            className={`dict-section-pill${isActive ? ' dict-section-pill--active' : ''}`}
            onClick={() => setSectionFilter(isActive ? '' : section.name)}
            title={isActive ? translate("projects:dictionaryManager.dictSectionPills_title_cancelFilterAll") : translate("projects:dictionaryManager.dictSectionPills_title_text", { name: label })}
          >
            {label}
            <span className="dict-section-pill__count">{section.count}</span>
          </button>
        );
      })}
    </div>
  ) : null;

  const handleReload = async () => {
    if (refreshing) return;
    setRefreshing(true);
    const startedAt = Date.now();
    try {
      await onReload();
    } finally {
      const elapsedMs = Date.now() - startedAt;
      const minVisibleMs = 420;
      const minReachedMs = Math.max(elapsedMs, minVisibleMs);
      const remainToFullCycleMs = (REFRESH_SPIN_CYCLE_MS - (minReachedMs % REFRESH_SPIN_CYCLE_MS)) % REFRESH_SPIN_CYCLE_MS;
      const remainMs = Math.max(0, minVisibleMs - elapsedMs) + remainToFullCycleMs;
      if (remainMs > 0) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, remainMs));
      }
      setRefreshing(false);
    }
  };

  useEffect(() => {
    if (!contextMenu) return;

    const onPointerDown = (event: PointerEvent) => {
      const menuEl = contextMenuRef.current;
      if (menuEl && menuEl.contains(event.target as Node)) return;
      setContextMenu(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setContextMenu(null);
    };

    window.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [contextMenu]);

  const handleRevealFile = async (file: string) => {
    const filePath = data?.dict_contents?.[file]?.path;
    if (!filePath) {
      setLocalError(uiMessage("projects:dictionaryManager.handleRevealFile_setLocalError_unableDictionaryFile", { value: stripProjectDirMarker(file) }));
      setInfo(null);
      return;
    }

    setLocalError(null);
    setInfo(null);
    try {
      await invoke('reveal_file', { path: filePath });
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : uiMessage("projects:dictionaryManager.handleRevealFile_setLocalError_fileFailed", { value: String(e) }));
    }
  };

  const handleGenerateGptDict = async () => {
    if (!onGenerateGptDict || generatingGptDict) return;
    setGeneratingGptDict(true);
    setLocalError(null);
    setInfo(null);
    try {
      await onGenerateGptDict();
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : uiMessage("projects:dictionaryManager.handleGenerateGptDict_setLocalError_aIGPTDictionaryJobFailed"));
    } finally {
      setGeneratingGptDict(false);
    }
  };

  // 启动前先确认：任务会调用模型、消耗额度，得让用户看清楚用的是哪个后端
  const confirmGenerateGptDict = () => {
    setShowGenerateConfirm(false);
    void handleGenerateGptDict();
  };

  const ensureSelection = (nextFiles: string[]) => {
    if (nextFiles.length === 0) {
      setSelectedFile(null);
      setDraftText('');
      setDirty(false);
      return;
    }
    setSelectedFile((prev) => (prev && nextFiles.includes(prev) ? prev : nextFiles[0]));
  };

  useEffect(() => {
    if (!selectedFile && activeFiles.length > 0) {
      const first = activeFiles[0];
      setSelectedFile(first);
      const next = data?.dict_contents[first]?.lines.join('\n') ?? '';
      setDraftText(next);
      setDirty(false);
    }
  }, [activeFiles, selectedFile, data]);

  useEffect(() => {
    if (!selectedFile || !selectedContent || dirty) return;
    const next = selectedContent.lines.join('\n');
    if (draftText !== next) {
      setDraftText(next);
    }
  }, [selectedFile, selectedContent, dirty, draftText]);

  const handleSelectFile = (file: string) => {
    if (dirty && !confirm(translate("projects:dictionaryManager.handleSelectFile_confirm_currentFileNotSave"))) {
      return;
    }
    setSelectedFile(file);
    setSectionFilter('');
    const next = data?.dict_contents[file]?.lines.join('\n') ?? '';
    setDraftText(next);
    setDirty(false);
    setInfo(null);
    setLocalError(null);
  };

  const handleTabChange = (tab: DictTab) => {
    if (dirty && !confirm(translate("projects:dictionaryManager.handleTabChange_confirm_currentFileNotSave"))) {
      return;
    }
    setActiveTab(tab);
    setSearchTerm('');
    setSectionFilter('');
    const files = getFilesByTab(data, tab);
    ensureSelection(files);
    if (files.length > 0 && data) {
      setDraftText((data.dict_contents[files[0]]?.lines ?? []).join('\n'));
    }
    setDirty(false);
  };

  const updateRowCell = (rowIndex: number, cellIndex: number, value: string) => {
    const next = [...parsedRows];
    const row = next[rowIndex];
    if (!row || row.type === 'blank') return;
    if (row.type === 'comment' && cellIndex > 0) return;
    const nextValues = [...row.values];
    nextValues[cellIndex] = value;
    next[rowIndex] = { ...row, values: nextValues };
    setDraftText(rowsToText(next));
    setDirty(true);
    setInfo(null);
  };

  const deleteRow = (rowIndex: number) => {
    const next = parsedRows.filter((_, i) => i !== rowIndex);
    setDraftText(rowsToText(next));
    setDirty(true);
    setInfo(null);
  };

  const buildRowByType = (rowType: DictRowType): DictRow => {
    if (rowType === 'gpt') return { type: 'gpt', values: ['', '', ''], raw: '' };
    if (rowType === 'conditional') return { type: 'conditional', values: ['pre_src', '', '', '', ''], raw: '' };
    if (rowType === 'situation') return { type: 'situation', values: ['diag', '', ''], raw: '' };
    if (rowType === 'comment') return { type: 'comment', values: [''], raw: '' };
    return { type: 'normal', values: ['', '', ''], raw: '' };
  };

  const addRow = (rowType?: DictRowType, insertAfterRowIndex?: number) => {
    const targetType = rowType ?? (activeTab === 'gpt' ? 'gpt' : 'normal');
    const base = buildRowByType(targetType);
    let insertIndex = typeof insertAfterRowIndex === 'number' ? Math.max(0, insertAfterRowIndex + 1) : parsedRows.length;
    if (typeof insertAfterRowIndex !== 'number' && activeSection) {
      // 正在看某个类目：新条目加到该分区末尾（跳过分区末尾的空行），否则加到文件末尾就看不到了
      let last = rowSections.lastIndexOf(activeSection);
      while (last > 0 && parsedRows[last].type === 'blank' && rowSections[last - 1] === activeSection) last -= 1;
      insertIndex = last + 1;
    }
    const next = [...parsedRows.slice(0, insertIndex), base, ...parsedRows.slice(insertIndex)];
    setDraftText(rowsToText(next));
    setDirty(true);
    setInfo(null);
  };

  const handleTextEditorKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Tab') return;
    e.preventDefault();
    const target = e.currentTarget;
    const start = target.selectionStart;
    const end = target.selectionEnd;
    const nextValue = `${draftText.slice(0, start)}\t${draftText.slice(end)}`;
    setDraftText(nextValue);
    setDirty(true);
    setInfo(null);
    window.requestAnimationFrame(() => {
      target.setSelectionRange(start + 1, start + 1);
      target.focus();
    });
  };

  const handleSave = async () => {
    if (!selectedFile) return;
    if (activeTab === 'gpt') {
      const invalidRow = parsedRows
        .map((row, index) => ({ row, index }))
        .find(({ row }) => {
          if (row.type !== 'gpt') return false;
          const src = row.values[0]?.trim() ?? '';
          const dst = row.values[1]?.trim() ?? '';
          return !src || !dst;
        });
      if (invalidRow) {
        setLocalError(uiMessage("projects:dictionaryManager.handleSave_setLocalError_gPTDictionarySourceTranslationTextRequired", { value: invalidRow.index + 1 }));
        setInfo(null);
        return;
      }
    }
    setSaving(true);
    setLocalError(null);
    setInfo(null);
    try {
      await onSaveFile(selectedFile, draftText);
      setDirty(false);
      setInfo(uiMessage("projects:dictionaryManager.handleSave_setInfo_doneSave"));
      await onReload();
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : uiMessage("projects:dictionaryManager.handleSave_setLocalError_saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    const raw = newFilename.trim();
    if (!raw) {
      setLocalError(uiMessage("projects:dictionaryManager.handleCreate_setLocalError_fileRequired"));
      return;
    }
    const name = /\.txt$/i.test(raw) ? raw : `${raw}.txt`;
    setCreating(true);
    setLocalError(null);
    setInfo(null);
    try {
      const createdFileKey = await onCreateFile(activeTab, name);
      setNewFilename('');
      setSelectedFile(createdFileKey);
      await onReload();
      setInfo(uiMessage("projects:dictionaryManager.handleCreate_setInfo_doneCreateDictionaryFile"));
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : uiMessage("projects:dictionaryManager.handleCreate_setLocalError_createFailed"));
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedFile) return;
    if (!confirm(translate("projects:dictionaryManager.handleDelete_confirm_deleteDictionaryFile", { value: stripProjectDirMarker(selectedFile) }))) return;
    setDeleting(true);
    setLocalError(null);
    setInfo(null);
    try {
      await onDeleteFile(selectedFile);
      setDirty(false);
      await onReload();
      setInfo(uiMessage("projects:dictionaryManager.handleDelete_setInfo_doneDeleteDictionaryFile"));
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : uiMessage("projects:dictionaryManager.handleDelete_setLocalError_deleteFailed"));
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>{title}</h1></div>
        <LoadingState title={translate("projects:dictionaryManager.projectDictionaryPage_title_loadDictionary")} description={translate("projects:dictionaryManager.projectDictionaryPage_description_pendingReadCurrentDictionaryDirectoryFile")} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>{title}</h1></div>
        <ErrorState title={translate("projects:dictionaryManager.projectDictionaryPage_title_loadDictionaryFailed")} description={error} />
      </div>
    );
  }

  // 二次确认里写清楚用的是哪个后端：项目没单独指定就是全局默认，跟开始翻译同一口径
  const gendicBackendText = formatBackendUsage(
    gendicBackend ?? { backend: translate("projects:dictionaryManager.backend_backend_currentProjectBackendConfig"), model: '', profile: '' },
  );
  const gendicBackendMissing = gendicBackend?.missing === true;

  return (
    <div className="project-dictionary-page">
      <div className="project-dictionary-page__header">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>

      {localError && <InlineFeedback tone="error" title={translate("projects:dictionaryManager.projectDictionaryPage_title_failed")} description={localError} />}
      {info && <InlineFeedback className="inline-alert--floating" tone="success" title={translate("projects:dictionaryManager.projectDictionaryPage_title_success")} description={info} />}

      <div className="project-dictionary-page__content">
        <div className="dict-tabs">
          {(['gpt', 'pre', 'post'] as DictTab[]).map((tab) => (
            <button
              key={tab}
              className={`dict-tab ${activeTab === tab ? 'dict-tab--active' : ''}`}
              type="button"
              onClick={() => handleTabChange(tab)}
            >
              {tab === 'pre' ? translate("projects:dictionaryManager.dictTabs_message_dictionary") : tab === 'gpt' ? translate("projects:dictionaryManager.dictTabs_message_gPTDictionary") : translate("projects:dictionaryManager.dictTabs_message_dictionaryVariant2")}
              <span className="dict-tab__count">{getFilesByTab(data, tab).length}</span>
            </button>
          ))}
          {activeTab === 'gpt' && onGenerateGptDict ? (
            <Button
              variant="secondary"
              onClick={() => setShowGenerateConfirm(true)}
              disabled={generatingGptDict}
              title={translate("projects:dictionaryManager.dictTabs_title_aIGenDicSourceExtractGPTDictionary")}
            >
              <Icon name="bot" />
              {generatingGptDict ? translate("projects:dictionaryManager.dictTabs_message_text") : translate("projects:dictionaryManager.dictTabs_message_aIGPTDictionary")}
            </Button>
          ) : null}
        </div>

        <div className="dict-layout">
          <aside className="dict-layout__sidebar">
            <div className="dict-layout__sidebar-header">
              <h3>{translate("projects:dictionaryManager.dictLayoutSidebarHeader_message_dictionaryFile")}</h3>
              <button
                type="button"
                className={`icon-btn icon-btn--refresh${refreshing ? ' icon-btn--spinning' : ''}`}
                onClick={() => void handleReload()}
                disabled={refreshing}
                title={translate("projects:dictionaryManager.dictLayoutSidebarHeader_title_dictionaryFile")}
                aria-label={translate("projects:dictionaryManager.dictLayoutSidebarHeader_ariaLabel_dictionaryFile")}
              >
                <svg viewBox="0 0 16 16" width="15" height="15" fill="none">
                  <path d="M13.5 8a5.5 5.5 0 11-1.4-3.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  <path d="M12 2v3.5H8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </div>
            <div className="dict-create-file">
              <input
                type="text"
                placeholder={translate("projects:dictionaryManager.dictCreateFile_placeholder_fileCustomPreTxt")}
                value={newFilename}
                onChange={(e) => setNewFilename(e.target.value)}
              />
              <Button onClick={() => void handleCreate()} disabled={creating}>{translate("projects:dictionaryManager.dictCreateFile_message_new")}</Button>
            </div>
            <div className="dict-file-list">
              {activeFiles.map((file) => {
                const content = data?.dict_contents?.[file];
                const isActive = selectedFile === file;
                return (
                  <button
                    key={file}
                    className={`dict-file-item ${isActive ? 'dict-file-item--active' : ''}`}
                    type="button"
                    onClick={() => handleSelectFile(file)}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setContextMenu({ x: e.clientX, y: e.clientY, file });
                    }}
                  >
                    <span className="dict-file-item__name">{stripProjectDirMarker(file)}</span>
                    {content && <span className="dict-file-item__count">{translate("projects:dictionaryManager.dictFileList_message_entry", { count: content.count })}</span>}
                  </button>
                );
              })}
              {activeFiles.length === 0 && (
                <EmptyState title={translate("projects:dictionaryManager.dictFileList_title_currentDictionaryFile")} description={translate("projects:dictionaryManager.dictFileList_description_createCountDictionaryFile")} />
              )}
            </div>
          </aside>

          <div className="dict-layout__main">
            {selectedFile ? (
              <Panel
                title={stripProjectDirMarker(selectedFile)}
                description={translate("projects:dictionaryManager.dictLayoutMain_description_entryEntry", { value: selectedContent?.count ?? 0, value2: selectedContent?.path ?? '' })}
                actions={(
                  <div className="dict-panel-actions">
                    <Button variant="secondary" onClick={() => setMode(mode === 'card' ? 'text' : 'card')}>
                      {mode === 'card' ? translate("projects:dictionaryManager.dictPanelActions_message_text") : translate("projects:dictionaryManager.dictPanelActions_message_textVariant2")}
                    </Button>
                    <Button variant="secondary" onClick={() => void handleDelete()} disabled={deleting}>{translate("projects:dictionaryManager.dictPanelActions_message_deleteFile")}</Button>
                    <Button onClick={() => void handleSave()} disabled={saving || !dirty}>{translate("common:actions.save")}</Button>
                  </div>
                )}
              >
                <div className="dict-toolbar">
                  <input
                    type="text"
                    placeholder={translate("projects:dictionaryManager.dictToolbar_placeholder_searchDictionaryEntry")}
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    className="dict-search"
                  />
                  {mode === 'card' && (
                    activeTab === 'gpt' ? (
                      <Button variant="secondary" onClick={() => addRow('gpt')}>{translate("projects:dictionaryManager.dictToolbar_message_entry")}</Button>
                    ) : (
                      <>
                        <Button variant="secondary" onClick={() => addRow('normal')}>{translate("projects:dictionaryManager.dictToolbar_message_entryVariant2")}</Button>
                        <Button variant="secondary" onClick={() => addRow('conditional')}>{translate("projects:dictionaryManager.dictToolbar_message_entryEntry")}</Button>
                      </>
                    )
                  )}
                </div>

                {mode === 'text' ? (
                  <textarea
                    className="dict-text-editor"
                    value={draftText}
                    onChange={(e) => {
                      setDraftText(e.target.value);
                      setDirty(true);
                      setInfo(null);
                    }}
                    onKeyDown={handleTextEditorKeyDown}
                    spellCheck={false}
                  />
                ) : (
                  <div className="dict-card-mode">
                    {/* 一条都没筛出来时卡片不渲染，胶囊单独占一行——否则筛选没法取消 */}
                    {groupedRows.length === 0 && sectionPills ? (
                      <div className="dict-card dict-card--filters">
                        <div className="dict-card__header">{sectionPills}</div>
                      </div>
                    ) : null}
                    <div className="dict-card-list">
                      {groupedRows.map((group, groupIndex) => (
                        <DictEntryGroupCard
                          key={`${groupIndex}-${group.type}-${group.items[0]?.rowIndex ?? 0}`}
                          group={group}
                          tab={activeTab}
                          headerExtra={groupIndex === 0 ? sectionPills : undefined}
                          onCellChange={updateRowCell}
                          onDelete={deleteRow}
                          onAddRow={addRow}
                          onOpenInCache={onOpenInCache}
                        />
                      ))}
                      {groupedRows.length === 0 && (
                        <EmptyState
                          title={searchTerm.trim() || activeSection ? translate("projects:dictionaryManager.dictCardList_title_matchEntry") : translate("projects:dictionaryManager.dictCardList_title_dictionary")}
                          description={searchTerm.trim() || activeSection ? translate("projects:dictionaryManager.dictCardList_description_searchEntry") : translate("projects:dictionaryManager.dictCardList_description_buttonAddEntryDictionaryEntry")}
                          action={(
                            activeTab === 'gpt' ? (
                              <Button variant="secondary" onClick={() => addRow('gpt')}>{translate("projects:dictionaryManager.dictCardList_action_entry")}</Button>
                            ) : (
                              <div className="dict-empty-actions">
                                <Button variant="secondary" onClick={() => addRow('normal')}>{translate("projects:dictionaryManager.dictEmptyActions_message_entry")}</Button>
                                <Button variant="secondary" onClick={() => addRow('conditional')}>{translate("projects:dictionaryManager.dictEmptyActions_message_entryEntry")}</Button>
                              </div>
                            )
                          )}
                        />
                      )}
                    </div>
                  </div>
                )}
              </Panel>
            ) : (
              <EmptyState title={translate("projects:dictionaryManager.dictLayoutMain_title_selectCountDictionaryFile")} description={translate("projects:dictionaryManager.dictLayoutMain_description_selectDictionaryFileStartEdit")} />
            )}
          </div>
        </div>
      </div>
      {showGenerateConfirm ? (
        <div
          className="dict-dialog-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="gendic-confirm-title"
          onClick={() => setShowGenerateConfirm(false)}
        >
          <div className="dict-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dict-dialog__header">
              <h3 className="dict-dialog__title" id="gendic-confirm-title">
                <Icon name="bot" />{translate("projects:dictionaryManager.dictDialogTitle_h3_aIGPTDictionary")}</h3>
              <p className="dict-dialog__subtitle"><UiTrans k="projects:dictionaryManager.dictDialogHeader_message_00GenDicGPTDictionary" values={{ gendicBackendText: gendicBackendText }} components={[<strong />]} /></p>
            </div>
            <div className="dict-dialog__body">
              <p>{translate("projects:dictionaryManager.dictDialogBody_message_backendCurrentProjectBackendConfigProjectEmpty")}</p>
              <p>{translate("projects:dictionaryManager.dictDialogBody_message_genDicNameTableExtractProjectDirectoryProjectGPT")}</p>
              {gendicBackendMissing ? (
                <p className="dict-dialog__warning">
                  <Icon name="warning" />{translate("projects:dictionaryManager.dictDialogWarning_p_currentEmptyModelConfigFailedModelSettings")}</p>
              ) : null}
            </div>
            <div className="dict-dialog__actions">
              <Button variant="secondary" onClick={() => setShowGenerateConfirm(false)}>{translate("common:actions.cancel")}</Button>
              <Button onClick={confirmGenerateGptDict} disabled={generatingGptDict}>
                <Icon name="play" />{translate("projects:dictionaryManager.dictDialogActions_button_confirm")}</Button>
            </div>
          </div>
        </div>
      ) : null}

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
              const file = contextMenu.file;
              setContextMenu(null);
              void handleRevealFile(file);
            }}
          >
            <span className="cache-context-menu__icon" aria-hidden="true"><Icon name="folder-open" /></span>
            <span className="cache-context-menu__label">{translate("projects:dictionaryManager.cacheContextMenuItem_message_file")}</span>
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
}
