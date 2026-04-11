import { useEffect, useMemo, useState } from 'react';
import { Button } from './Button';
import { Panel } from './Panel';
import type { DictFileContent, DictionaryCategory } from '../lib/api';

type DictTab = DictionaryCategory;
type DictRowType = 'normal' | 'conditional' | 'situation' | 'gpt' | 'comment' | 'blank';

type DictRow = {
  type: DictRowType;
  values: string[];
  raw: string;
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
};

function getFilesByTab(data: DictionaryManagerData | null, tab: DictTab): string[] {
  if (!data) return [];
  if (tab === 'pre') return data.pre_dict_files;
  if (tab === 'gpt') return data.gpt_dict_files;
  return data.post_dict_files;
}

function parseRows(text: string, tab: DictTab): DictRow[] {
  const lines = text.split('\n');
  return lines.map((line) => {
    if (!line.trim()) return { type: 'blank', values: [], raw: line };
    if (line.startsWith('//') || line.startsWith('\\\\')) {
      return { type: 'comment', values: [line], raw: line };
    }
    const parts = line.split('\t');
    if (tab === 'gpt') {
      const [src = '', dst = '', ...notes] = parts;
      return { type: 'gpt', values: [src, dst, notes.join('\t')], raw: line };
    }
    if (
      parts.length >= 4
      && ['pre_jp', 'post_jp', 'pre_zh', 'post_zh'].includes(parts[0])
    ) {
      const [target = '', cond = '', search = '', ...replace] = parts;
      return { type: 'conditional', values: [target, cond, search, replace.join('\t')], raw: line };
    }
    if (parts.length >= 3 && ['diag', 'mono'].includes(parts[0])) {
      const [scene = '', search = '', ...replace] = parts;
      return { type: 'situation', values: [scene, search, replace.join('\t')], raw: line };
    }
    const [search = '', ...replace] = parts;
    return { type: 'normal', values: [search, replace.join('\t')], raw: line };
  });
}

function rowsToText(rows: DictRow[]): string {
  return rows.map((row) => {
    if (row.type === 'blank') return '';
    if (row.type === 'comment') return row.values[0] ?? row.raw;
    return row.values.join('\t');
  }).join('\n');
}

export function DictionaryManager(props: DictionaryManagerProps) {
  const { data, loading, error, onReload, onCreateFile, onSaveFile, onDeleteFile, title, description } = props;

  const [activeTab, setActiveTab] = useState<DictTab>('pre');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [mode, setMode] = useState<'table' | 'text'>('table');
  const [draftText, setDraftText] = useState<string>('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(false);
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
    if (!searchTerm.trim()) return parsedRows.map((row, rowIndex) => ({ row, rowIndex }));
    const needle = searchTerm.toLowerCase();
    return parsedRows
      .map((row, rowIndex) => ({ row, rowIndex }))
      .filter(({ row }) => row.values.join('\t').toLowerCase().includes(needle));
  }, [parsedRows, searchTerm]);

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

  const addRow = () => {
    const base: DictRow = activeTab === 'gpt'
      ? { type: 'gpt', values: ['', '', ''], raw: '' }
      : { type: 'normal', values: ['', ''], raw: '' };
    const next = [...parsedRows, base];
    setDraftText(rowsToText(next));
    setDirty(true);
    setInfo(null);
  };

  const handleSave = async () => {
    if (!selectedFile) return;
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
    const name = newFilename.trim();
    if (!name) {
      setLocalError('文件名不能为空');
      return;
    }
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
    if (!confirm(`确定删除字典文件「${selectedFile}」？`)) return;
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
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>{title}</h1></div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  return (
    <div className="project-dictionary-page">
      <div className="project-dictionary-page__header">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>

      {localError && <div className="inline-alert inline-alert--error" role="alert">{localError}</div>}
      {info && <div className="inline-alert inline-alert--success" role="status">{info}</div>}

      <div className="project-dictionary-page__content">
        <div className="dict-tabs">
          {(['pre', 'gpt', 'post'] as DictTab[]).map((tab) => (
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
        </div>

        <div className="dict-layout">
          <aside className="dict-layout__sidebar">
            <div className="dict-layout__sidebar-header">
              <h3>字典文件</h3>
              <Button variant="secondary" onClick={() => void onReload()}>刷新</Button>
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
                    <span className="dict-file-item__name">{file}</span>
                    {content && <span className="dict-file-item__count">{content.count}条</span>}
                  </button>
                );
              })}
              {activeFiles.length === 0 && (
                <div className="empty-state">
                  <strong>当前分类无字典文件</strong>
                  <span>请先创建一个字典文件。</span>
                </div>
              )}
            </div>
          </aside>

          <div className="dict-layout__main">
            {selectedFile ? (
              <Panel
                title={selectedFile}
                description={`${selectedContent?.count ?? 0} 条有效条目 · ${selectedContent?.path ?? ''}`}
                actions={(
                  <div className="dict-panel-actions">
                    <Button variant="secondary" onClick={() => setMode(mode === 'table' ? 'text' : 'table')}>
                      {mode === 'table' ? '切换纯文本' : '切换表格'}
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
                    spellCheck={false}
                  />
                ) : (
                  <div className="dict-table-wrap">
                    <table className="dict-table">
                      <thead>
                        <tr>
                          <th>类型</th>
                          <th>列1</th>
                          <th>列2</th>
                          <th>列3</th>
                          <th>列4</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredRows.map(({ row, rowIndex }) => (
                          <tr key={`${rowIndex}-${row.raw}`}>
                            <td>
                              <span className={`dict-row-type dict-row-type--${row.type}`}>{row.type}</span>
                            </td>
                            {[0, 1, 2, 3].map((col) => (
                              <td key={col}>
                                  <input
                                    className="dict-cell-input"
                                    value={row.values[col] ?? ''}
                                    disabled={row.type === 'blank' || (row.type === 'comment' && col > 0)}
                                  onChange={(e) => updateRowCell(rowIndex, col, e.target.value)}
                                  />
                                </td>
                              ))}
                            <td>
                              <button type="button" className="dict-row-delete" onClick={() => deleteRow(rowIndex)}>删除行</button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <div className="form-actions" style={{ marginTop: 12 }}>
                      <Button variant="secondary" onClick={addRow}>+ 新增行</Button>
                    </div>
                  </div>
                )}
              </Panel>
            ) : (
              <div className="empty-state">
                <strong>选择一个字典文件</strong>
                <span>从左侧选择字典文件开始编辑。</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
