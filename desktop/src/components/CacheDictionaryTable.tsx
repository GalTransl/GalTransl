import { t, useUiLanguage } from '../i18n';
import type { CacheDictionaryMatch } from '../lib/api';

type RowType = 'normal' | 'conditional' | 'situation' | 'gpt';
type TableRow = { match: CacheDictionaryMatch; type: RowType; values: string[] };

function parseMatch(match: CacheDictionaryMatch): TableRow {
  // Follow the dictionary loader's separators, retaining empty replacement cells.
  let rule = match.rule.replace(/ {4}/g, '\t');
  if (match.kind === 'gpt' && rule.includes('->')) rule = rule.replace(/->/g, '\t').replace(/#/g, '\t');
  const parts = rule.split('\t');
  if (match.kind === 'gpt') {
    return { match, type: 'gpt', values: [parts[0] ?? '', parts[1] ?? '', parts.slice(2).join('\t')] };
  }
  if (parts.length >= 4 && ['pre_src', 'post_src', 'pre_dst', 'post_dst', 'pre_jp', 'post_jp', 'pre_zh', 'post_zh'].includes(parts[0])) {
    return { match, type: 'conditional', values: [...parts.slice(0, 4), parts.slice(4).join('\t')] };
  }
  if (parts.length >= 3 && ['mono', 'diag'].includes(parts[0])) {
    return { match, type: 'situation', values: [parts[0], parts[1], parts.slice(2).join('\t')] };
  }
  return { match, type: 'normal', values: [parts[0] ?? '', parts[1] ?? '', parts.slice(2).join('\t')] };
}

function columns(type: RowType) {
  const label = (key: 'source' | 'translationText' | 'text' | 'replace' | 'target' | 'entry' | 'textVariant2' | 'textVariant3') => t(`projects:dictionaryManager.getFieldLabels_message_${key}`);
  if (type === 'gpt') return [label('source'), label('translationText'), label('text')];
  const replacement = label('replace');
  if (type === 'conditional') return [label('target'), label('entry'), t('common:actions.search'), replacement, label('textVariant2')];
  if (type === 'situation') return [label('textVariant3'), t('common:actions.search'), replacement];
  return [t('common:actions.search'), replacement, label('textVariant2')];
}

export function CacheDictionaryTable({ matches }: { matches: CacheDictionaryMatch[] }) {
  useUiLanguage();
  const groups = new Map<string, { file: string; type: RowType; rows: TableRow[] }>();
  for (const match of matches) {
    const row = parseMatch(match);
    const key = JSON.stringify([match.file, row.type]);
    let group = groups.get(key);
    if (!group) { group = { file: match.file, type: row.type, rows: [] }; groups.set(key, group); }
    group.rows.push(row);
  }
  return <div className="cache-dictionary-mini-list">
    {[...groups.entries()].map(([key, group]) => {
      const labels = columns(group.type);
      const typeLabel = group.type === 'gpt' ? 'GPT' : t(`projects:dictionaryManager.getTypeLabel_message_${({ normal: 'textVariant3', conditional: 'entry', situation: 'textVariant4' } as const)[group.type]}`);
      return <div className={`cache-dictionary-mini cache-dictionary-mini--${group.type}`} key={key}>
        <div className="cache-dictionary-mini__header">
          <span className="cache-dictionary-mini__type">{typeLabel}</span>
          <span className="cache-dictionary-details__source" title={group.file}>{group.file.split(/[\\/]/).pop()}</span>
        </div>
        <div className="cache-dictionary-mini__scroll">
          <table className="cache-dictionary-mini__table" aria-label={`${typeLabel} · ${group.file.split(/[\\/]/).pop()}`}>
            <colgroup>
              <col className="cache-dictionary-mini__line-column" />
              {labels.map((_, index) => <col key={index} />)}
            </colgroup>
            <thead><tr>
              <th scope="col">{t('projects:cacheDictionary.line')}</th>
              {labels.map((label, index) => <th scope="col" key={index}>{label}</th>)}
            </tr></thead>
            <tbody>{group.rows.map(({ match, values }, rowIndex) => <tr key={`${match.line}:${rowIndex}`} title={match.rule}>
              <td className="cache-dictionary-mini__line">{match.line}</td>
              {values.map((value, index) => <td key={index}>
                {value || <span className="cache-dictionary-mini__empty" title={t('projects:cacheDictionary.emptyCell')}>∅</span>}
              </td>)}
            </tr>)}</tbody>
          </table>
        </div>
      </div>;
    })}
  </div>;
}
