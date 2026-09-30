import { Panel } from '../../components/Panel';
import { CustomSelect } from '../../components/CustomSelect';
import { PluginSettingsEditor } from '../../components/PluginSettingsEditor';
import { ConfigFieldRow, ConfigFieldGroup, type ConfigFieldDef } from './ConfigFieldRow';
import type { PluginInfo } from '../../lib/api';

const FILE_FIELD_GROUPS: { title: string; fields: ConfigFieldDef[] }[] = [
  {
    title: '文件分割',
    fields: [
      { key: 'splitFile', label: '文件分割', description: '单文件分片模式：no 关闭，Num 按句数切片，Equal 按份数均分。', type: 'select', options: ['no', 'Num', 'Equal'] },
      { key: 'splitFileNum', label: '分割数量', description: 'Num 模式下表示每片句数；Equal 模式下表示分片总数。', type: 'number', placeholder: '2048' },
      { key: 'splitFileCrossNum', label: '分割交叉句数', description: '分片间重叠句数，可提升片段衔接质量（常用 0 或 10）。', type: 'number', placeholder: '0' },
    ],
  },
  {
    title: '文本格式',
    fields: [
      { key: 'linebreakSymbol', label: '换行符', description: 'JSON 内换行符类型，供问题检测/自动修复使用。', type: 'text', placeholder: 'auto' },
    ],
  },
  {
    title: '缓存与日志',
    fields: [
      { key: 'save_steps', label: '缓存保存频率', description: '每处理 N 个批次保存一次缓存。', type: 'number', placeholder: '1' },
      { key: 'loggingLevel', label: '日志级别', description: 'debug 详细，info 常规，warning 仅警告。', type: 'select', options: ['debug', 'info', 'warning'] },
      { key: 'saveLog', label: '保存日志到文件', description: '是否将运行日志写入文件。', type: 'select', options: ['true', 'false'] },
    ],
  },
];

interface FileIOSettingsSectionProps {
  config: Record<string, unknown> | null;
  filePlugins: PluginInfo[];
  onFilePluginChange: (value: string) => void;
  onPluginSettingChange: (pluginName: string, key: string, value: unknown) => void;
  onFieldChange: (path: string, value: string) => void;
}

export function FileIOSettingsSection({
  config,
  filePlugins,
  onFilePluginChange,
  onPluginSettingChange,
  onFieldChange,
}: FileIOSettingsSectionProps) {
  const commonConfig = (config?.common as Record<string, unknown>) || {};
  const selectedFilePlugin = filePlugins.find(
    (p) => p.name === String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')
  );
  return (
    <Panel title="文件读写" description="配置文件插件、文件分割、文本格式及缓存与日志保存方式。">
      <div className="config-form">
        {/* ── 文件插件 ── */}
        <div className="plugin-section">
          <div className="plugin-section__title">文件插件</div>
          <label className="field">
            <CustomSelect
              value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
              onChange={(e) => onFilePluginChange(e.target.value)}
            >
              <option value="auto">自动识别 (auto)</option>
              {filePlugins.length > 0 ? (
                filePlugins.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.display_name} ({p.name})
                  </option>
                ))
              ) : String((config?.plugin as Record<string, unknown>)?.filePlugin) === 'auto' ? null : (
                <option value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}>
                  {String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
                </option>
              )}
            </CustomSelect>
            {selectedFilePlugin?.description && (
              <span className="field__hint" style={{ whiteSpace: 'pre-line' }}>{selectedFilePlugin.description}</span>
            )}
            <span className="field__hint">
              从全局插件管理中获取可用文件插件；「自动识别」按每个输入文件的类型分别选择插件，gt_input 可混放多种格式
            </span>
          </label>
          {/* 文件插件设置项 */}
          {(() => {
            if (!selectedFilePlugin || Object.keys(selectedFilePlugin.settings || {}).length === 0) return null;
            return (
              <PluginSettingsEditor
                plugin={selectedFilePlugin}
                overrides={((config?.plugin as Record<string, unknown>)?.[selectedFilePlugin.name] as Record<string, unknown>) || {}}
                onChange={onPluginSettingChange}
              />
            );
          })()}
        </div>

        {FILE_FIELD_GROUPS.map((group) => (
          <ConfigFieldGroup key={group.title} title={group.title}>
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
