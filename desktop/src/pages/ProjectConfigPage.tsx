import { useCallback, useEffect, useRef, useState } from 'react';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Panel } from '../components/Panel';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, InlineFeedback, LoadingState } from '../components/page-state';
import {
  type PluginInfo,
  fetchProjectConfig,
  updateProjectConfig,
  fetchBackendProfiles,
  fetchPlugins,
  getDefaultBackendProfile,
  getSelectedBackendProfileDisplay,
  setSelectedBackendProfile,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT } from '../lib/api';
import { normalizeError } from '../lib/errors';
import {
  ConfigSectionNav,
  CommonSettingsSection,
  BackendSettingsSection,
  PluginSettingsSection,
  DictionarySettingsSection,
  ProblemAnalyzeSection,
  type ConfigSectionKey,
} from './project-config';

export function ProjectConfigPage({ ctx }: { ctx: ProjectPageContext }) {
  const { projectDir, projectId, configFileName } = ctx;

  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [activeSection, setActiveSection] = useState<ConfigSectionKey>('common');
  const [yamlView, setYamlView] = useState(false);

  // Global backend profile selection
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<string>('');
  const [defaultProfileName, setDefaultProfileName] = useState(() => getDefaultBackendProfile());

  // Plugin lists from global plugin manager
  const [filePlugins, setFilePlugins] = useState<PluginInfo[]>([]);
  const [textPlugins, setTextPlugins] = useState<PluginInfo[]>([]);

  // Ref for scroll-to-section
  const mainRef = useRef<HTMLDivElement>(null);

  // Load config
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchProjectConfig(projectId, configFileName)
      .then((data) => {
        if (!cancelled) {
          setConfig(data.config);
          setDirty(false);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(normalizeError(err, '加载配置失败'));
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
      setSelectedProfile(getSelectedBackendProfileDisplay(projectDir));
    }
    return () => { cancelled = true; };
  }, [projectDir]);

  // React to global default backend profile changes
  useEffect(() => {
    const handler = () => {
      setDefaultProfileName(getDefaultBackendProfile());
      if (projectDir) {
        setSelectedProfile(getSelectedBackendProfileDisplay(projectDir));
      }
    };
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, handler);
    return () => window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, handler);
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

  // Get/set nested config value. Prefer literal flat keys containing dots
  // (e.g. YAML under `common:` uses keys like `gpt.translation_guideline`).
  const getNestedValue = useCallback((obj: Record<string, unknown>, path: string): unknown => {
    const keys = path.split('.');
    let current: unknown = obj;
    for (let i = 0; i < keys.length; i++) {
      if (current == null || typeof current !== 'object') return undefined;
      const remaining = keys.slice(i).join('.');
      const cur = current as Record<string, unknown>;
      if (Object.prototype.hasOwnProperty.call(cur, remaining)) {
        return cur[remaining];
      }
      current = cur[keys[i]];
    }
    return current;
  }, []);

  const setNestedValue = useCallback((obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> => {
    const keys = path.split('.');
    const result = JSON.parse(JSON.stringify(obj));
    let current: Record<string, unknown> = result;
    for (let i = 0; i < keys.length - 1; i++) {
      const remaining = keys.slice(i).join('.');
      if (Object.prototype.hasOwnProperty.call(current, remaining)) {
        current[remaining] = value;
        return result;
      }
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
    setDirty(true);
  }, [setNestedValue]);

  const handleListFieldChange = useCallback((path: string, value: string[]) => {
    setConfig((prev) => {
      if (!prev) return prev;
      return setNestedValue(prev, path, value);
    });
    setSaveSuccess(false);
    setDirty(true);
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
    setDirty(true);
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
    setDirty(true);
  }, []);

  const handleSave = useCallback(async () => {
    if (!projectId || !config) return;
    setSaving(true);
    setError(null);
    setSaveSuccess(false);
    try {
      await updateProjectConfig(projectId, {
        config,
        config_file_name: configFileName });
      setSaveSuccess(true);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '保存配置失败'));
    } finally {
      setSaving(false);
    }
  }, [projectId, config, configFileName]);

  // Scroll to active section
  const handleSectionChange = useCallback((section: ConfigSectionKey) => {
    setActiveSection(section);
    setYamlView(false);
    // Scroll main area to top so new section is visible
    if (mainRef.current) {
      mainRef.current.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, []);

  if (loading) {
    return (
      <div className="project-config-page">
        <PageHeader className="project-config-page__header" title="配置编辑" />
        <LoadingState title="加载配置中…" description={`正在读取 ${configFileName}。`} />
      </div>
    );
  }

  if (error && !config) {
    return (
      <div className="project-config-page">
        <PageHeader className="project-config-page__header" title="配置编辑" />
        <ErrorState title="加载配置失败" description={error} />
      </div>
    );
  }

  const commonConfig = (config?.common || {}) as Record<string, unknown>;

  return (
    <div className="project-config-page">
      <PageHeader className="project-config-page__header" title="配置编辑" description={`可视化编辑项目配置文件 ${configFileName}`} />

      <div className="project-config-page__content">
        <ConfigSectionNav
          activeSection={activeSection}
          onSectionChange={handleSectionChange}
          yamlView={yamlView}
          onYamlToggle={() => setYamlView(!yamlView)}
          onSave={() => void handleSave()}
          saving={saving}
          dirty={dirty}
          disabled={!config}
        />

        <div className="project-config-page__main" ref={mainRef}>
          {error && (
            <InlineFeedback tone="error" title="配置保存失败" description={error} />
          )}
          {saveSuccess && (
            <InlineFeedback tone="success" title="配置已保存" description="当前项目配置已成功写入磁盘。" onDismiss={() => setSaveSuccess(false)} />
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
                <CommonSettingsSection
                  commonConfig={commonConfig}
                  onFieldChange={handleFieldChange}
                  onListFieldChange={handleListFieldChange}
                />
              )}

              {activeSection === 'backendSpecific' && (
                <BackendSettingsSection
                  config={config}
                  selectedProfile={selectedProfile}
                  defaultProfileName={defaultProfileName}
                  backendProfileNames={backendProfileNames}
                  onProfileChange={(profile) => {
                    setSelectedProfile(profile);
                    setSelectedBackendProfile(projectDir, profile);
                  }}
                  onBackendChange={(newBackend) => {
                    setConfig((prev) => prev ? { ...prev, backendSpecific: newBackend } : prev);
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                  onCommonChange={(newCommon) => {
                    setConfig((prev) => prev ? { ...prev, common: newCommon } : prev);
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                  onProxyChange={(newProxy) => {
                    setConfig((prev) => prev ? { ...prev, proxy: newProxy } : prev);
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                  onDirty={() => { setSaveSuccess(false); setDirty(true); }}
                />
              )}

              {activeSection === 'plugin' && (
                <PluginSettingsSection
                  config={config}
                  filePlugins={filePlugins}
                  textPlugins={textPlugins}
                  onFilePluginChange={(value) => {
                    setConfig((prev) => {
                      const plugin = { ...((prev?.plugin as Record<string, unknown>) || {}) };
                      plugin.filePlugin = value;
                      return prev ? { ...prev, plugin } : prev;
                    });
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                  onPluginSettingChange={handlePluginSettingChange}
                  onToggleTextPlugin={handleToggleTextPlugin}
                />
              )}

              {activeSection === 'dictionary' && (
                <DictionarySettingsSection
                  dictConfig={(config?.dictionary as Record<string, unknown>) || {}}
                  onChange={(newDict) => {
                    setConfig((prev) => prev ? { ...prev, dictionary: newDict } : prev);
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                />
              )}

              {activeSection === 'problemAnalyze' && (
                <ProblemAnalyzeSection
                  config={config}
                  onProblemListChange={(lines) => {
                    setConfig((prev) => {
                      const pa = { ...((prev?.problemAnalyze as Record<string, unknown>) || {}) };
                      pa.problemList = lines;
                      return prev ? { ...prev, problemAnalyze: pa } : prev;
                    });
                  }}
                  onDirty={() => { setSaveSuccess(false); setDirty(true); }}
                />
              )}
            </>
          )}
          </div>

        </div>
      </div>
    </div>
  );
}
