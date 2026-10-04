import { t as translate, useUiLanguage } from "../../i18n";
import type { ReactNode } from 'react';
import { Icon, type IconName } from '../../components/Icon';

export type ConfigSectionKey = 'fileIO' | 'common' | 'backendSpecific' | 'textProcessing' | 'dictionary' | 'problemAnalyze' | 'retranslKey' | 'problemFilterKey' | 'projectGuideline';

export interface ConfigSectionDef {
  key: ConfigSectionKey;
  label: string;
  icon: IconName;
}

export const CONFIG_SECTIONS: ConfigSectionDef[] = [
  { key: 'fileIO', get label() { return translate("config:configSectionNav.label_label_file"); }, icon: 'folder-open' },
  { key: 'common', get label() { return translate("config:configSectionNav.label_label_translationSettings"); }, icon: 'settings' },
  { key: 'backendSpecific', get label() { return translate("config:configSectionNav.label_label_translationBackend"); }, icon: 'bot' },
  { key: 'textProcessing', get label() { return translate("config:configSectionNav.label_label_textProcess"); }, icon: 'file-text' },
  { key: 'dictionary', get label() { return translate("config:configSectionNav.label_label_dictionarySettings"); }, icon: 'book' },
  { key: 'problemAnalyze', get label() { return translate("config:configSectionNav.label_label_problem"); }, icon: 'search' },
  { key: 'retranslKey', get label() { return translate("config:configSectionNav.label_label_retranslate"); }, icon: 'repeat' },
  { key: 'problemFilterKey', get label() { return translate("config:configSectionNav.label_label_problemFilter"); }, icon: 'ban' },
  { key: 'projectGuideline', get label() { return translate("config:configSectionNav.label_label_projectGuideline"); }, icon: 'bookmark' },
];

interface ConfigSectionNavProps {
  activeSection: ConfigSectionKey;
  onSectionChange: (section: ConfigSectionKey) => void;
  yamlView: boolean;
  onYamlToggle: () => void;
  onSave: () => void;
  saving: boolean;
  dirty: boolean;
  disabled?: boolean;
  extraActions?: ReactNode;
}

export function ConfigSectionNav({
  activeSection,
  onSectionChange,
  yamlView,
  onYamlToggle,
  onSave,
  saving,
  dirty,
  disabled = false,
}: ConfigSectionNavProps) {
  useUiLanguage();
  return (
    <aside className="project-config-page__sidebar">
      {CONFIG_SECTIONS.map((section) => (
        <button
          type="button"
          key={section.key}
          className={`project-config-page__section-btn ${activeSection === section.key ? 'project-config-page__section-btn--active' : ''}`}
          onClick={() => { onSectionChange(section.key); }}
        >
          <span><Icon name={section.icon} /></span>
          <span>{section.label}</span>
        </button>
      ))}
      <button
        type="button"
        className="project-config-page__save-btn"
        onClick={onSave}
        disabled={saving || disabled}
      >
        <span><Icon name="save" /></span>
        <span>
          {saving ? translate("common:actions.saving") : translate("config:configSectionNav.projectConfigPageSaveBtn_message_saveConfig")}
          {/* 未保存提示：一个小圆点（原来是圆点字符，CSS 画的更稳、颜色也走 token） */}
          {dirty && !saving && <span className="project-config-page__dirty-dot" title={translate("config:configSectionNav.projectConfigPageSaveBtn_title_notSaveChange")} />}
        </span>
      </button>
      <div className="project-config-page__section-divider" />
      <button
        type="button"
        className={`project-config-page__section-btn ${yamlView ? 'project-config-page__section-btn--active' : ''}`}
        onClick={onYamlToggle}
      >
        <span><Icon name="note" /></span>
        <span>{translate("config:configSectionNav.projectConfigPageSidebar_message_yAMLSource")}</span>
      </button>
    </aside>
  );
}
