import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useRef, useState } from 'react';
import { usePageActive, useRetainPage } from '../components/PageActivity';
import { useSearchParams } from 'react-router-dom';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Panel } from '../components/Panel';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, InlineFeedback, LoadingState } from '../components/page-state';
import {
  type PluginInfo,
  fetchProjectConfig,
  previewProjectConfigYaml,
  updateProjectConfig,
  fetchPlugins,
  getBackendProfileNames,
  getDefaultBackendProfile,
  getSelectedBackendProfileDisplay,
  setSelectedBackendProfile,
  setProjectConfigDirty,
  BACKEND_PROFILES_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT } from '../lib/api';
import { normalizeError } from '../lib/errors';
import { prefersReducedMotion } from '../lib/motion';
import {
  ConfigSectionNav,
  CONFIG_SECTIONS,
  TranslationSettingsSection,
  BackendSettingsSection,
  FileIOSettingsSection,
  TextProcessingSettingsSection,
  DictionarySettingsSection,
  ProblemAnalyzeSection,
  RetranslKeySection,
  ProblemFilterSection,
  ProjectGuidelineSection,
  type ConfigSectionKey,
} from './project-config';

export function ProjectConfigPage({ ctx }: { ctx: ProjectPageContext }) {
  useUiLanguage();
  const { projectDir, projectId, configFileName } = ctx;

  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [problemPlugins, setProblemPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [dirty, setDirty] = useState(false);
  useRetainPage(dirty || saving);
  const configRef = useRef(config);
  configRef.current = config;
  const contextRef = useRef('');
  contextRef.current = `${projectId}:${configFileName}`;
  const active = usePageActive();
  const [searchParams] = useSearchParams();
  const [activeSection, setActiveSection] = useState<ConfigSectionKey>(() => {
    const s = searchParams.get('section');
    // 兼容原来的插件设置链接，默认进入排在首位的文件读写。
    if (s === 'plugin') return 'fileIO';
    return CONFIG_SECTIONS.find((section) => section.key === s)?.key ?? 'fileIO';
  });
  const [yamlView, setYamlView] = useState(false);
  useEffect(() => {
    const section = searchParams.get('section');
    if (section === 'plugin') setActiveSection('fileIO');
    else if (CONFIG_SECTIONS.some((item) => item.key === section)) setActiveSection(section as ConfigSectionKey);
  }, [searchParams]);

  const wasActiveRef = useRef(active);
  const refreshAllowedRef = useRef(false);
  refreshAllowedRef.current = active && !dirty && !saving;
  useEffect(() => {
    const wasActive = wasActiveRef.current;
    wasActiveRef.current = active;
    if (wasActive || !refreshAllowedRef.current || !projectId) return;
    let cancelled = false;
    void fetchProjectConfig(projectId, configFileName).then((data) => {
      if (!cancelled && refreshAllowedRef.current) setConfig(data.config);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [active, projectId, configFileName]);

  // Global backend profile selection
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<string>('');
  const [defaultProfileName, setDefaultProfileName] = useState(() => getDefaultBackendProfile());

  // Plugin lists from global plugin manager
  const [filePlugins, setFilePlugins] = useState<PluginInfo[]>([]);
  const [textPlugins, setTextPlugins] = useState<PluginInfo[]>([]);

  useEffect(() => {
    if (projectDir) {
      setProjectConfigDirty(projectDir, dirty);
    }
  }, [projectDir, dirty]);

  // Ref for scroll-to-section
  const mainRef = useRef<HTMLDivElement>(null);

  // Load config
  useEffect(() => {
    if (!projectId) return;
    setSaving(false);
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
        if (!cancelled) setError(normalizeError(err, uiMessage("config:projectConfigPage.projectConfigPage_normalizeError_loadConfigFailed")));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId, configFileName]);

  // YAML 源码视图：由后端按保存时同样的格式渲染，所见即写入文件的内容
  const [yamlText, setYamlText] = useState<string | null>(null);
  const [yamlError, setYamlError] = useMessageState<string | null>(null);
  useEffect(() => {
    if (!yamlView || !config || !projectId) {
      setYamlText(null);
      return;
    }
    const controller = new AbortController();
    setYamlError(null);
    previewProjectConfigYaml(projectId, config, controller.signal)
      .then((data) => setYamlText(data.yaml))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setYamlText(null);
        setYamlError(normalizeError(err, uiMessage("config:projectConfigPage.yamlPreview_normalizeError_renderFailed")));
      });
    return () => controller.abort();
  }, [yamlView, config, projectId]);

  // Load backend profile names and current selection
  useEffect(() => {
    setBackendProfileNames(getBackendProfileNames());
    if (projectDir) {
      setSelectedProfile(getSelectedBackendProfileDisplay(projectDir));
    }
  }, [projectDir]);

  useEffect(() => {
    const handler = () => setBackendProfileNames(getBackendProfileNames());
    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, handler);
    return () => window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, handler);
  }, []);

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

  useEffect(() => {
    let cancelled = false;
    setProblemPlugins([]);
    if (projectId) fetchPlugins(projectId).then((plugins) => {
      if (!cancelled) setProblemPlugins(plugins.filter((plugin) => plugin.type === 'problem'));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

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
    // common 下的配置项都是平铺的点号键（后端按 `gpt.xxx.yyy` 整体读取）。
    // 文件里还没有这个键时不能按点号拆成嵌套对象，否则后端读不到。
    if (keys[0] === 'common' && keys.length > 2) {
      const isObject = (v: unknown): v is Record<string, unknown> => v != null && typeof v === 'object' && !Array.isArray(v);
      if (!isObject(result.common)) result.common = {};
      const common = result.common as Record<string, unknown>;
      common[keys.slice(1).join('.')] = value;
      // 清掉以前误写出的同名嵌套值（只删叶子，不动还有别的子项的对象）
      const parents: Record<string, unknown>[] = [common];
      let node: unknown = common;
      for (const key of keys.slice(1, -1)) {
        node = isObject(node) ? node[key] : undefined;
        if (!isObject(node)) return result;
        parents.push(node);
      }
      const leafParent = parents[parents.length - 1];
      const leafKey = keys[keys.length - 1];
      if (!Object.prototype.hasOwnProperty.call(leafParent, leafKey) || isObject(leafParent[leafKey])) return result;
      delete leafParent[leafKey];
      for (let i = parents.length - 1; i > 0 && Object.keys(parents[i]).length === 0; i--) {
        delete parents[i - 1][keys[i]];
      }
      return result;
    }
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
    if (!projectId || !config) return false;
    const context = `${projectId}:${configFileName}`;
    setSaving(true);
    setError(null);
    setSaveSuccess(false);
    try {
      await updateProjectConfig(projectId, {
        config,
        config_file_name: configFileName });
      if (contextRef.current === context && configRef.current === config) {
        setSaveSuccess(true);
        setDirty(false);
      }
      return true;
    } catch (err) {
      if (contextRef.current === context) setError(normalizeError(err, uiMessage("config:projectConfigPage.handleSave_normalizeError_saveConfigFailed")));
      return false;
    } finally {
      if (contextRef.current === context) setSaving(false);
    }
  }, [projectId, config, configFileName]);

  // Scroll to active section
  const handleSectionChange = useCallback((section: ConfigSectionKey) => {
    setActiveSection(section);
    setYamlView(false);
    // Scroll main area to top so new section is visible
    if (mainRef.current) {
      mainRef.current.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    }
  }, []);

  if (loading) {
    return (
      <div className="project-config-page">
        <PageHeader className="project-config-page__header" title={translate("config:projectConfigPage.projectConfigPage_title_configEdit")} />
        <LoadingState title={translate("config:projectConfigPage.projectConfigPage_title_loadConfig")} description={translate("config:projectConfigPage.projectConfigPage_description_pendingRead", { configFileName: configFileName })} />
      </div>
    );
  }

  if (error && !config) {
    return (
      <div className="project-config-page">
        <PageHeader className="project-config-page__header" title={translate("config:projectConfigPage.projectConfigPage_title_configEdit")} />
        <ErrorState title={translate("config:projectConfigPage.projectConfigPage_title_loadConfigFailed")} description={error} />
      </div>
    );
  }

  const commonConfig = (config?.common || {}) as Record<string, unknown>;

  return (
    <div className="project-config-page">
      <PageHeader className="project-config-page__header" title={translate("config:projectConfigPage.projectConfigPage_title_configEdit")} description={translate("config:projectConfigPage.projectConfigPage_description_editProjectConfigFile", { configFileName: configFileName })} />

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
            <InlineFeedback tone="error" title={translate("config:projectConfigPage.projectConfigPageMain_title_configSaveFailed")} description={error} />
          )}
          {saveSuccess && (
            <InlineFeedback className="inline-alert--floating" tone="success" title={translate("config:projectConfigPage.projectConfigPageMain_title_configDoneSave")} description={translate("config:projectConfigPage.projectConfigPageMain_description_currentProjectConfigDoneSuccess")} onDismiss={() => setSaveSuccess(false)} />
          )}

          <fieldset key={yamlView ? 'yaml' : activeSection} className="section-fade-in project-config-fields" disabled={saving}>
          {yamlView ? (
            <Panel title={translate("config:projectConfigPage.sectionFadeInProjectConfigFields_title_yAMLSource")} description={translate("config:projectConfigPage.sectionFadeInProjectConfigFields_description_editYAMLConfigSourcePreviewChange")}>
              {yamlError ? (
                <InlineFeedback tone="error" title={translate("config:projectConfigPage.yamlPreview_title_renderFailed")} description={yamlError} />
              ) : (
                <pre className="yaml-preview">
                  {!config
                    ? translate("config:projectConfigPage.yamlPreview_message_config")
                    : yamlText ?? translate("config:projectConfigPage.yamlPreview_message_rendering")}
                </pre>
              )}
            </Panel>
          ) : (
            <>
              {activeSection === 'common' && (
                <TranslationSettingsSection
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

              {activeSection === 'fileIO' && (
                <FileIOSettingsSection
                  config={config}
                  filePlugins={filePlugins}
                  onFieldChange={handleFieldChange}
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
                />
              )}

              {activeSection === 'textProcessing' && (
                <TextProcessingSettingsSection
                  config={config}
                  textPlugins={textPlugins}
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
                  projectId={projectId}
                  problemPlugins={problemPlugins}
                  onPluginSettingChange={handlePluginSettingChange}
                  onProblemPluginsChange={(plugins) => handleListFieldChange('plugin.problemPlugins', plugins)}
                  onProblemListChange={(lines) => {
                    setConfig((prev) => {
                      const pa = { ...((prev?.problemAnalyze as Record<string, unknown>) || {}) };
                      pa.problemList = lines;
                      if (lines === null) delete pa.GPT35;
                      return prev ? { ...prev, problemAnalyze: pa } : prev;
                    });
                  }}
                  onDirty={() => { setSaveSuccess(false); setDirty(true); }}
                />
              )}

              {activeSection === 'projectGuideline' && (
                <ProjectGuidelineSection projectId={projectId} projectDir={projectDir} />
              )}

              {activeSection === 'retranslKey' && (
                <RetranslKeySection
                  key="retranslKey"
                  config={config}
                  onChange={(keys) => {
                    setConfig((prev) => {
                      if (!prev) return prev;
                      const common = { ...((prev.common as Record<string, unknown>) || {}) };
                      common.retranslKey = keys;
                      return { ...prev, common };
                    });
                  }}
                  onDirty={() => { setSaveSuccess(false); setDirty(true); }}
                />
              )}

              {activeSection === 'problemFilterKey' && (
                <ProblemFilterSection
                  key="problemFilterKey"
                  config={config}
                  onChange={(field, keys) => {
                    setConfig((prev) => {
                      if (!prev) return prev;
                      const common = { ...((prev.common as Record<string, unknown>) || {}) };
                      common[field] = keys;
                      return { ...prev, common };
                    });
                    setSaveSuccess(false);
                    setDirty(true);
                  }}
                  onDirty={() => { setSaveSuccess(false); setDirty(true); }}
                />
              )}
            </>
          )}
          </fieldset>

        </div>
      </div>
    </div>
  );
}
