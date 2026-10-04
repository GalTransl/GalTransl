import { localizePlugin } from "../../i18n/plugins";
import { t as translate, useUiLanguage } from "../../i18n";
import { Panel } from '../../components/Panel';
import type { ReactNode } from 'react';
import { CustomSelect } from '../../components/CustomSelect';
import { PluginSettingsEditor } from '../../components/PluginSettingsEditor';
import { ConfigFieldRow, ConfigFieldGroup, type ConfigFieldDef } from './ConfigFieldRow';
import type { PluginInfo } from '../../lib/api';

const FILE_FIELD_GROUPS: { title: string; fields: ConfigFieldDef[] }[] = [
  {
    get title() { return translate("config:fileIOSettingsSection.title_title_fileSplit"); },
    fields: [
      { key: 'splitFile', labelKey: "config:fields.splitFile.label", descriptionKey: "config:fields.splitFile.description", type: 'select', options: [{ value: 'no', labelKey: "config:fileIOSettingsSection.option_no" }, { value: 'Num', labelKey: "config:fileIOSettingsSection.option_num" }, { value: 'Equal', labelKey: "config:fileIOSettingsSection.option_equal" }] },
      { key: 'splitFileNum', labelKey: "config:fields.splitFileNum.label", descriptionKey: "config:fields.splitFileNum.description", type: 'number', placeholder: '2048' },
      { key: 'splitFileCrossNum', labelKey: "config:fields.splitFileCrossNum.label", descriptionKey: "config:fields.splitFileCrossNum.description", type: 'number', placeholder: '0' },
    ],
  },
  {
    get title() { return translate("config:fileIOSettingsSection.title_title_textFormat"); },
    fields: [
      { key: 'linebreakSymbol', labelKey: "config:fields.linebreakSymbol.label", descriptionKey: "config:fields.linebreakSymbol.description", type: 'text', placeholder: 'auto' },
    ],
  },
  {
    get title() { return translate("config:fileIOSettingsSection.title_title_cache"); },
    fields: [
      { key: 'save_steps', labelKey: "config:fields.save_steps.label", descriptionKey: "config:fields.save_steps.description", type: 'number', placeholder: '1' },
      { key: 'loggingLevel', labelKey: "config:fields.loggingLevel.label", descriptionKey: "config:fields.loggingLevel.description", type: 'select', options: [{ value: 'debug', labelKey: "config:fileIOSettingsSection.option_debug" }, { value: 'info', labelKey: "config:fileIOSettingsSection.option_info" }, { value: 'warning', labelKey: "config:fileIOSettingsSection.option_warning" }] },
      { key: 'saveLog', labelKey: "config:fields.saveLog.label", descriptionKey: "config:fields.saveLog.description", type: 'select', options: [{ value: 'true', labelKey: "config:fileIOSettingsSection.option_true" }, { value: 'false', labelKey: "config:fileIOSettingsSection.option_false" }] },
    ],
  },
];

interface FileIOSettingsSectionProps {
  config: Record<string, unknown> | null;
  filePlugins: PluginInfo[];
  onFilePluginChange: (value: string) => void;
  onPluginSettingChange: (pluginName: string, key: string, value: unknown) => void;
  onFieldChange: (path: string, value: string) => void;
  reextractAction?: ReactNode;
}

export function FileIOSettingsSection({
  config,
  filePlugins,
  onFilePluginChange,
  onPluginSettingChange,
  onFieldChange,
  reextractAction,
}: FileIOSettingsSectionProps) {
  useUiLanguage();
  const commonConfig = (config?.common as Record<string, unknown>) || {};
  const selectedFilePlugin = filePlugins.find(
    (p) => p.name === String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')
  );
  return (
    <Panel title={translate("config:fileIOSettingsSection.fileIOSettingsSection_title_file")} description={translate("config:fileIOSettingsSection.fileIOSettingsSection_description_configFilePluginFileSplitTextFormat")}>
      <div className="config-form">
        {/* ── 文件插件 ── */}
        <div className="plugin-section">
          <div className="plugin-section__title">{translate("config:fileIOSettingsSection.pluginSection_message_filePlugin")}</div>
          <label className="field">
            <CustomSelect
              value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
              onChange={(e) => onFilePluginChange(e.target.value)}
            >
              <option value="auto">{translate("config:fileIOSettingsSection.field_message_autoAuto")}</option>
              {filePlugins.length > 0 ? (
                filePlugins.map((p) => (
                  <option key={p.name} value={p.name}>
                    {localizePlugin(p).display_name} ({p.name})
                  </option>
                ))
              ) : String((config?.plugin as Record<string, unknown>)?.filePlugin) === 'auto' ? null : (
                <option value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}>
                  {String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
                </option>
              )}
            </CustomSelect>
            {localizePlugin(selectedFilePlugin)?.description && (
              <span className="field__hint" style={{ whiteSpace: 'pre-line' }}>{localizePlugin(selectedFilePlugin)?.description}</span>
            )}
            <span className="field__hint">{translate("config:fileIOSettingsSection.field_message_pluginFilePluginAutoCountFileSelect")}</span>
          </label>
          {/* 文件插件设置项 */}
          {(() => {
            if (!selectedFilePlugin || Object.keys(selectedFilePlugin.settings || {}).length === 0) return null;
            return (
              <PluginSettingsEditor
                plugin={selectedFilePlugin}
                overrides={((config?.plugin as Record<string, unknown>)?.[selectedFilePlugin.name] as Record<string, unknown>) || {}}
                onChange={onPluginSettingChange}
                afterField={selectedFilePlugin.name.replace('(project_dir)', '') === 'file_msgtool_script'
                  ? { key: 'source_encoding', content: reextractAction } : undefined}
              />
            );
          })()}
        </div>

        {FILE_FIELD_GROUPS.map((group) => (
          <ConfigFieldGroup key={group.fields[0].key} title={group.title}>
            {group.fields.map((field) => (
              <ConfigFieldRow
                key={field.key}
                field={field}
                value={commonConfig[field.key]}
                onChange={onFieldChange}
                pathPrefix="common"
              />
            ))}
          </ConfigFieldGroup>
        ))}
      </div>
    </Panel>
  );
}
