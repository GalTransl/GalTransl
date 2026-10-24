import { t, useUiLanguage } from '../i18n';
import type { CacheSearchCondition, CacheSearchField } from '../lib/api';
import { CustomSelect } from './CustomSelect';

export function CacheSearchConditionRow({ condition, onChange, onRemove }: {
  condition: CacheSearchCondition;
  onChange: (condition: CacheSearchCondition) => void;
  onRemove?: () => void;
}) {
  useUiLanguage();
  return (
    <div className="cache-search-input-group">
      <input
        type="text" className="cache-search cache-search--global"
        placeholder={t('projects:projectCachePage.cacheSearchInputGroup_placeholder_search')}
        aria-label={t('projects:projectCachePage.cacheSearchInputGroup_placeholder_search')}
        value={condition.query} onChange={(e) => onChange({ ...condition, query: e.target.value })}
      />
      <CustomSelect
        className="cache-search-field" value={condition.field}
        aria-label={t('projects:projectCachePage.searchConditionField')}
        onChange={(e) => onChange({ ...condition, field: e.target.value as CacheSearchField })}
      >
        <option value="all">{t('projects:projectCachePage.cacheSearchField_message_all')}</option>
        <option value="src">{t('projects:projectCachePage.cacheSearchField_message_source')}</option>
        <option value="dst">{t('projects:projectCachePage.cacheSearchField_message_translationText')}</option>
        <option value="problem">{t('projects:projectCachePage.cacheSearchField_message_problem')}</option>
        <option value="pre_src">{t('projects:projectCachePage.searchFieldPreSrc')}</option>
        <option value="post_src">{t('projects:projectCachePage.searchFieldPostSrc')}</option>
        <option value="pre_dst">{t('projects:projectCachePage.searchFieldPreDst')}</option>
        <option value="proofread_dst">{t('projects:projectCachePage.searchFieldProofreadDst')}</option>
      </CustomSelect>
      <label className="cache-search-regex-toggle" title={t('projects:projectCachePage.cacheSearchRegexToggle_title_regexSearch')}>
        <input type="checkbox" checked={condition.options.re}
          onChange={(e) => onChange({ ...condition, options: { re: e.target.checked } })} />
        <span className="cache-search-regex-toggle__track" aria-hidden="true" />
        <span className="cache-search-regex-toggle__label">{t('projects:projectCachePage.cacheSearchRegexToggle_message_regex')}</span>
      </label>
      {onRemove && <button type="button" className="cache-search-condition-remove" onClick={onRemove}
        aria-label={t('projects:projectCachePage.removeSearchCondition')}>×</button>}
    </div>
  );
}
