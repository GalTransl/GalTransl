import { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import { speakerStyle } from '../lib/speaker';
import {
  ApiError,
  type FileEntry,
  type CacheEntry,
  fetchProjectCache,
  fetchCacheFile,
  saveCacheFile,
  deleteCacheEntry,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

/* ── Cache Entry Card ── */
function CacheEntryCard({
  entry,
  filename,
  projectId,
  onEntryChange,
  onDelete,
}: {
  entry: CacheEntry;
  filename: string;
  projectId: string;
  onEntryChange: (index: number, field: keyof CacheEntry, value: string) => void;
  onDelete: (index: number) => void;
}) {
  const hasProblem = !!entry.problem;
  const speaker = Array.isArray(entry.name) ? entry.name.join('/') : entry.name || '—';
  const [expanded, setExpanded] = useState(false);

  return (
    <article className={`cache-card ${hasProblem ? 'cache-card--problem' : ''}`}>
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
        <div className={`cache-card__field ${expanded ? 'cache-card__field--textarea' : ''}`}>
          <span className="cache-card__field-label">原文</span>
          {expanded ? (
            <textarea
              className="cache-card__textarea"
              value={entry.post_jp ?? ''}
              onChange={(e) => onEntryChange(entry.index, 'post_jp', e.target.value)}
              placeholder="原文"
              rows={3}
            />
          ) : (
            <input
              className="cache-card__input"
              value={entry.post_jp ?? ''}
              onChange={(e) => onEntryChange(entry.index, 'post_jp', e.target.value)}
              placeholder="原文"
            />
          )}
        </div>
        <div className={`cache-card__field ${expanded ? 'cache-card__field--textarea' : ''}`}>
          <span className="cache-card__field-label">译文</span>
          {expanded ? (
            <textarea
              className="cache-card__textarea cache-card__textarea--zh"
              value={entry.pre_zh ?? ''}
              onChange={(e) => onEntryChange(entry.index, 'pre_zh', e.target.value)}
              placeholder="译文"
              rows={3}
            />
          ) : (
            <input
              className="cache-card__input cache-card__input--zh"
              value={entry.pre_zh ?? ''}
              onChange={(e) => onEntryChange(entry.index, 'pre_zh', e.target.value)}
              placeholder="译文"
            />
          )}
        </div>
      </div>
    </article>
  );
}

/* ── Main Page ── */
export function ProjectCachePage() {
  const { projectId } = useOutletContext<OutletContext>();

  const [cacheFiles, setCacheFiles] = useState<FileEntry[]>([]);
  const [cacheDir, setCacheDir] = useState<string>('');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [entries, setEntries] = useState<CacheEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshingFiles, setRefreshingFiles] = useState(false);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterProblems, setFilterProblems] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

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
    let cancelled = false;
    setLoadingEntries(true);
    fetchCacheFile(projectId, selectedFile)
      .then((res) => {
        if (!cancelled) {
          setEntries(res.entries);
          setDirty(false);
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

  const filteredEntries = entries.filter((e) => {
    if (filterProblems && !e.problem) return false;
    if (searchTerm) {
      const term = searchTerm.toLowerCase();
      return (
        (e.post_jp?.toLowerCase().includes(term)) ||
        (e.pre_zh?.toLowerCase().includes(term))
      );
    }
    return true;
  });

  const total = entries.length;
  const translated = entries.filter((e) => e.pre_zh).length;
  const withProblems = entries.filter((e) => e.problem).length;

  const handleEntryChange = (index: number, field: keyof CacheEntry, value: string) => {
    setEntries((prev) =>
      prev.map((e) => (e.index === index ? { ...e, [field]: value } : e)),
    );
    setDirty(true);
    setInfo(null);
  };

  const handleDelete = async (index: number) => {
    if (!selectedFile) return;
    try {
      setLocalError(null);
      await deleteCacheEntry(projectId, selectedFile, index);
      setEntries((prev) => {
        const next = prev.filter((e) => e.index !== index);
        // Re-index remaining entries
        return next.map((e, i) => ({ ...e, index: i }));
      });
      setInfo('已删除缓存条目');
    } catch (err) {
      setLocalError(getErrorMessage(err, '删除缓存条目失败'));
    }
  };

  const handleSave = async () => {
    if (!selectedFile) return;
    setSaving(true);
    setLocalError(null);
    setInfo(null);
    try {
      await saveCacheFile(projectId, selectedFile, entries);
      setDirty(false);
      setInfo('已保存');
    } catch (err) {
      setLocalError(getErrorMessage(err, '保存缓存失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleSelectFile = (file: string) => {
    if (dirty && !confirm('当前文件有未保存改动，切换会丢失改动，是否继续？')) {
      return;
    }
    setSelectedFile(file);
    setLocalError(null);
    setInfo(null);
  };

  if (loading) {
    return (
      <div className="project-cache-page">
        <div className="project-cache-page__header"><h1>缓存浏览</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  return (
    <div className="project-cache-page">
      <div className="project-cache-page__header">
        <div className="project-cache-page__header-row">
          <h1>缓存浏览</h1>
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
          <div className="cache-layout__sidebar-header">
            <h3>缓存文件</h3>
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
          <div className="cache-file-list">
            {cacheFiles.map((file) => (
              <button
                type="button"
                key={file.name}
                className={`cache-file-item ${selectedFile === file.name ? 'cache-file-item--active' : ''}`}
                onClick={() => handleSelectFile(file.name)}
              >
                <span className="cache-file-item__name">{file.name}</span>
                <span className="cache-file-item__size">{formatSize(file.size)}</span>
              </button>
            ))}
          </div>
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

              {loadingEntries ? (
                <div className="empty-state"><strong>加载中…</strong></div>
              ) : (
                <div className="cache-card-list">
                  {filteredEntries.map((entry) => (
                    <CacheEntryCard
                      key={`${selectedFile}-${entry.index}`}
                      entry={entry}
                      filename={selectedFile}
                      projectId={projectId}
                      onEntryChange={handleEntryChange}
                      onDelete={handleDelete}
                    />
                  ))}
                  {filteredEntries.length === 0 && (
                    <div className="empty-state">
                      <strong>无匹配条目</strong>
                      <span>尝试更换搜索关键词。</span>
                    </div>
                  )}
                </div>
              )}
            </Panel>
          ) : (
            <div className="empty-state">
              <strong>选择一个缓存文件</strong>
              <span>从左侧选择缓存文件查看翻译内容。</span>
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
