import type { ReactNode } from 'react';
import { Icon, type IconName } from '../../components/Icon';

export type ConfigSectionKey = 'common' | 'backendSpecific' | 'plugin' | 'dictionary' | 'problemAnalyze' | 'retranslKey' | 'problemFilterKey';

export interface ConfigSectionDef {
  key: ConfigSectionKey;
  label: string;
  icon: IconName;
}

export const CONFIG_SECTIONS: ConfigSectionDef[] = [
  { key: 'common', label: '通用设置', icon: 'settings' },
  { key: 'backendSpecific', label: '翻译后端', icon: 'bot' },
  { key: 'plugin', label: '插件设置', icon: 'plug' },
  { key: 'dictionary', label: '字典设置', icon: 'book' },
  { key: 'problemAnalyze', label: '问题分析', icon: 'search' },
  { key: 'retranslKey', label: '重翻关键字', icon: 'repeat' },
  { key: 'problemFilterKey', label: '问题过滤', icon: 'ban' },
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
          {saving ? '保存中…' : '保存配置'}
          {/* 未保存提示：一个小圆点（原来是圆点字符，CSS 画的更稳、颜色也走 token） */}
          {dirty && !saving && <span className="project-config-page__dirty-dot" title="有未保存的修改" />}
        </span>
      </button>
      <div className="project-config-page__section-divider" />
      <button
        type="button"
        className={`project-config-page__section-btn ${yamlView ? 'project-config-page__section-btn--active' : ''}`}
        onClick={onYamlToggle}
      >
        <span><Icon name="note" /></span>
        <span>YAML源码</span>
      </button>
    </aside>
  );
}
