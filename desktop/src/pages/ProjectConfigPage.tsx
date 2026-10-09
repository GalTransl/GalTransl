import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useRef, useState } from 'react';
import { usePageActive, useRetainPage } from '../components/PageActivity';
import { useSearchParams } from 'react-router-dom';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Panel } from '../components/Panel';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, InlineFeedback, LoadingState } from '../components/page-state';
import {
  type PluginInfo,
  type InputReextractResult,
  fetchProjectConfig,
  updateProjectConfig,
  reextractMsgtoolInput,
  fetchPlugins,
  getBackendProfileNames,
  getDefaultBackendProfile,
  getSelectedBackendProfileDisplay,
  setSelectedBackendProfile,
  setProjectConfigDirty,
  BACKEND_PROFILES_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT } from '../lib/api';
import { normalizeError } from '../lib/errors';
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
  const [reextracting, setReextracting] = useState(false);
  useRetainPage(dirty || saving || reextracting);
  const [reextractResult, setReextractResult] = useState<InputReextractResult | null>(null);
  const [reextractError, setReextractError] = useMessageState<string | null>(null);
  const reextractRequestRef = useRef(0);
  const reextractBusyRef = useRef(false);
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
  refreshAllowedRef.current = active && !dirty && !saving && !reextracting;
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
    reextractRequestRef.current += 1;
    reextractBusyRef.current = false;
    setReextracting(false);
    setReextractResult(null);
    setReextractError(null);
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

  const handleReextract = useCallback(async () => {
    if (!projectId || !config || saving || reextractBusyRef.current) return;
    reextractBusyRef.current = true;
    const request = ++reextractRequestRef.current;
    const isCurrent = () => request === reextractRequestRef.current;
    setReextracting(true);
    setReextractResult(null);
    setReextractError(null);
    try {
      const saved = await handleSave();
      if (!isCurrent()) return;
      if (!saved) {
        setReextractError(uiMessage("config:projectConfigPage.handleReextract_setReextractError_configSaveFailedNotStartExtract"));
        return;
      }
      const result = await reextractMsgtoolInput(projectId, configFileName);
      if (isCurrent()) setReextractResult(result);
    } catch (err) {
      if (isCurrent()) setReextractError(normalizeError(err, uiMessage("config:projectConfigPage.handleReextract_normalizeError_extractSourceFailed")));
    } finally {
      if (isCurrent()) {
        reextractBusyRef.current = false;
        setReextracting(false);
      }
    }
  }, [projectId, config, configFileName, saving, handleSave]);

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
          disabled={!config || reextracting}
        />

        <div className="project-config-page__main" ref={mainRef}>
          {error && (
            <InlineFeedback tone="error" title={translate("config:projectConfigPage.projectConfigPageMain_title_configSaveFailed")} description={error} />
          )}
          {saveSuccess && (
            <InlineFeedback className="inline-alert--floating" tone="success" title={translate("config:projectConfigPage.projectConfigPageMain_title_configDoneSave")} description={translate("config:projectConfigPage.projectConfigPageMain_description_currentProjectConfigDoneSuccess")} onDismiss={() => setSaveSuccess(false)} />
          )}

          <fieldset key={yamlView ? 'yaml' : activeSection} className="section-fade-in project-config-fields" disabled={saving || reextracting}>
          {yamlView ? (
            <Panel title={translate("config:projectConfigPage.sectionFadeInProjectConfigFields_title_yAMLSource")} description={translate("config:projectConfigPage.sectionFadeInProjectConfigFields_description_editYAMLConfigSourcePreviewChange")}>
              <pre className="yaml-preview">
                {config ? JSON.stringify(config, null, 2) : translate("config:projectConfigPage.yamlPreview_message_config")}
              </pre>
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
                  reextractAction={(
                    <div className="plugin-setting-row">
                      <span className="plugin-setting-row__label">{translate("config:projectConfigPage.pluginSettingRow_message_sourceExtract")}</span>
                      <div className="plugin-setting-row__control">
                        <Button type="button" variant="secondary" className="plugin-reextract-button"
                          onClick={() => void handleReextract()} disabled={saving || reextracting}>
                          {reextracting ? (saving ? translate("config:projectConfigPage.pluginReextractButton_message_pendingSaveConfig") : translate("config:projectConfigPage.pluginReextractButton_message_pendingExtract")) : translate("config:projectConfigPage.pluginReextractButton_message_extractSource")}
                        </Button>
                        <span className="plugin-setting-row__hint">{translate("config:projectConfigPage.pluginSettingRowControl_message_saveCurrentConfigExtractPluginProcessAll")}</span>
                        {reextractError && <p role="alert">{reextractError}</p>}
                        {reextractResult && (
                          <div role="status">
                            {reextractResult.refreshed.length === 0 && reextractResult.errors.length === 0
                              ? translate("config:projectConfigPage.pluginSettingRowControl_message_notPluginProcessFile")
                              : translate("config:projectConfigPage.pluginSettingRowControl_message_extractCompleteSuccessCountFileSentenceFailed", { count: reextractResult.refreshed.length, total_entries: reextractResult.total_entries, count2: reextractResult.errors.length })}
                            {reextractResult.errors.length > 0 && (
                              <details open>
                                <summary>{translate("config:projectConfigPage.pluginSettingRowControl_message_failedFile")}</summary>
                                {reextractResult.errors.map((item) => <p key={item.filename}>{item.filename}：{item.error}</p>)}
                              </details>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )}
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
