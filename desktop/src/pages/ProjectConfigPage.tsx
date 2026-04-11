import { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Panel } from '../components/Panel';
import { BackendConfigEditor } from '../components/BackendConfigEditor';
import { ProxyConfigEditor } from '../components/ProxyConfigEditor';
import { PluginSettingsEditor } from '../components/PluginSettingsEditor';
import {
  ApiError,
  type PluginInfo,
  fetchProjectConfig,
  updateProjectConfig,
  fetchBackendProfiles,
  fetchPlugins,
  getSelectedBackendProfile,
  setSelectedBackendProfile,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

type ConfigSection = 'common' | 'backendSpecific' | 'plugin' | 'dictionary' | 'problemAnalyze';

const CONFIG_SECTIONS: { key: ConfigSection; label: string; icon: string }[] = [
  { key: 'common', label: '通用设置', icon: '⚙️' },
  { key: 'backendSpecific', label: '翻译后端', icon: '🤖' },
  { key: 'plugin', label: '插件设置', icon: '🧩' },
  { key: 'dictionary', label: '字典设置', icon: '📖' },
  { key: 'problemAnalyze', label: '问题分析', icon: '🔍' },
];

// Field definitions for common config section
const COMMON_FIELDS: {
  key: string;
  label: string;
  description: string;
  type: 'number' | 'text' | 'select';
  options?: string[];
  placeholder?: string;
}[] = [
  { key: 'workersPerProject', label: '并发文件数', description: '项目级并行文件数；单文件并行需配合“文件分割”。', type: 'number', placeholder: '16' },
  { key: 'gpt.numPerRequestTranslate', label: '单次翻译句数', description: '每次请求打包的句子数，建议不超过 16。', type: 'number', placeholder: '10' },
  { key: 'language', label: '目标语言', description: '翻译输出语言。', type: 'select', options: ['zh-cn', 'zh-tw', 'en', 'ja', 'ko', 'ru', 'fr'] },
  { key: 'sortBy', label: '翻译顺序', description: 'name 按文件名，size 优先大文件（并行时通常更快）。', type: 'select', options: ['name', 'size'] },
  { key: 'splitFile', label: '文件分割', description: '单文件分片模式：no 关闭，Num 按句数切片，Equal 按份数均分。', type: 'select', options: ['no', 'Num', 'Equal'] },
  { key: 'splitFileNum', label: '分割数量', description: 'Num 模式下表示每片句数；Equal 模式下表示分片总数。', type: 'number', placeholder: '2048' },
  { key: 'splitFileCrossNum', label: '分割交叉句数', description: '分片间重叠句数，可提升片段衔接质量（常用 0 或 10）。', type: 'number', placeholder: '0' },
  { key: 'save_steps', label: '缓存保存频率', description: '每处理 N 个批次保存一次缓存。', type: 'number', placeholder: '1' },
  { key: 'start_time', label: '定时启动', description: '24 小时制时间（如 00:30）；留空则立即启动。', type: 'text', placeholder: '留空则立即启动' },
  { key: 'linebreakSymbol', label: '换行符', description: 'JSON 内换行符类型，供问题检测/自动修复使用。', type: 'text', placeholder: 'auto' },
  { key: 'skipH', label: '跳过敏感句', description: '是否跳过可能触发敏感词检测的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'smartRetry', label: '智能重试', description: '解析失败时自动缩小批次并重置上下文，减少无效重试。', type: 'select', options: ['true', 'false'] },
  { key: 'retranslFail', label: '重翻失败句', description: '启动时是否自动重翻标记为 (Failed) 的句子。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.contextNum', label: '上下文句数', description: '每次请求附带的前文句数，常用 8。', type: 'number', placeholder: '8' },
  { key: 'gpt.translation_guideline', label: '翻译规范', description: '使用的翻译规范文件名（位于 translation_guidelines）。', type: 'text', placeholder: '日译中_基础.md' },
  { key: 'gpt.enhance_jailbreak', label: '改善拒答', description: '启用后可降低模型拒答概率。', type: 'select', options: ['true', 'false'] },
  { key: 'gpt.change_prompt', label: '修改Prompt', description: 'no 不改；AdditionalPrompt 追加；OverwritePrompt 覆盖默认提示词。', type: 'select', options: ['no', 'AdditionalPrompt', 'OverwritePrompt'] },
  { key: 'gpt.prompt_content', label: '额外Prompt内容', description: '仅在“修改Prompt”非 no 时生效。', type: 'text' },
  { key: 'gpt.token_limit', label: 'Token限制(Sakura)', description: 'Sakura 场景下单轮 token 上限；0 表示不限制。', type: 'number', placeholder: '0' },
  { key: 'loggingLevel', label: '日志级别', description: 'debug 详细，info 常规，warning 仅警告。', type: 'select', options: ['debug', 'info', 'warning'] },
  { key: 'saveLog', label: '保存日志到文件', description: '是否将运行日志写入文件。', type: 'select', options: ['true', 'false'] },
];

export function ProjectConfigPage() {
  const { projectDir, projectId, configFileName } = useOutletContext<OutletContext>();

  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [activeSection, setActiveSection] = useState<ConfigSection>('common');
  const [yamlView, setYamlView] = useState(false);

  // Global backend profile selection
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<string>('');

  // Plugin lists from global plugin manager
  const [filePlugins, setFilePlugins] = useState<PluginInfo[]>([]);
  const [textPlugins, setTextPlugins] = useState<PluginInfo[]>([]);

  // Load config
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchProjectConfig(projectId, configFileName)
      .then((data) => {
        if (!cancelled) setConfig(data.config);
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载配置失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId, configFileName]);

  // Load backend profile names and current selection
  useEffect(() => {
    let cancelled = false;
    fetchBackendProfiles()
      .then((data) => {
        if (!cancelled) setBackendProfileNames(Object.keys(data.profiles || {}));
      })
      .catch(() => {
        // silently ignore — profiles are optional
      });
    if (projectDir) {
      setSelectedProfile(getSelectedBackendProfile(projectDir));
    }
    return () => { cancelled = true; };
  }, [projectDir]);

  // Load plugin lists from global plugin manager
  useEffect(() => {
    let cancelled = false;
    fetchPlugins()
      .then((plugins) => {
        if (!cancelled) {
          setFilePlugins(plugins.filter((p) => p.type === 'file'));
          setTextPlugins(plugins.filter((p) => p.type === 'text' && p.name !== 'text_example_nouse'));
        }
      })
      .catch(() => {
        // silently ignore — plugins are optional
      });
    return () => { cancelled = true; };
  }, []);

  // Get/set nested config value
  const getNestedValue = useCallback((obj: Record<string, unknown>, path: string): unknown => {
    const keys = path.split('.');
    let current: unknown = obj;
    for (const key of keys) {
      if (current == null || typeof current !== 'object') return undefined;
      current = (current as Record<string, unknown>)[key];
    }
    return current;
  }, []);

  const setNestedValue = useCallback((obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> => {
    const keys = path.split('.');
    const result = JSON.parse(JSON.stringify(obj));
    let current: Record<string, unknown> = result;
    for (let i = 0; i < keys.length - 1; i++) {
      if (current[keys[i]] == null || typeof current[keys[i]] !== 'object') {
        current[keys[i]] = {};
      }
      current = current[keys[i]] as Record<string, unknown>;
    }
    current[keys[keys.length - 1]] = value;
    return result;
  }, []);

  const handleFieldChange = useCallback((path: string, value: string) => {
    setConfig((prev) => {
      if (!prev) return prev;
      // Try to parse numbers
      let parsedValue: unknown = value;
      if (value !== '' && !Number.isNaN(Number(value))) {
        parsedValue = Number(value);
      } else if (value === 'true') {
        parsedValue = true;
      } else if (value === 'false') {
        parsedValue = false;
      }
      return setNestedValue(prev, path, parsedValue);
    });
    setSaveSuccess(false);
  }, [setNestedValue]);

  // Unified plugin setting change handler
  const handlePluginSettingChange = useCallback((pluginName: string, key: string, value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev;
      const plugin = { ...((prev.plugin as Record<string, unknown>) || {}) };
      const currentOverrides = { ...((plugin[pluginName] as Record<string, unknown>) || {}) };
      currentOverrides[key] = value;
      plugin[pluginName] = currentOverrides;
      return { ...prev, plugin };
    });
    setSaveSuccess(false);
  }, []);

  // Toggle a text plugin on/off
  const handleToggleTextPlugin = useCallback((pluginName: string) => {
    setConfig((prev) => {
      if (!prev) return prev;
      const plugin = { ...((prev.plugin as Record<string, unknown>) || {}) };
      const currentList: string[] = Array.isArray(plugin.textPlugins)
        ? [...(plugin.textPlugins as string[])]
        : [];
      const idx = currentList.indexOf(pluginName);
      if (idx >= 0) {
        currentList.splice(idx, 1);
      } else {
        currentList.push(pluginName);
      }
      plugin.textPlugins = currentList;
      return { ...prev, plugin };
    });
    setSaveSuccess(false);
  }, []);

  const handleSave = useCallback(async () => {
    if (!projectId || !config) return;
    setSaving(true);
    setError(null);
    setSaveSuccess(false);
    try {
      await updateProjectConfig(projectId, {
        config,
        config_file_name: configFileName,
      });
      setSaveSuccess(true);
    } catch (err) {
      setError(getErrorMessage(err, '保存配置失败'));
    } finally {
      setSaving(false);
    }
  }, [projectId, config, configFileName]);

  if (loading) {
    return (
      <div className="project-config-page">
        <div className="project-config-page__header">
          <h1>配置编辑</h1>
        </div>
        <div className="empty-state">
          <strong>加载中…</strong>
        </div>
      </div>
    );
  }

  if (error && !config) {
    return (
      <div className="project-config-page">
        <div className="project-config-page__header">
          <h1>配置编辑</h1>
        </div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  const commonConfig = (config?.common || {}) as Record<string, unknown>;

  return (
    <div className="project-config-page">
      <div className="project-config-page__header">
        <h1>配置编辑</h1>
        <p>可视化编辑项目配置文件 {configFileName}</p>
      </div>

      <div className="project-config-page__content">
        <aside className="project-config-page__sidebar">
          {CONFIG_SECTIONS.map((section) => (
            <button
              type="button"
              key={section.key}
              className={`project-config-page__section-btn ${activeSection === section.key ? 'project-config-page__section-btn--active' : ''}`}
              onClick={() => setActiveSection(section.key)}
            >
              <span>{section.icon}</span>
              <span>{section.label}</span>
            </button>
          ))}
          <button
            type="button"
            className="project-config-page__save-btn"
            onClick={() => void handleSave()}
            disabled={saving || !config}
          >
            <span>💾</span>
            <span>{saving ? '保存中…' : '保存配置'}</span>
          </button>
          <div className="project-config-page__section-divider" />
          <button
            type="button"
            className={`project-config-page__section-btn ${yamlView ? 'project-config-page__section-btn--active' : ''}`}
            onClick={() => setYamlView(!yamlView)}
          >
            <span>📝</span>
            <span>YAML源码</span>
          </button>
        </aside>

        <div className="project-config-page__main">
          {error && (
            <div className="inline-alert inline-alert--error" role="alert">{error}</div>
          )}
          {saveSuccess && (
            <div className="inline-alert inline-alert--success" role="status">配置已保存</div>
          )}

          <div key={yamlView ? 'yaml' : activeSection} className="section-fade-in">
          {yamlView ? (
            <Panel title="YAML源码" description="直接编辑YAML配置源码（只读预览，修改请使用上方表单）">
              <pre className="yaml-preview">
                {config ? JSON.stringify(config, null, 2) : '无配置数据'}
              </pre>
            </Panel>
          ) : (
            <>
              {activeSection === 'common' && (
                <Panel title="通用设置" description="翻译核心参数配置（说明已与 sampleProject/config.inc.yaml 同步）。">
                  <div className="config-form">
                    {COMMON_FIELDS.map((field) => {
                      const fieldId = `common-${field.key.replace(/\./g, '-')}`;
                      const value = getNestedValue(commonConfig, field.key);
                      const displayValue = value == null ? '' : String(value);
                      return (
                        <div key={field.key} className="field">
                          <label htmlFor={fieldId}>{field.label}</label>
                          {field.type === 'select' ? (
                            <select
                              id={fieldId}
                              value={displayValue}
                              onChange={(e) => handleFieldChange(`common.${field.key}`, e.target.value)}
                            >
                              {field.options?.map((opt) => (
                                <option key={opt} value={opt}>{opt}</option>
                              ))}
                            </select>
                          ) : (
                            <input
                              id={fieldId}
                              type={field.type}
                              value={displayValue}
                              placeholder={field.placeholder}
                              onChange={(e) => handleFieldChange(`common.${field.key}`, e.target.value)}
                            />
                          )}
                          <span className="field__hint">{field.description}</span>
                        </div>
                      );
                    })}
                  </div>
                </Panel>
              )}

              {activeSection === 'backendSpecific' && (
                <Panel title="翻译后端" description="OpenAI兼容接口、Sakura本地模型和代理配置。">
                  <div className="config-form">
                    <label className="field">
                      <span>全局后端配置</span>
                      <select
                        value={selectedProfile}
                        onChange={(e) => {
                          const val = e.target.value;
                          setSelectedProfile(val);
                          setSelectedBackendProfile(projectDir, val);
                        }}
                      >
                        <option value="">不使用（使用项目自身配置）</option>
                        {backendProfileNames.map((name) => (
                          <option key={name} value={name}>{name}</option>
                        ))}
                      </select>
                      <span className="field__hint">
                        {selectedProfile
                          ? `翻译时将使用全局配置「${selectedProfile}」覆盖项目后端设置`
                          : '新项目默认使用全局默认配置，可在「翻译后端配置」页面设置默认'}
                      </span>
                    </label>

                    {selectedProfile ? (
                      <div className="inline-alert inline-alert--info" role="status">
                        已选择全局配置「{selectedProfile}」，翻译时将使用该配置覆盖项目后端设置。如需修改配置内容，请前往「翻译后端配置」页面。
                      </div>
                    ) : (
                      <BackendConfigEditor
                        config={config?.backendSpecific as Record<string, unknown> || {}}
                        onChange={(newBackend) => {
                          setConfig((prev) => prev ? { ...prev, backendSpecific: newBackend } : prev);
                          setSaveSuccess(false);
                        }}
                      />
                    )}

                    <ProxyConfigEditor
                      proxyConfig={(config?.proxy as Record<string, unknown>) || {}}
                      onChange={(newProxy) => {
                        setConfig((prev) => prev ? { ...prev, proxy: newProxy } : prev);
                        setSaveSuccess(false);
                      }}
                    />
                  </div>
                </Panel>
              )}

              {activeSection === 'plugin' && (
                <Panel title="插件设置" description="文件插件和文本插件配置。">
                  <div className="config-form">
                    {/* ── 文件插件 ── */}
                    <div className="plugin-section">
                      <div className="plugin-section__title">文件插件</div>
                      <label className="field">
                        <select
                          value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
                          onChange={(e) => {
                            setConfig((prev) => {
                              const plugin = { ...((prev?.plugin as Record<string, unknown>) || {}) };
                              plugin.filePlugin = e.target.value;
                              return prev ? { ...prev, plugin } : prev;
                            });
                            setSaveSuccess(false);
                          }}
                        >
                          {filePlugins.length > 0 ? (
                            filePlugins.map((p) => (
                              <option key={p.name} value={p.name}>
                                {p.display_name} ({p.name})
                              </option>
                            ))
                          ) : (
                            <option value={String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}>
                              {String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')}
                            </option>
                          )}
                        </select>
                        <span className="field__hint">从全局插件管理中获取可用文件插件</span>
                      </label>
                      {/* 文件插件设置项 */}
                      {(() => {
                        const selectedFilePlugin = filePlugins.find(
                          (p) => p.name === String((config?.plugin as Record<string, unknown>)?.filePlugin ?? 'file_galtransl_json')
                        );
                        if (!selectedFilePlugin || Object.keys(selectedFilePlugin.settings || {}).length === 0) return null;
                        return (
                          <PluginSettingsEditor
                            plugin={selectedFilePlugin}
                            overrides={((config?.plugin as Record<string, unknown>)?.[selectedFilePlugin.name] as Record<string, unknown>) || {}}
                            onChange={handlePluginSettingChange}
                          />
                        );
                      })()}
                    </div>

                    {/* ── 文本插件 ── */}
                    <div className="plugin-section">
                      <div className="plugin-section__title">文本插件</div>
                      {textPlugins.length > 0 ? (
                        <div className="plugin-check-list">
                          {textPlugins.map((plugin) => {
                            const enabledTextPlugins = new Set(
                              Array.isArray((config?.plugin as Record<string, unknown>)?.textPlugins)
                                ? ((config?.plugin as Record<string, unknown>).textPlugins as string[])
                                : []
                            );
                            const isChecked = enabledTextPlugins.has(plugin.name);
                            const hasSettings = Object.keys(plugin.settings || {}).length > 0;

                            return (
                              <div key={plugin.name} className="plugin-check-item">
                                <label className="plugin-check-item__header">
                                  <input
                                    type="checkbox"
                                    checked={isChecked}
                                    onChange={() => handleToggleTextPlugin(plugin.name)}
                                  />
                                  <span className="plugin-check-item__name">
                                    {plugin.display_name}
                                  </span>
                                  <span className="plugin-check-item__module">
                                    ({plugin.name})
                                  </span>
                                  {plugin.version && (
                                    <span className="plugin-check-item__version">
                                      v{plugin.version}
                                    </span>
                                  )}
                                </label>
                                {plugin.description && (
                                  <div className="plugin-check-item__desc">{plugin.description}</div>
                                )}
                                {isChecked && hasSettings && (
                                  <div className="plugin-check-item__settings">
                                    <PluginSettingsEditor
                                      plugin={plugin}
                                      overrides={((config?.plugin as Record<string, unknown>)?.[plugin.name] as Record<string, unknown>) || {}}
                                      onChange={handlePluginSettingChange}
                                    />
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="plugin-check-empty">未找到可用的文本插件</div>
                      )}
                    </div>
                  </div>
                </Panel>
              )}

              {activeSection === 'dictionary' && (
                <Panel title="字典设置" description="译前/GPT/译后字典文件配置。">
                  <DictConfigEditor
                    dictConfig={(config?.dictionary as Record<string, unknown>) || {}}
                    onChange={(newDict) => {
                      setConfig((prev) => prev ? { ...prev, dictionary: newDict } : prev);
                      setSaveSuccess(false);
                    }}
                  />
                </Panel>
              )}

              {activeSection === 'problemAnalyze' && (
                <Panel title="问题分析" description="翻译质量检测配置。">
                  <div className="config-form">
                    <label className="field">
                      <span>问题检测列表</span>
                      <textarea
                        rows={8}
                        value={Array.isArray((config?.problemAnalyze as Record<string, unknown>)?.problemList)
                          ? ((config?.problemAnalyze as Record<string, unknown>).problemList as string[]).join('\n')
                          : String((config?.problemAnalyze as Record<string, unknown>)?.problemList ?? '')}
                        onChange={(e) => {
                          const lines = e.target.value.split('\n').filter(Boolean);
                          setConfig((prev) => {
                            const pa = { ...((prev?.problemAnalyze as Record<string, unknown>) || {}) };
                            pa.problemList = lines;
                            return prev ? { ...prev, problemAnalyze: pa } : prev;
                          });
                          setSaveSuccess(false);
                        }}
                      />
                      <span className="field__hint">每行一个问题类型，如：词频过高、残留日文等</span>
                    </label>
                  </div>
                </Panel>
              )}
            </>
          )}
          </div>

        </div>
      </div>
    </div>
  );
}

// ---- Dictionary Config Sub-editor ----

function DictConfigEditor({
  dictConfig,
  onChange,
}: {
  dictConfig: Record<string, unknown>;
  onChange: (newConfig: Record<string, unknown>) => void;
}) {
  return (
    <>
      <label className="field">
        <span>通用字典文件夹</span>
        <input
          type="text"
          value={String(dictConfig.defaultDictFolder ?? 'Dict')}
          onChange={(e) => onChange({ ...dictConfig, defaultDictFolder: e.target.value })}
        />
      </label>
      <label className="field">
        <span>译前字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.preDict) ? (dictConfig.preDict as string[]).join('\n') : String(dictConfig.preDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, preDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>GPT字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig['gpt.dict']) ? (dictConfig['gpt.dict'] as string[]).join('\n') : String(dictConfig['gpt.dict'] ?? '')}
          onChange={(e) => onChange({ ...dictConfig, 'gpt.dict': e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>译后字典</span>
        <textarea
          rows={4}
          value={Array.isArray(dictConfig.postDict) ? (dictConfig.postDict as string[]).join('\n') : String(dictConfig.postDict ?? '')}
          onChange={(e) => onChange({ ...dictConfig, postDict: e.target.value.split('\n').filter(Boolean) })}
        />
        <span className="field__hint">每行一个字典文件名</span>
      </label>
      <label className="field">
        <span>字典用在name字段(译前)</span>
        <select
          value={String(dictConfig.usePreDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePreDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典用在name字段(GPT)</span>
        <select
          value={String(dictConfig.useGPTDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, useGPTDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典用在name字段(译后)</span>
        <select
          value={String(dictConfig.usePostDictInName ?? 'false')}
          onChange={(e) => onChange({ ...dictConfig, usePostDictInName: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
      <label className="field">
        <span>字典排序</span>
        <select
          value={String(dictConfig.sortDict ?? 'true')}
          onChange={(e) => onChange({ ...dictConfig, sortDict: e.target.value === 'true' })}
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </label>
    </>
  );
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
