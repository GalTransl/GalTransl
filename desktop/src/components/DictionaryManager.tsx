import { useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { Button } from './Button';
import { Panel } from './Panel';
import { EmptyState, ErrorState, InlineFeedback, LoadingState } from './page-state';
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

function parseRows(text: string, tab: DictTab): DictRow[] {
  const lines = text.split('\n');
  return lines.map((line) => {
    if (!line.trim() && !line.includes('\t')) return { type: 'blank', values: [], raw: line };
    if (line.startsWith('//') || line.startsWith('#') || line.startsWith('\\\\')) {
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
  if (type === 'comment') return '注释';
  if (type === 'blank') return '空行';
  if (type === 'gpt') return 'GPT';
  if (type === 'normal') return '普通';
  if (type === 'conditional') return '条件';
  if (type === 'situation') return '场景';
  return type;
}

/** Field labels for each row type */
function getFieldLabels(type: DictRowType, _tab: DictTab): string[] {
  if (type === 'gpt') return ['原文', '译文', '解释(可空)'];
  if (type === 'normal') return ['搜索', '替换', '备注'];
  if (type === 'conditional') return ['目标', '条件', '搜索', '替换', '备注'];
  if (type === 'situation') return ['场景', '搜索', '替换'];
  if (type === 'comment') return ['内容'];
  return [];
}

/* ── Grouped dict entries card ── */
function DictEntryGroupCard({
  group,
  tab,
  onCellChange,
  onDelete,
  onAddRow,
}: {
  group: DictRowGroup;
  tab: DictTab;
  onCellChange: (rowIndex: number, cellIndex: number, value: string) => void;
  onDelete: (rowIndex: number) => void;
  onAddRow: (rowType: DictRowType, insertAfterRowIndex: number) => void;
}) {
  const labels = getFieldLabels(group.type, tab);
  const tableStyle = { '--dict-column-count': labels.length } as CSSProperties;

  return (
    <article className={`dict-card dict-card--${group.type} dict-card--grouped`}>
      <div className="dict-card__header">
        <div className="dict-card__badges">
          <span className={`dict-card__pill dict-card__pill--${group.type}`}>
            {getTypeLabel(group.type, tab)}
          </span>
          <span className="dict-card__pill dict-card__pill--index">{group.items.length}条</span>
        </div>
      </div>

      <div className="dict-card__table" style={tableStyle}>
        <div className="dict-card__table-head">
          <div className="dict-card__head-cell dict-card__head-cell--index">ID</div>
          {labels.map((label, ci) => (
            <div key={ci} className="dict-card__head-cell">{label || `列${ci + 1}`}</div>
          ))}
        </div>

        {group.items.map(({ row, rowIndex }) => (
          <div key={`${rowIndex}`} className="dict-card__table-row">
            <div className="dict-card__cell dict-card__cell--index">#{rowIndex + 1}</div>
            {labels.map((label, ci) => (
              <div key={ci} className="dict-card__cell">
                <input
                  className="dict-card__input"
                  value={row.values[ci] ?? ''}
                  onChange={(e) => onCellChange(rowIndex, ci, e.target.value)}
                  placeholder={label || `列${ci + 1}`}
                />
              </div>
            ))}
            <button
              type="button"
              className="dict-card__row-delete"
              onClick={() => onDelete(rowIndex)}
              title="删除此条"
            >
              ✕
            </button>
          </div>
        ))}

        <div className="dict-card__table-add-row">
          <button
            type="button"
            className="dict-card__add-row-btn"
            onClick={() => onAddRow(group.type, group.items[group.items.length - 1]?.rowIndex ?? -1)}
            title="新增同类型条目"
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
  const {
    data,
    loading,
    error,
    onReload,
    onCreateFile,
    onSaveFile,
    onDeleteFile,
    onGenerateGptDict,
    title,
    description,
  } = props;

  const [activeTab, setActiveTab] = useState<DictTab>('gpt');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [mode, setMode] = useState<'card' | 'text'>('card');
  const [draftText, setDraftText] = useState<string>('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [generatingGptDict, setGeneratingGptDict] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [newFilename, setNewFilename] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const activeFiles = useMemo(() => getFilesByTab(data, activeTab), [data, activeTab]);

  const selectedContent = useMemo(() => {
    if (!data || !selectedFile) return null;
    return data.dict_contents[selectedFile] ?? null;
  }, [data, selectedFile]);

  const parsedRows = useMemo(() => parseRows(draftText, activeTab), [draftText, activeTab]);

  const filteredRows = useMemo(() => {
    const visible = parsedRows
      .map((row, rowIndex) => ({ row, rowIndex }))
      .filter(({ row }) => {
        // 过滤掉注释行（// 或 # 开头）
        if (row.type === 'comment') return false;
        // 过滤掉空行
        if (row.type === 'blank') return false;
        // 过滤掉少于 1 个 tab 分隔的行（即没有 tab 的行）
        if (!row.raw.includes('\t')) return false;
        return true;
      });
    if (!searchTerm.trim()) return visible;
    const needle = searchTerm.toLowerCase();
    return visible.filter(({ row }) => row.values.join('\t').toLowerCase().includes(needle));
  }, [parsedRows, searchTerm]);

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

  const handleGenerateGptDict = async () => {
    if (!onGenerateGptDict || generatingGptDict) return;
    setGeneratingGptDict(true);
    setLocalError(null);
    setInfo(null);
    try {
      await onGenerateGptDict();
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : '启动 AI 生成 GPT 字典任务失败');
    } finally {
      setGeneratingGptDict(false);
    }
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
    if (dirty && !confirm('当前文件有未保存改动，切换会丢失改动，是否继续？')) {
      return;
    }
    setSelectedFile(file);
    const next = data?.dict_contents[file]?.lines.join('\n') ?? '';
    setDraftText(next);
    setDirty(false);
    setInfo(null);
    setLocalError(null);
  };

  const handleTabChange = (tab: DictTab) => {
    if (dirty && !confirm('当前文件有未保存改动，切换分类会丢失改动，是否继续？')) {
      return;
    }
    setActiveTab(tab);
    setSearchTerm('');
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
    const insertIndex = typeof insertAfterRowIndex === 'number' ? Math.max(0, insertAfterRowIndex + 1) : parsedRows.length;
    const next = [...parsedRows.slice(0, insertIndex), base, ...parsedRows.slice(insertIndex)];
    setDraftText(rowsToText(next));
    setDirty(true);
    setInfo(null);
  };

  const handleTextEditorKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
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
        setLocalError(`GPT字典第 ${invalidRow.index + 1} 行的原文和译文不能为空`);
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
      setInfo('已保存');
      await onReload();
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    const raw = newFilename.trim();
    if (!raw) {
      setLocalError('文件名不能为空');
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
      setInfo('已创建字典文件');
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : '创建失败');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedFile) return;
    if (!confirm(`确定删除字典文件「${stripProjectDirMarker(selectedFile)}」？`)) return;
    setDeleting(true);
    setLocalError(null);
    setInfo(null);
    try {
      await onDeleteFile(selectedFile);
      setDirty(false);
      await onReload();
      setInfo('已删除字典文件');
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>{title}</h1></div>
        <LoadingState title="加载字典中…" description="正在读取当前字典目录与文件内容。" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>{title}</h1></div>
        <ErrorState title="加载字典失败" description={error} />
      </div>
    );
  }

  return (
    <div className="project-dictionary-page">
      <div className="project-dictionary-page__header">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>

      {localError && <InlineFeedback tone="error" title="操作失败" description={localError} />}
      {info && <InlineFeedback className="inline-alert--floating" tone="success" title="操作成功" description={info} />}

      <div className="project-dictionary-page__content">
        <div className="dict-tabs">
          {(['gpt', 'pre', 'post'] as DictTab[]).map((tab) => (
            <button
              key={tab}
              className={`dict-tab ${activeTab === tab ? 'dict-tab--active' : ''}`}
              type="button"
              onClick={() => handleTabChange(tab)}
            >
              {tab === 'pre' ? '译前字典' : tab === 'gpt' ? 'GPT字典' : '译后字典'}
              <span className="dict-tab__count">{getFilesByTab(data, tab).length}</span>
            </button>
          ))}
          {activeTab === 'gpt' && onGenerateGptDict ? (
            <Button
              variant="secondary"
              onClick={() => void handleGenerateGptDict()}
              disabled={generatingGptDict}
            >
              {generatingGptDict ? '启动中…' : 'AI生成GPT字典'}
            </Button>
          ) : null}
        </div>

        <div className="dict-layout">
          <aside className="dict-layout__sidebar">
            <div className="dict-layout__sidebar-header">
              <h3>字典文件</h3>
              <button
                type="button"
                className={`icon-btn icon-btn--refresh${refreshing ? ' icon-btn--spinning' : ''}`}
                onClick={() => void handleReload()}
                disabled={refreshing}
                title="刷新字典文件列表"
                aria-label="刷新字典文件列表"
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
                placeholder="新文件名，如 custom_pre.txt"
                value={newFilename}
                onChange={(e) => setNewFilename(e.target.value)}
              />
              <Button onClick={() => void handleCreate()} disabled={creating}>新建</Button>
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
                  >
                    <span className="dict-file-item__name">{stripProjectDirMarker(file)}</span>
                    {content && <span className="dict-file-item__count">{content.count}条</span>}
                  </button>
                );
              })}
              {activeFiles.length === 0 && (
                <EmptyState title="当前分类无字典文件" description="请先创建一个字典文件。" />
              )}
            </div>
          </aside>

          <div className="dict-layout__main">
            {selectedFile ? (
              <Panel
                title={stripProjectDirMarker(selectedFile)}
                description={`${selectedContent?.count ?? 0} 条有效条目 · ${selectedContent?.path ?? ''}`}
                actions={(
                  <div className="dict-panel-actions">
                    <Button variant="secondary" onClick={() => setMode(mode === 'card' ? 'text' : 'card')}>
                      {mode === 'card' ? '切换纯文本' : '切换卡片'}
                    </Button>
                    <Button variant="secondary" onClick={() => void handleDelete()} disabled={deleting}>删除文件</Button>
                    <Button onClick={() => void handleSave()} disabled={saving || !dirty}>保存</Button>
                  </div>
                )}
              >
                <div className="dict-toolbar">
                  <input
                    type="text"
                    placeholder="搜索字典条目…"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    className="dict-search"
                  />
                  {mode === 'card' && (
                    activeTab === 'gpt' ? (
                      <Button variant="secondary" onClick={() => addRow('gpt')}>
                        + 新增条目
                      </Button>
                    ) : (
                      <>
                        <Button variant="secondary" onClick={() => addRow('normal')}>
                          + 普通条目
                        </Button>
                        <Button variant="secondary" onClick={() => addRow('conditional')}>
                          + 条件条目
                        </Button>
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
                    <div className="dict-card-list">
                      {groupedRows.map((group, groupIndex) => (
                        <DictEntryGroupCard
                          key={`${groupIndex}-${group.type}-${group.items[0]?.rowIndex ?? 0}`}
                          group={group}
                          tab={activeTab}
                          onCellChange={updateRowCell}
                          onDelete={deleteRow}
                          onAddRow={addRow}
                        />
                      ))}
                      {groupedRows.length === 0 && (
                        <EmptyState
                          title={searchTerm.trim() ? '无匹配条目' : '字典为空'}
                          description={searchTerm.trim() ? '尝试更换搜索关键词或新增条目。' : '点击下方按钮添加第一条字典条目。'}
                          action={(
                            activeTab === 'gpt' ? (
                              <Button variant="secondary" onClick={() => addRow('gpt')}>+ 新增条目</Button>
                            ) : (
                              <div className="dict-empty-actions">
                                <Button variant="secondary" onClick={() => addRow('normal')}>+ 普通条目</Button>
                                <Button variant="secondary" onClick={() => addRow('conditional')}>+ 条件条目</Button>
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
              <EmptyState title="选择一个字典文件" description="从左侧选择字典文件开始编辑。" />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
