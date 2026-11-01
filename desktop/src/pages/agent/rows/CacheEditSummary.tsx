import { memo, useEffect, useId, useState } from 'react';
import { AnimatedDialog } from '../../../components/AnimatedDialog';
import { Button } from '../../../components/Button';
import { Icon } from '../../../components/Icon';
import { usePageActive } from '../../../components/PageActivity';
import { AnimatedCollapse, AnimatePresence } from '../../../components/UiMotion';
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
  const active = usePageActive();
  const filesId = useId();
  const diffTitleId = useId();
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState(edits);
  const [selectedFile, setSelectedFile] = useState<string>();
  const [diff, setDiff] = useState<CacheHistoryFile>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [undoResult, setUndoResult] = useState<CacheUndoResult>();
  const [undoneIds, setUndoneIds] = useState<string[]>([]);
  useEffect(() => { setRecords(edits); }, [edits]);
  useEffect(() => { if (!active) setSelectedFile(undefined); }, [active]);
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
  const undone = !changeIds.length && !pending;
  return <section className={`agent-cache-edits${undone ? ' is-undone' : ''}`} aria-label={translate('agent:cacheHistory.title')}>
    <div className="agent-cache-edits__header">
      <button type="button" className="agent-cache-edits__toggle" aria-expanded={open} aria-controls={filesId} onClick={() => setOpen(value => !value)}>
        <span className="agent-cache-edits__icon"><Icon name={undone ? 'check' : 'pencil'} /></span>
        <span className="agent-cache-edits__heading">
          <span className="agent-cache-edits__title">{translate('agent:cacheHistory.changes')}</span>
          <span className="agent-cache-edits__summary">{translate('agent:cacheHistory.summary', { count: files.length, edits: records.length })}</span>
        </span>
        <Icon name="chevron-down" className="agent-cache-edits__caret" />
      </button>
      <Button variant="secondary" className="agent-cache-edits__action agent-cache-edits__undo" disabled={busy || !changeIds.length || pending} onClick={() => void undo()}>
        <Icon name={undone ? 'check' : 'undo'} />{translate(pending ? 'agent:cacheHistory.pending' : undone ? 'agent:cacheHistory.undone' : busy && !selectedFile ? 'agent:cacheHistory.undoing' : 'agent:cacheHistory.undoTurn')}
      </Button>
    </div>
    <AnimatedCollapse open={open} id={filesId}>{() => <div className="agent-cache-edits__files">
      {files.map(filename => {
        const fileEdits = records.filter(edit => edit.files.some(file => file.filename === filename));
        return <div key={filename} className={`agent-cache-edits__file${selectedFile === filename ? ' is-selected' : ''}`}>
          <span className="agent-cache-edits__file-icon"><Icon name="database" /></span>
          <div className="agent-cache-edits__file-info">
            <span className="agent-cache-edits__filename" title={filename}>{filename}</span>
            <span className="agent-cache-edits__meta">
              <span className="agent-cache-edits__actors">{[...new Set(fileEdits.map(edit => edit.actor_label || translate('agent:cacheHistory.user')))].join('、')}</span>
              <span aria-hidden="true">·</span>
              <span>{translate('agent:cacheHistory.editCount', { count: fileEdits.length })}</span>
            </span>
          </div>
          <Button variant="secondary" className="agent-cache-edits__action" disabled={busy} aria-haspopup="dialog" onClick={() => void showDiff(filename)}>
            <Icon name="file-text" />{translate('agent:cacheHistory.viewDiff')}
          </Button>
        </div>;
      })}
    </div>}</AnimatedCollapse>
    {(error || warning) && <p className="agent-cache-edits__error" role="alert">{error || warning}</p>}
    {undoResult?.status === 'conflict' && <div className="agent-cache-edits__error" role="alert">
      <p><Icon name="warning" />{translate('agent:cacheHistory.conflict')}</p>
      {undoResult.conflicts.map((conflict, index) => <div key={index}>{conflict.file}{conflict.index !== undefined ? ` #${conflict.index}` : ''}{conflict.field ? `.${conflict.field}` : ''}：{conflict.reason}</div>)}
    </div>}
    <AnimatePresence>{selectedFile && <AnimatedDialog key={selectedFile} backdropClassName="agent-cache-edits__backdrop" className="agent-cache-edits__review" labelledBy={diffTitleId} close={() => setSelectedFile(undefined)}>
      <div className="agent-cache-edits__review-header">
        <div className="agent-cache-edits__review-heading">
          <span className="agent-cache-edits__review-label">{translate('agent:cacheHistory.diffPreview')}</span>
          <h2 id={diffTitleId} title={selectedFile}>{selectedFile}</h2>
        </div>
        {diff && <span className="agent-cache-edits__diff-counts"><b className="agent-cache-edits__added">+{diff.added}</b><b className="agent-cache-edits__removed">−{diff.removed}</b></span>}
        <button type="button" className="icon-btn agent-cache-edits__close" aria-label={translate('agent:cacheHistory.closeDiff')} title={translate('agent:cacheHistory.closeDiff')} onClick={() => setSelectedFile(undefined)}><Icon name="close" /></button>
      </div>
      {diff?.diff ? <div className="agent-cache-edits__diff" role="region" aria-label={translate('agent:cacheHistory.diffPreview')} tabIndex={0}>
        {cacheDiffRows(diff.diff).map((row, index) => <div key={index} className={row.kind}>
          <span className="agent-cache-edits__line-number" aria-hidden="true">{row.before}</span>
          <span className="agent-cache-edits__line-number" aria-hidden="true">{row.after}</span>
          <code>{row.text}</code>
        </div>)}
      </div> : <div className="agent-cache-edits__empty" role="status"><Icon name="file-text" />{busy ? translate('agent:cacheHistory.loading') : error || translate('agent:cacheHistory.noDiff')}</div>}
    </AnimatedDialog>}</AnimatePresence>
  </section>;
});

function cacheDiffRows(diff: string) {
  let before = 1;
  let after = 1;
  return diff.split('\n').filter(line => line && !line.startsWith('--- ') && !line.startsWith('+++ ')).map(text => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) {
      before = Number(hunk[1]);
      after = Number(hunk[2]);
      return { text, kind: 'is-hunk', before: '', after: '' };
    }
    if (text.startsWith('\\')) return { text, kind: 'is-meta', before: '', after: '' };
    if (text.startsWith('+')) return { text, kind: 'is-added', before: '', after: after++ };
    if (text.startsWith('-')) return { text, kind: 'is-removed', before: before++, after: '' };
    return { text, kind: 'is-context', before: before++, after: after++ };
  });
}
