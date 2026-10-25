import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Icon } from '../../components/Icon';
import { useUiLanguage } from '../../i18n';
import { extractionApi } from './api';
import { x } from './labels';
import type { FileList, Preview, Session } from './types';

export function Workspace({ session, refresh, running, importProject, busy }: { session: Session; refresh: number; running: boolean; importProject: () => void; busy: boolean }) {
  useUiLanguage();
  const [directory, setDirectory] = useState('');
  const [files, setFiles] = useState<FileList | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewPath, setPreviewPath] = useState('');
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const previewGeneration = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    const token = ++generation.current;
    setFiles(null); setError('');
    void extractionApi<FileList>('files', { session_id: session.id, path: directory }, undefined, controller.signal)
      .then(result => { if (token === generation.current) setFiles(result); })
      .catch(error => { if (!controller.signal.aborted) setError(error.message); });
    return () => { controller.abort(); generation.current++; };
  }, [session.id, directory, refresh, revision]);
  useEffect(() => () => { previewGeneration.current++; }, []);
  const showPreview = async (path: string, offset = 0) => {
    const token = ++previewGeneration.current; setError(''); setLoading(true);
    setPreviewPath(path); setPreviewOpen(true);
    try {
      const result = await extractionApi<Preview>('preview', { session_id: session.id, path, offset });
      if (token === previewGeneration.current) setPreview(result);
    } catch (error) { if (token === previewGeneration.current) { setError(error instanceof Error ? error.message : String(error)); setPreviewOpen(false); } }
    finally { if (token === previewGeneration.current) setLoading(false); }
  };
  const more = async () => {
    if (files?.next_offset == null) return;
    const token = generation.current;
    try {
      const result = await extractionApi<FileList>('files', { session_id: session.id, path: directory, offset: files.next_offset });
      if (token === generation.current) setFiles(current => current ? { ...result, entries: [...current.entries, ...result.entries].slice(-2000) } : result);
    } catch (error) { setError(error instanceof Error ? error.message : String(error)); }
  };
  return <aside className="extract-workspace">
    <header><strong>{x('workspace')}</strong><button title={x('reload')} onClick={() => setRevision(v => v + 1)}><Icon name="refresh" /></button></header>
    <div className="extract-workspace-path"><small>{x('source')}</small><div title={session.game_dir}>{session.game_dir}</div><small>{x('outputDir')}</small><div title={session.workspace}>{session.workspace}</div>
      <button onClick={() => { void invoke('open_folder', { path: session.workspace }).catch(error => setError(String(error))); }}><Icon name="folder" /> {x('openFolder')}</button>
    </div>
    <div className="extract-files-heading"><strong>{x('files')}</strong><span>{files?.total ?? '—'}</span></div>
    <div className="extract-file-list">
      {directory && <button className="extract-file" onClick={() => setDirectory(directory.split('/').slice(0, -1).join('/'))}><Icon name="undo" />{x('back')}</button>}
      <small className="extract-file-path">/{directory}</small>
      {files?.entries.map(entry => <button className={`extract-file${previewOpen && previewPath === [directory, entry.name].filter(Boolean).join('/') ? ' is-active' : ''}`} key={entry.name} title={entry.name} onClick={() => {
        const path = [directory, entry.name].filter(Boolean).join('/');
        if (entry.is_dir) setDirectory(path); else void showPreview(path);
      }}><Icon name={entry.is_dir ? 'folder' : 'note'} /><span>{entry.name}</span>{!entry.is_dir && <small>{Math.ceil(entry.size / 1024)} {x('kilobytes')}</small>}</button>)}
      {files?.entries.length === 0 && <p className="extract-muted">{x('noFiles')}</p>}
      {files?.next_offset != null && <button onClick={() => void more()}>{x('more')}</button>}
    </div>
    {error && <p className="extract-error" role="alert">{error}</p>}
    <footer><button className="extract-primary" onClick={importProject} disabled={running || busy} title={x('importHint')}><Icon name="globe" />{x('importProject')}</button></footer>
    <div className={`extract-preview${previewOpen ? ' is-open' : ''}`} role="dialog" aria-label={x('preview')} aria-hidden={!previewOpen} {...(!previewOpen ? { inert: '' } : {})}>
      <header><strong title={previewPath}>{previewPath || x('loading')}</strong><button onClick={() => { previewGeneration.current++; setPreviewOpen(false); setLoading(false); }} aria-label={x('close')}>×</button></header>
      <pre aria-busy={loading}>{loading ? x('loading') : preview?.content}</pre>
      {preview?.next_offset != null && <button disabled={loading} onClick={() => void showPreview(preview.path, preview.next_offset!)}>{x('more')}</button>}
    </div>
  </aside>;
}
