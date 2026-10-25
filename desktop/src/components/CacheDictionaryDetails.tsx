import { useEffect, useState } from 'react';
import { t, useUiLanguage } from '../i18n';
import { fetchCacheDictionaryMatches, type CacheDictionarySentence, type CacheDictionaryResult } from '../lib/api';
import { CacheDictionaryTable } from './CacheDictionaryTable';

export function CacheDictionaryDetails({ projectId, configFileName, entry, id }: {
  projectId: string; configFileName: string; entry: CacheDictionarySentence; id: string;
}) {
  useUiLanguage();
  const [result, setResult] = useState<CacheDictionaryResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const sentenceKey = JSON.stringify(entry);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setResult(null);
    fetchCacheDictionaryMatches(projectId, configFileName, [entry], { details: true, offset, signal: controller.signal })
      .then(response => { if (!controller.signal.aborted) setResult(response.results[0]); })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason instanceof Error ? reason.message : reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  // Text edits remount the component in the card, resetting pagination.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, configFileName, sentenceKey, offset, retry]);

  const matches = result?.entries ?? [];
  return <section className="cache-dictionary-details" id={id} aria-label={t('projects:cacheDictionary.details')} aria-busy={loading}>
    <p className="cache-dictionary-details__hint">{t('projects:cacheDictionary.hint')}</p>
    {loading && <p role="status">{t('common:actions.loading')}</p>}
    {error && <div role="alert">{error} <button type="button" onClick={() => setRetry(value => value + 1)}>{t('projects:cacheDictionary.retry')}</button></div>}
    {result && <>
      {(['pre', 'post', 'gpt'] as const).filter(kind => result.counts[kind] > 0 && matches.some(match => match.kind === kind)).map(kind => <div key={kind} className="cache-dictionary-details__group">
        <strong>{t(`projects:cacheDictionary.${kind}`)} · {result.counts[kind]}</strong>
        <CacheDictionaryTable matches={matches.filter(match => match.kind === kind)} />
      </div>)}
      {result.total === 0 && <p>{t('projects:cacheDictionary.empty')}</p>}
      {(result.total ?? 0) > 100 && <div className="cache-dictionary-details__pagination">
        <button type="button" disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 100))}>{t('projects:cacheDictionary.previous')}</button>
        <span>{offset + 1}–{Math.min(offset + matches.length, result.total ?? 0)} / {result.total}</span>
        <button type="button" disabled={offset + matches.length >= (result.total ?? 0)} onClick={() => setOffset(value => value + 100)}>{t('projects:cacheDictionary.next')}</button>
      </div>}
    </>}
  </section>;
}
