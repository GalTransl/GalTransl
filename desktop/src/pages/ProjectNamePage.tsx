import { useCallback, useEffect, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback, LoadingState } from '../components/page-state';
import {
  type NameEntry,
  fetchNameTable,
  generateNameTable,
  saveNameTable,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

export function ProjectNamePage() {
  const { projectId } = useOutletContext<OutletContext>();

  const [names, setNames] = useState<NameEntry[]>([]);
  const [sourceFile, setSourceFile] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [debouncedSearch, setDebouncedSearch] = useState('');

  // Debounced search
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [searchQuery]);

  const loadData = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchNameTable(projectId);
      setNames(res.names);
      setSourceFile(res.source_file);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '加载人名表失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleGenerate = useCallback(async () => {
    if (!projectId) return;
    setGenerating(true);
    setError(null);
    try {
      const res = await generateNameTable(projectId);
      setNames(res.names);
      setSourceFile(res.source_file);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '生成人名表失败'));
    } finally {
      setGenerating(false);
    }
  }, [projectId]);

  const handleSave = useCallback(async () => {
    if (!projectId) return;
    setSaving(true);
    setError(null);
    try {
      await saveNameTable(projectId, names);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '保存人名表失败'));
    } finally {
      setSaving(false);
    }
  }, [projectId, names]);

  const handleDstNameChange = useCallback((index: number, value: string) => {
    setNames((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], dst_name: value };
      return next;
    });
    setDirty(true);
  }, []);

  const handleDeleteRow = useCallback((index: number) => {
    setNames((prev) => prev.filter((_, i) => i !== index));
    setDirty(true);
  }, []);

  const handleAddRow = useCallback(() => {
    setNames((prev) => [...prev, { src_name: '', dst_name: '', count: 0 }]);
    setDirty(true);
  }, []);

  const handlePaste = useCallback((e: React.ClipboardEvent, field: 'src_name' | 'dst_name', startIndex: number) => {
    const text = e.clipboardData.getData('text/plain');
    // If the pasted text contains newlines, split and fill multiple rows
    if (text.includes('\n') || text.includes('\t')) {
      e.preventDefault();
      const lines = text.split('\n').filter((l) => l.trim() !== '');
      const newNames = [...names];
      for (let i = 0; i < lines.length; i++) {
        const parts = lines[i].split('\t');
        const targetIndex = startIndex + i;
        if (targetIndex >= newNames.length) {
          // Auto-add rows
          newNames.push({ src_name: '', dst_name: '', count: 0 });
        }
        if (field === 'src_name') {
          newNames[targetIndex] = { ...newNames[targetIndex], src_name: (parts[0] || '').trim() };
          if (parts.length > 1) {
            newNames[targetIndex] = { ...newNames[targetIndex], dst_name: (parts[1] || '').trim() };
          }
        } else {
          newNames[targetIndex] = { ...newNames[targetIndex], dst_name: (parts[0] || '').trim() };
        }
      }
      setNames(newNames);
      setDirty(true);
    }
  }, [names]);

  // Filter by search
  const filteredNames = debouncedSearch
    ? names.filter((n) =>
        n.src_name.toLowerCase().includes(debouncedSearch.toLowerCase()) ||
        n.dst_name.toLowerCase().includes(debouncedSearch.toLowerCase())
      )
    : names;

  const translatedCount = names.filter((n) => n.dst_name.trim() !== '').length;

  if (loading) return <LoadingState />;

  return (
    <div className="page name-page">
      <PageHeader title="人名翻译" description="管理项目的人名替换表，填入中文译名后可用于翻译工作台的 name 字段替换。" />

      <div className="name-page__toolbar">
        <div className="name-page__toolbar-left">
          <Button onClick={handleGenerate} disabled={generating}>
            {generating ? '生成中...' : '从缓存生成人名表'}
          </Button>
          <Button onClick={handleSave} disabled={!dirty || saving} variant="primary">
            {saving ? '保存中...' : '保存'}
          </Button>
        </div>
        <div className="name-page__toolbar-right">
          <input
            type="text"
            className="name-page__search"
            placeholder="搜索人名..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {error && <InlineFeedback tone="error">{error}</InlineFeedback>}

      <Panel title="人名替换表">
        <div className="name-page__stats">
          <span className="name-page__stat">
            共 {names.length} 个人名
          </span>
          <span className="name-page__stat">
            已翻译 {translatedCount} / {names.length}
          </span>
          {sourceFile && (
            <span className="name-page__stat">
              来源: {sourceFile}
            </span>
          )}
        </div>

        {names.length === 0 && !generating ? (
          <EmptyState
            title="尚未生成人名表"
            description="点击「从缓存生成人名表」从当前项目的缓存文件中提取所有人名。"
          />
        ) : (
          <div className="name-page__table-wrap">
            <table className="name-page__table">
              <thead>
                <tr>
                  <th className="name-page__th name-page__th--index">#</th>
                  <th className="name-page__th name-page__th--jp">原名</th>
                  <th className="name-page__th name-page__th--cn">译名</th>
                  <th className="name-page__th name-page__th--count">次数</th>
                  <th className="name-page__th name-page__th--actions" />
                </tr>
              </thead>
              <tbody>
                {filteredNames.map((entry, i) => {
                  // Find original index in unfiltered array
                  const originalIndex = names.indexOf(entry);
                  const hasTranslation = entry.dst_name.trim() !== '';
                  return (
                    <tr key={originalIndex} className={`name-page__row${hasTranslation ? ' name-page__row--translated' : ''}`}>
                      <td className="name-page__td name-page__td--index">{originalIndex + 1}</td>
                      <td className="name-page__td name-page__td--jp">
                        <input
                          type="text"
                          className="name-page__input"
                          value={entry.src_name}
                          onChange={(e) => {
                            const newNames = [...names];
                            newNames[originalIndex] = { ...newNames[originalIndex], src_name: e.target.value };
                            setNames(newNames);
                            setDirty(true);
                          }}
                          onPaste={(e) => handlePaste(e, 'src_name', originalIndex)}
                        />
                      </td>
                      <td className="name-page__td name-page__td--cn">
                        <input
                          type="text"
                          className="name-page__input"
                          value={entry.dst_name}
                          placeholder={entry.src_name ? `输入 ${entry.src_name} 的译名...` : ''}
                          onChange={(e) => handleDstNameChange(originalIndex, e.target.value)}
                          onPaste={(e) => handlePaste(e, 'dst_name', originalIndex)}
                        />
                      </td>
                      <td className="name-page__td name-page__td--count">{entry.count}</td>
                      <td className="name-page__td name-page__td--actions">
                        <button
                          type="button"
                          className="name-page__delete-btn"
                          onClick={() => handleDeleteRow(originalIndex)}
                          title="删除此行"
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="name-page__footer">
          <Button onClick={handleAddRow}>+ 添加人名</Button>
        </div>
      </Panel>
    </div>
  );
}
