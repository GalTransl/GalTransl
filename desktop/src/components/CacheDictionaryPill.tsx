import { t, useUiLanguage } from '../i18n';
import type { CacheDictionaryCounts } from '../lib/api';

export function CacheDictionaryPill({ counts, error, expanded, controls, onToggle }: {
  counts?: CacheDictionaryCounts;
  error: string;
  expanded: boolean;
  controls: string;
  onToggle: () => void;
}) {
  useUiLanguage();
  if (!counts && !error) return null;
  const label = counts
    ? (['pre', 'post', 'gpt'] as const)
      .filter(kind => counts[kind] > 0)
      .map(kind => t(`projects:cacheDictionary.${kind}Count`, { count: counts[kind] }))
      .join(' · ')
    : t('projects:cacheDictionary.failed');
  if (!label) return null;
  return <button
    type="button"
    className="cache-card__pill cache-card__pill--dictionary"
    title={error || t('projects:cacheDictionary.details')}
    aria-expanded={expanded}
    aria-controls={controls}
    onClick={onToggle}
  >{label}</button>;
}
