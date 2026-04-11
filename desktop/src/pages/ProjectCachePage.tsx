import { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import {
  ApiError,
  type FileEntry,
  type CacheEntry,
  fetchProjectCache,
  fetchCacheFile,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

export function ProjectCachePage() {
  const { projectDir, projectId } = useOutletContext<OutletContext>();

  const [cacheFiles, setCacheFiles] = useState<FileEntry[]>([]);
  const [cacheDir, setCacheDir] = useState<string>('');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [entries, setEntries] = useState<CacheEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingEntries, setLoadingEntries] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [filterProblems, setFilterProblems] = useState(false);

  // Load cache file list
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    fetchProjectCache(projectId)
      .then((res) => {
        if (!cancelled) {
          setCacheFiles(res.files.filter((f) => f.is_file && f.name.endsWith('.json')));
          setCacheDir(res.cache_dir || '');
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载缓存列表失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId]);

  // Load selected cache file entries
  useEffect(() => {
    if (!projectId || !selectedFile) return;
    let cancelled = false;
    setLoadingEntries(true);
    fetchCacheFile(projectId, selectedFile)
      .then((res) => {
        if (!cancelled) setEntries(res.entries);
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

  const stats = useCallback(() => {
    const total = entries.length;
    const translated = entries.filter((e) => e.pre_zh).length;
    const withProblems = entries.filter((e) => e.problem).length;
    return { total, translated, withProblems };
  }, [entries]);

  if (loading) {
    return (
      <div className="project-cache-page">
        <div className="project-cache-page__header"><h1>缓存浏览</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  const { total, translated, withProblems } = stats();

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
        <p>查看翻译缓存，对比原文与译文，筛选问题句。</p>
      </div>

      {error && <div className="inline-alert inline-alert--error" role="alert">{error}</div>}

      <div className="cache-layout">
        <aside className="cache-layout__sidebar">
          <h3>缓存文件</h3>
          <div className="cache-file-list">
            {cacheFiles.map((file) => (
              <button
                key={file.name}
                className={`cache-file-item ${selectedFile === file.name ? 'cache-file-item--active' : ''}`}
                onClick={() => setSelectedFile(file.name)}
              >
                <span className="cache-file-item__name">{file.name}</span>
                <span className="cache-file-item__size">{formatSize(file.size)}</span>
              </button>
            ))}
          </div>
        </aside>

        <div className="cache-layout__main">
          {selectedFile ? (
            <Panel title={selectedFile} description={`${total} 句 · ${translated} 已翻译 · ${withProblems} 有问题`}>
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
                <div className="cache-table-wrapper">
                  <table className="cache-table">
                    <thead>
                      <tr>
                        <th className="cache-table__idx">#</th>
                        <th className="cache-table__speaker">说话人</th>
                        <th className="cache-table__jp">原文</th>
                        <th className="cache-table__zh">译文</th>
                        <th className="cache-table__problem">问题</th>
                        <th className="cache-table__engine">引擎</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredEntries.map((entry) => (
                        <tr key={entry.index} className={entry.problem ? 'cache-table__row--problem' : ''}>
                          <td className="cache-table__idx">{entry.index}</td>
                          <td className="cache-table__speaker">
                            {Array.isArray(entry.name) ? entry.name.join('/') : entry.name || '—'}
                          </td>
                          <td className="cache-table__jp" title={entry.post_jp}>
                            {truncate(entry.post_jp, 80)}
                          </td>
                          <td className="cache-table__zh" title={entry.pre_zh}>
                            {truncate(entry.pre_zh, 80) || '—'}
                          </td>
                          <td className="cache-table__problem">
                            {entry.problem ? <span className="problem-badge">{entry.problem}</span> : ''}
                          </td>
                          <td className="cache-table__engine">{entry.trans_by || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
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

function truncate(str: string, max: number): string {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '…' : str;
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
