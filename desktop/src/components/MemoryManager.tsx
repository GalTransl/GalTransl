import { useEffect, useState } from 'react';
import { t, useUiLanguage } from '../i18n';
import { deleteMemory, fetchMemories, type MemoryEntry } from '../lib/api';

export function MemoryManager({ backendUrl, projectDir }: { backendUrl: string; projectDir: string }) {
  useUiLanguage();
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [deleting, setDeleting] = useState('');
  const [confirmation, setConfirmation] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    setEntries([]);
    setError('');
    setConfirmation('');
    fetchMemories(projectDir).then(result => {
      if (active) setEntries(result.entries);
    }).catch(error => {
      if (active) setError(String(error));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [backendUrl, projectDir, reload]);

  const remove = (id: string) => {
    setDeleting(id);
    setError('');
    return deleteMemory(projectDir, id).then(() => {
      setEntries(entries => entries.filter(entry => entry.id !== id));
      setConfirmation('');
    }).catch(error => setError(String(error))).finally(() => setDeleting(''));
  };

  return <section className="agent-context-settings memory-manager" aria-busy={loading || Boolean(deleting)}>
    <div className="agent-context-settings__header">
      <h3>{t('common:mnemonSettings.entries')}</h3>
      <button type="button" disabled={loading || Boolean(deleting)} onClick={() => setReload(value => value + 1)}>{t('common:mnemonSettings.refresh')}</button>
    </div>
    <p className="agent-context-settings__hint">{t('common:mnemonSettings.auditHint')}</p>
    {projectDir && <p className="memory-manager__project">{projectDir}</p>}
    {loading && <p role="status">{t('common:mnemonSettings.loading')}</p>}
    {error && <p className="agent-context-settings__error" role="alert">{error}</p>}
    {!loading && !error && entries.length === 0 && <p>{t('common:mnemonSettings.empty')}</p>}
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
          <button type="button" disabled={Boolean(deleting)} onClick={() => void remove(entry.id)}>{t('common:mnemonSettings.confirm')}</button>
          <button type="button" disabled={Boolean(deleting)} onClick={() => setConfirmation('')}>{t('common:actions.cancel')}</button>
        </div> : <button type="button" disabled={Boolean(deleting)} onClick={() => setConfirmation(entry.id)}>{t('common:mnemonSettings.delete')}</button>}
      </li>)}
    </ul>
  </section>;
}
