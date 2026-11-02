import { useEffect, useId, useState } from 'react';
import { t, useUiLanguage } from '../i18n';
import { deleteMemory, fetchMemories, saveMemory, type MemoryAgent, type MemoryDraft, type MemoryEntry } from '../lib/api';
import { AnimatedDialog } from './AnimatedDialog';
import { Icon } from './Icon';

export function MemoryManagerDialog({ agent, backendUrl, projectDir, close }: { agent: MemoryAgent; backendUrl: string; projectDir: string; close: () => void }) {
  const titleId = useId();
  return <AnimatedDialog backdropClassName="agent-settings-backdrop" className="agent-settings-dialog memory-manager-dialog" labelledBy={titleId} close={close}>
    <header className="agent-settings-dialog__header">
      <h2 id={titleId}>{t(agent === 'translation' ? 'common:mnemonSettings.translationEntries' : 'common:mnemonSettings.extractionEntries')}</h2>
      <button type="button" onClick={close} aria-label={t('common:actions.close')}><Icon name="close" /></button>
    </header>
    <div className="agent-settings-dialog__body">
      <MemoryManager key={`${agent}:${backendUrl}:${projectDir}`} agent={agent} backendUrl={backendUrl} projectDir={projectDir} />
    </div>
  </AnimatedDialog>;
}

export function MemoryManager({ agent, backendUrl, projectDir }: { agent: MemoryAgent; backendUrl: string; projectDir: string }) {
  useUiLanguage();
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [deleting, setDeleting] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [draft, setDraft] = useState<MemoryDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const editorTitleId = useId();
  const busy = saving || Boolean(deleting);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setEntries([]);
    setError('');
    setConfirmation('');
    setDraft(null);
    fetchMemories(projectDir, agent).then(result => {
      if (active) setEntries(result.entries);
    }).catch(error => {
      if (active) setError(String(error));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [agent, backendUrl, projectDir, reload]);

  const remove = (id: string) => {
    setDeleting(id);
    setError('');
    return deleteMemory(projectDir, id, agent).then(() => {
      setEntries(entries => entries.filter(entry => entry.id !== id));
      setConfirmation('');
    }).catch(error => setError(String(error))).finally(() => setDeleting(''));
  };

  const save = () => {
    if (!draft) return;
    setSaving(true);
    setError('');
    return saveMemory(projectDir, { ...draft, content: draft.content.trim() }, agent).then(result => {
      setEntries(entries => entries.some(entry => entry.id === result.entry.id)
        ? entries.map(entry => entry.id === result.entry.id ? result.entry : entry)
        : [result.entry, ...entries]);
      setDraft(null);
    }).catch(error => setError(String(error))).finally(() => setSaving(false));
  };

  return <section className="memory-manager" aria-busy={loading || busy}>
    <div className="memory-manager__toolbar">
      <button type="button" className="extract-primary" disabled={loading || busy || Boolean(draft)} onClick={() => {
        setConfirmation('');
        setError('');
        setDraft({ content: '', scope: projectDir ? 'project' : 'global', importance: 'normal' });
      }}>{t('common:mnemonSettings.add')}</button>
      <button type="button" disabled={loading || busy || Boolean(draft)} onClick={() => setReload(value => value + 1)}>{t('common:mnemonSettings.refresh')}</button>
    </div>
    <p className="agent-context-settings__hint">{t('common:mnemonSettings.auditHint')}</p>
    {projectDir && <p className="memory-manager__project">{projectDir}</p>}
    {loading && <p role="status">{t('common:mnemonSettings.loading')}</p>}
    {error && <p className="agent-context-settings__error" role="alert">{error}</p>}
    {draft && <form className="memory-manager__editor" aria-labelledby={editorTitleId} onSubmit={event => { event.preventDefault(); void save(); }}>
      <h3 id={editorTitleId}>{t(draft.id ? 'common:mnemonSettings.edit' : 'common:mnemonSettings.add')}</h3>
      <div className="memory-manager__fields">
        <label>{t('common:mnemonSettings.scope')}
          <select value={draft.scope} disabled={saving || Boolean(draft.id)} onChange={event => setDraft({ ...draft, scope: event.target.value === 'global' ? 'global' : 'project' })}>
            <option value="global">{t('common:mnemonSettings.global')}</option>
            <option value="project" disabled={!projectDir}>{t('common:mnemonSettings.project')}</option>
          </select>
        </label>
        <label>{t('common:mnemonSettings.importance')}
          <select value={draft.importance} disabled={saving} onChange={event => setDraft({ ...draft, importance: event.target.value === 'critical' ? 'critical' : 'normal' })}>
            <option value="normal">{t('common:mnemonSettings.normal')}</option>
            <option value="critical">{t('common:mnemonSettings.critical')}</option>
          </select>
        </label>
      </div>
      <label>{t('common:mnemonSettings.content')}
        <textarea autoFocus required maxLength={1000} rows={6} value={draft.content} disabled={saving}
          placeholder={t('common:mnemonSettings.placeholder')} onChange={event => setDraft({ ...draft, content: event.target.value })} />
      </label>
      <p className="agent-context-settings__hint">{t('common:mnemonSettings.editorHint', { count: draft.content.length })}</p>
      {draft.scope === 'global' && <p className="agent-context-settings__hint">{t('common:mnemonSettings.globalSaveHint')}</p>}
      <div className="memory-manager__actions">
        <button type="submit" className="extract-primary" disabled={saving || !draft.content.trim()}>{t(saving ? 'common:mnemonSettings.saving' : draft.id ? 'common:mnemonSettings.save' : 'common:mnemonSettings.add')}</button>
        <button type="button" disabled={saving} onClick={() => { setDraft(null); setError(''); }}>{t('common:actions.cancel')}</button>
      </div>
    </form>}
    {!loading && !error && !draft && entries.length === 0 && <p>{t('common:mnemonSettings.empty')}</p>}
    <ul className="memory-manager__list">
      {entries.map(entry => <li key={entry.id}>
        <div className="memory-manager__meta">
          <span>{t(entry.scope === 'global' ? 'common:mnemonSettings.global' : 'common:mnemonSettings.project')}</span>
          <span>{t(entry.importance === 'critical' ? 'common:mnemonSettings.critical' : 'common:mnemonSettings.normal')}</span>
          <time dateTime={entry.updated_at}>{entry.updated_at}</time>
        </div>
        <p className="memory-manager__content">{entry.content}</p>
        <code>{entry.id}</code>
        {confirmation === entry.id ? <div className="memory-manager__actions">
          <p>{t(entry.scope === 'global' ? 'common:mnemonSettings.confirmGlobalDelete' : 'common:mnemonSettings.confirmDelete')}</p>
          <button type="button" disabled={busy} onClick={() => void remove(entry.id)}>{t('common:mnemonSettings.confirm')}</button>
          <button type="button" disabled={busy} onClick={() => setConfirmation('')}>{t('common:actions.cancel')}</button>
        </div> : <div className="memory-manager__actions">
          <button type="button" disabled={busy || Boolean(draft)} onClick={() => {
            setError('');
            setConfirmation('');
            setDraft({ id: entry.id, content: entry.content, scope: entry.scope, importance: entry.importance });
          }}>{t('common:mnemonSettings.edit')}</button>
          <button type="button" disabled={busy || Boolean(draft)} onClick={() => setConfirmation(entry.id)}>{t('common:mnemonSettings.delete')}</button>
        </div>}
      </li>)}
    </ul>
  </section>;
}
