import { memo, useEffect, useState } from 'react';
import { Icon } from '../../../components/Icon';
import { invalidateCacheFilesForToolResult } from '../../../components/AgentCacheRef';
import { t as translate, useUiLanguage } from '../../../i18n';
import { diffCacheHistory, listCacheHistory, undoCacheHistory, type CacheEdit, type CacheHistoryFile, type CacheUndoResult } from '../../../lib/api';

export const CacheEditSummary = memo(function CacheEditSummary({ edits, projectDir, sessionId, turnId, warning }: {
  edits: CacheEdit[];
  projectDir: string;
  sessionId: string;
  turnId: string;
  warning?: string;
}) {
  useUiLanguage();
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState(edits);
  const [selectedFile, setSelectedFile] = useState<string>();
  const [diff, setDiff] = useState<CacheHistoryFile>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [undoResult, setUndoResult] = useState<CacheUndoResult>();
  const [undoneIds, setUndoneIds] = useState<string[]>([]);
  useEffect(() => { setRecords(edits); }, [edits]);
  useEffect(() => {
    let active = true;
    const load = async () => {
      const result: CacheEdit[] = [];
      let offset = 0;
      while (active) {
        const page = await listCacheHistory(projectDir, sessionId, turnId, offset);
        result.push(...page.items);
        if (!page.has_more) break;
        offset = page.next_offset!;
      }
      if (active) setRecords(result);
    };
    void load().catch(cause => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [projectDir, sessionId, turnId, edits]);
  const files = [...new Set(records.flatMap(edit => edit.files.map(file => file.filename)))];
  const changeIds = records.filter(edit => edit.status === 'applied'
    && (!edit.reverted_by || records.some(record => record.change_id === edit.reverted_by))
    && !undoneIds.includes(edit.change_id)).map(edit => edit.change_id);
  const pending = records.some(edit => edit.status !== 'applied');
  const undo = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await undoCacheHistory(projectDir, changeIds, sessionId);
      setUndoResult(result);
      if (result.status === 'reverted' || result.status === 'no_change') {
        setUndoneIds(previous => [...previous, ...changeIds]);
        setDiff(undefined);
        invalidateCacheFilesForToolResult(result);
      }
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  const showDiff = async (filename: string) => {
    setSelectedFile(filename);
    setDiff(undefined);
    setBusy(true);
    setError('');
    try {
      const result = await diffCacheHistory(projectDir, records.filter(edit => edit.status === 'applied').map(edit => edit.change_id), filename);
      setDiff(result.items[0]);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  };
  if (!files.length && !warning) return null;
  return <section className="agent-cache-edits" aria-label={translate('agent:cacheHistory.title')}>
    <div className="agent-cache-edits__header">
      <button type="button" className="agent-cache-edits__toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        <Icon name={open ? 'chevron-down' : 'chevron-right'} />
        {translate('agent:cacheHistory.summary', { count: files.length, edits: records.length })}
      </button>
      <button type="button" className="btn btn--ghost" disabled={busy || !changeIds.length || pending} onClick={() => void undo()}>
        <Icon name="undo" />{translate(pending ? 'agent:cacheHistory.pending' : changeIds.length ? 'agent:cacheHistory.undoTurn' : 'agent:cacheHistory.undone')}
      </button>
    </div>
    {open && <div className="agent-cache-edits__files">
      {files.map(filename => <div key={filename} className="agent-cache-edits__file">
        <Icon name="database" /><span className="agent-cache-edits__filename">{filename}</span>
        <span className="agent-cache-edits__actors">{[...new Set(records.filter(edit => edit.files.some(file => file.filename === filename)).map(edit => edit.actor_label || translate('agent:cacheHistory.user')))].join('、')}</span>
        <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void showDiff(filename)}>{translate('agent:cacheHistory.viewDiff')}</button>
      </div>)}
    </div>}
    {(error || warning) && <p className="agent-cache-edits__error" role="alert">{error || warning}</p>}
    {undoResult?.status === 'conflict' && <div className="agent-cache-edits__error" role="alert">
      <p>{translate('agent:cacheHistory.conflict')}</p>
      {undoResult.conflicts.map((conflict, index) => <div key={index}>{conflict.file}{conflict.index !== undefined ? ` #${conflict.index}` : ''}{conflict.field ? `.${conflict.field}` : ''}：{conflict.reason}</div>)}
    </div>}
    {selectedFile && <div className="agent-cache-edits__review">
      <div className="agent-cache-edits__review-header"><strong>{selectedFile}</strong>
        {diff && <span><b className="agent-cache-edits__added">+{diff.added}</b> <b className="agent-cache-edits__removed">-{diff.removed}</b></span>}
        <button type="button" className="btn btn--ghost" onClick={() => setSelectedFile(undefined)}>{translate('agent:cacheHistory.closeDiff')}</button>
      </div>
      <pre className="agent-cache-edits__diff">{diff ? diff.diff.split('\n').map((line, index) => <span key={index} className={line.startsWith('+') ? 'is-added' : line.startsWith('-') ? 'is-removed' : line.startsWith('@@') ? 'is-hunk' : undefined}>{line}</span>) : translate('agent:cacheHistory.loading')}</pre>
    </div>}
  </section>;
});
