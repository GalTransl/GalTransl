import { t, useUiLanguage } from '../i18n';
import type { CacheSearchCondition, CacheSearchField } from '../lib/api';
import { CustomSelect } from './CustomSelect';

const CloseGlyph = () => (
  <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
    <path d="M4 4l8 8M12 4l-8 8" />
  </svg>
);

export function CacheSearchConditionRow({ condition, onChange, onRemove }: {
  condition: CacheSearchCondition;
  onChange: (condition: CacheSearchCondition) => void;
  onRemove?: () => void;
}) {
  useUiLanguage();
  return (
    <div className="cache-search-box">
      <CustomSelect
        className="cache-search-field cache-search-box__field" value={condition.field}
        aria-label={t('projects:projectCachePage.searchConditionField')}
        onChange={(e) => onChange({ ...condition, field: e.target.value as CacheSearchField })}
      >
        <option value="all">{t('projects:projectCachePage.cacheSearchField_message_all')}</option>
        <option value="src">{t('projects:projectCachePage.cacheSearchField_message_source')}</option>
        <option value="dst">{t('projects:projectCachePage.cacheSearchField_message_translationText')}</option>
        <option value="problem">{t('projects:projectCachePage.cacheSearchField_message_problem')}</option>
        <option value="name">{t('projects:projectCachePage.searchFieldName')}</option>
        <option value="pre_src">{t('projects:projectCachePage.searchFieldPreSrc')}</option>
        <option value="post_src">{t('projects:projectCachePage.searchFieldPostSrc')}</option>
        <option value="pre_dst">{t('projects:projectCachePage.searchFieldPreDst')}</option>
        <option value="proofread_dst">{t('projects:projectCachePage.searchFieldProofreadDst')}</option>
      </CustomSelect>
      <input
        type="text" className="cache-search-box__input"
        placeholder={t('projects:projectCachePage.cacheSearchInputGroup_placeholder_search')}
        aria-label={t('projects:projectCachePage.cacheSearchInputGroup_placeholder_search')}
        value={condition.query} onChange={(e) => onChange({ ...condition, query: e.target.value })}
      />
      {/* 悬停或聚焦时才露出的 ×：附加条件整条删掉；第一条删不掉，用它清空关键词并把范围复位到“全部”。
          放在正则开关左边，正则开关始终贴着右边框，× 出现或消失都不会推动它 */}
      {onRemove ? <button type="button" className="cache-search-box__remove" onClick={onRemove}
        title={t('projects:projectCachePage.removeSearchCondition')}
        aria-label={t('projects:projectCachePage.removeSearchCondition')}><CloseGlyph /></button>
        : (condition.query !== '' || condition.field !== 'all') && <button type="button" className="cache-search-box__remove"
          onClick={() => onChange({ ...condition, query: '', field: 'all' })}
          title={t('projects:projectCachePage.clearSearchCondition')}
          aria-label={t('projects:projectCachePage.clearSearchCondition')}><CloseGlyph /></button>}
      <label className="cache-search-regex-toggle" title={t('projects:projectCachePage.cacheSearchRegexToggle_title_regexSearch')}>
        <input type="checkbox" checked={condition.options.re}
          aria-label={t('projects:projectCachePage.cacheSearchRegexToggle_title_regexSearch')}
          onChange={(e) => onChange({ ...condition, options: { re: e.target.checked } })} />
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
          <path d="M10.5 2.5v7M7.5 4.25l6 3.5M7.5 7.75l6-3.5" />
          <rect x="2.5" y="10.5" width="3" height="3" rx="0.8" fill="currentColor" stroke="none" />
        </svg>
      </label>
    </div>
  );
}
