import { localizePlugin } from "../i18n/plugins";
import { UiTrans, message as uiMessage, t as translate, useFeedbackState, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { Switch } from '../components/Switch';
import { Panel } from '../components/Panel';
import { PageHeader } from '../components/PageHeader';
import { Icon } from '../components/Icon';
import { PluginSettingsEditor } from '../components/PluginSettingsEditor';
import { InlineFeedback } from '../components/page-state';
import {
  BACKEND_PROFILES_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  type PluginInfo,
  getDefaultBackendProfile,
  getBackendProfileNames,
  fetchPlugins,
  fetchDefaultProjectConfigTemplate,
  fetchProjectConfig,
  fetchTranslationGuidelines,
  updateProjectConfig,
  submitJob,
  fetchJob,
  fetchProgramDir,
  fetchProjectFiles,
  encodeProjectDir,
  detectFilePlugin,
} from '../lib/api';
import type { FilePluginDetection } from '../lib/api';
import { addProjectToHistory } from './HomePage';
import { basenamePath, isAbsolutePath, joinPath, normalizeFileUriPath } from '../lib/paths';

const STEPS = ["projects:newProjectWizard.sTEPS_message_project","projects:newProjectWizard.sTEPS_message_importFile","projects:newProjectWizard.sTEPS_message_translationBackend","common:actions.commonSettings","projects:newProjectWizard.sTEPS_message_extractNameTable","common:actions.completed"] as const;
const LAST_PARENT_DIR_KEY = 'galtransl-new-project-last-parent-dir';

// 最后一步的后续流程指引：从生成字典到取回译文的正经顺序
const FLOW_STEPS: { title: string; description: string }[] = [
  {
    get title() { return translate("projects:newProjectWizard.title_title_gPTDictionary"); },
    get description() { return translate("projects:newProjectWizard.description_description_projectProjectDictionaryAIGPTDictionaryGenDic"); },
  },
  {
    get title() { return translate("projects:newProjectWizard.title_title_checkDictionary"); },
    get description() { return translate("projects:newProjectWizard.description_description_dictionarySave"); },
  },
  {
    get title() { return translate("projects:newProjectWizard.title_title_translation"); },
    get description() { return translate("projects:newProjectWizard.description_description_startTranslationCountFileConfirmProblem"); },
  },
  {
    get title() { return translate("projects:newProjectWizard.title_title_translationProblem"); },
    get description() { return translate("projects:newProjectWizard.description_description_textTranslationTextDetectProblemSentenceJapaneseJapanese"); },
  },
  {
    get title() { return translate("projects:newProjectWizard.title_title_text"); },
    get description() { return translate("projects:newProjectWizard.description_description_dictionaryRebuildaDictionaryCacheTranslationTextGtOutput"); },
  },
];

type NewProjectWizardProps = {
  active: boolean;
  onProjectNameChange: (name: string) => void;
  onOpenProject: (projectDir: string, config: string) => void;
};

export function NewProjectWizard({ active, onProjectNameChange, onOpenProject }: NewProjectWizardProps) {
  const uiLanguage = useUiLanguage();
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [stepDirection, setStepDirection] = useState<'forward' | 'backward'>('forward');
  const [feedback, setFeedback] = useFeedbackState(null);

  // Step 1 state
  const [parentDir, setParentDir] = useState(() => {
    try {
      return localStorage.getItem(LAST_PARENT_DIR_KEY) || '';
    } catch {
      return '';
    }
  });
  const [projectName, setProjectName] = useState('');
  useEffect(() => {
    onProjectNameChange(projectName.trim());
  }, [projectName, onProjectNameChange]);
  const [projectCreated, setProjectCreated] = useState(false);
  // 只有用户自己填/挑过的父目录才记进「上次用的目录」：默认值不该被当成他的选择记下来
  const [parentDirTouched, setParentDirTouched] = useState(false);

  // Step 2 state
  const [importedFiles, setImportedFiles] = useState<string[]>([]);

  // Step 3 state
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedBackend, setSelectedBackend] = useState('__default__');
  const [defaultBackendName, setDefaultBackendName] = useState(() => getDefaultBackendProfile());

  // Step 4 state
  const [filePlugins, setFilePlugins] = useState<PluginInfo[]>([]);
  const [selectedFilePlugin, setSelectedFilePlugin] = useState('file_galtransl_json');
  const [pluginOverrides, setPluginOverrides] = useState<Record<string, Record<string, unknown>>>({});
  const [fileDetection, setFileDetection] = useState<FilePluginDetection | null>(null);
  // 用户手动选过插件后，重新进入这一步不再用识别结果覆盖
  const filePluginTouchedRef = useRef(false);
  const [workersPerProject, setWorkersPerProject] = useState(16);
  const [numPerRequest, setNumPerRequest] = useState(16);
  const [dynamicNumPerRequest, setDynamicNumPerRequest] = useState(false);
  const [dynamicNumPerRequestMin, setDynamicNumPerRequestMin] = useState(8);
  const [dynamicNumPerRequestMax, setDynamicNumPerRequestMax] = useState(64);
  const [language, setLanguage] = useState('zh-cn');
  const [guidelines, setGuidelines] = useState<string[]>([]);
  const [translationGuideline, setTranslationGuideline] = useState('');
  const [settingsSaved, setSettingsSaved] = useState(false);

  // 目标语言与翻译规范的默认值跟随界面语言：英文界面默认 English + Basic.md，
  // 中文界面保持原样（zh-cn + 日译中_增强v2）。用户手动选过之后就不再跟随，
  // 切换界面语言只改默认值，不会覆盖已选内容。
  const languageTouchedRef = useRef(false);
  const translationGuidelineTouchedRef = useRef(false);
  const preferredGuidelines = useMemo(
    () => (uiLanguage === 'en'
      ? [translate("projects:newProjectWizard.newProjectWizard_message_basicMd")]
      : [translate("projects:newProjectWizard.newProjectWizard_message_v2Md"), translate("projects:newProjectWizard.newProjectWizard_message_md")]),
    [uiLanguage],
  );

  useEffect(() => {
    if (languageTouchedRef.current) return;
    setLanguage(uiLanguage === 'en' ? 'en' : 'zh-cn');
  }, [uiLanguage]);

  useEffect(() => {
    if (translationGuidelineTouchedRef.current) return;
    // 名字要带 .md——接口给的是文件名，少写扩展名会一个都匹配不上，
    // 静默落到列表首位，看起来就像"默认值没生效"。
    setTranslationGuideline(preferredGuidelines.find((name) => guidelines.includes(name)) || guidelines[0] || '');
  }, [guidelines, preferredGuidelines]);

  // Step 5 state
  const [nameJobStatus, setNameJobStatus] = useState<'idle' | 'running' | 'completed' | 'failed'>('idle');
  const [nameJobMessage, setNameJobMessage] = useMessageState<string>('');

  const projectDir = useMemo(() => {
    if (!parentDir || !projectName) return '';
    return joinPath(parentDir, projectName);
  }, [parentDir, projectName]);

  const gtInputDir = useMemo(() => {
    if (!projectDir) return '';
    return joinPath(projectDir, 'gt_input');
  }, [projectDir]);

  const importPathsToInput = useCallback(
    async (paths: string[]) => {
      if (!gtInputDir || paths.length === 0) return;

      const existingNames = new Set(importedFiles.map((name) => name.toLowerCase()));
      const namesInBatch = new Set<string>();
      const pathsToImport: string[] = [];
      const acceptedNames: string[] = [];

      for (const p of paths) {
        const name = basenamePath(p) || p;
        const key = name.toLowerCase();
        if (existingNames.has(key) || namesInBatch.has(key)) {
          continue;
        }
        namesInBatch.add(key);
        pathsToImport.push(p);
        acceptedNames.push(name);
      }

      if (pathsToImport.length === 0) {
        setFeedback({ type: 'info', message: uiMessage("projects:newProjectWizard.message_message_doneFilterFileImport") });
        return;
      }

      try {
        await invoke('copy_files', { sources: pathsToImport, destinationDir: gtInputDir });
        setImportedFiles((prev) => [...prev, ...acceptedNames]);
        const filteredCount = paths.length - pathsToImport.length;
        setFeedback({
          type: 'success',
          message: filteredCount > 0
            ? uiMessage("projects:newProjectWizard.message_setFeedback_doneImportCountFileDoneFilterCount", { count: pathsToImport.length, filteredCount: filteredCount })
            : uiMessage("projects:newProjectWizard.message_setFeedback_doneImportCountFile", { count: pathsToImport.length }),
        });
      } catch (err) {
        setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_importFailed", { value: err instanceof Error ? err.message : String(err) }) });
      }
    },
    [gtInputDir, importedFiles],
  );

  useEffect(() => {
    if (!active || currentStep !== 1) return;
    const currentWindow = getCurrentWebviewWindow();
    let disposed = false;

    const unlistenPromise = currentWindow.onDragDropEvent((event: unknown) => {
      if (disposed) return;
      const payload = (event as { payload?: { type?: string; paths?: string[] } })?.payload;
      if (payload?.type !== 'drop') return;
      const paths = Array.isArray(payload.paths) ? payload.paths : [];
      if (paths.length === 0) {
        setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_notReadFileSelectFileImport") });
        return;
      }
      void importPathsToInput(paths);
    });

    return () => {
      disposed = true;
      void unlistenPromise.then((unlisten) => {
        if (!disposed) return;
        unlisten();
      });
    };
  }, [active, currentStep, importPathsToInput]);

  useEffect(() => {
    try {
      if (parentDirTouched && parentDir.trim()) {
        localStorage.setItem(LAST_PARENT_DIR_KEY, parentDir);
      }
    } catch {
      // ignore storage errors
    }
  }, [parentDir, parentDirTouched]);

  // 进「导入文件」时以 gt_input 里的实际文件为准刷新列表：文件可能是用户直接打开目录粘贴进去的，
  // 这种不经向导导入的文件本地状态里没有，会让列表和后面的判断都误以为"没有文件"
  useEffect(() => {
    if (!active || currentStep !== 1 || !projectDir) return;
    let cancelled = false;
    fetchProjectFiles(encodeProjectDir(projectDir))
      .then((res) => {
        if (cancelled) return;
        setImportedFiles(
          (res.input_files || []).filter((entry) => entry.is_file).map((entry) => entry.name),
        );
      })
      .catch(() => {
        // 列不出来就沿用本地记录（导入过的那些）
      });
    return () => {
      cancelled = true;
    };
  }, [active, currentStep, projectDir]);

  // 没记过上次用过的目录时，「父目录」默认填程序所在目录（只填空着的，不覆盖已有值）
  useEffect(() => {
    let cancelled = false;
    void fetchProgramDir().then((dir) => {
      if (cancelled || !dir) return;
      setParentDir((current) => (current.trim() ? current : dir));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Step 1: Create project ──
  const handleSelectParentDir = useCallback(async () => {
    const selected = await open({ directory: true });
    if (selected && typeof selected === 'string') {
      setParentDir(selected);
      setParentDirTouched(true);
    }
  }, []);

  const handleCreateProject = useCallback(async (): Promise<boolean> => {
    if (!projectDir) {
      setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_chooseDirectoryProjectName") });
      return false;
    }
    // 目标目录里已有 config.yaml 时不覆盖，避免把已有项目的配置冲掉
    const alreadyExists = await fetchProjectConfig(encodeProjectDir(projectDir), 'config.yaml')
      .then(() => true)
      .catch(() => false);
    if (alreadyExists) {
      setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_directoryDoneConfigYamlCountProjectName") });
      return false;
    }
    try {
      const configYaml = await fetchDefaultProjectConfigTemplate();
      await invoke('create_dir', { path: projectDir });
      await invoke('create_dir', { path: joinPath(projectDir, 'gt_input') });
      await invoke('create_dir', { path: joinPath(projectDir, 'gt_output') });
      await invoke('create_dir', { path: joinPath(projectDir, 'transl_cache') });
      await invoke('write_text_file', { path: joinPath(projectDir, 'config.yaml'), content: configYaml });
      setProjectCreated(true);
      // 先记入历史：中途离开向导（比如去模型设置）也能从首页找回这个项目
      addProjectToHistory(projectDir, 'config.yaml');
      setFeedback({ type: 'success', message: uiMessage("projects:newProjectWizard.message_message_projectCreateSuccess") });
      return true;
    } catch (err) {
      setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_createFailed", { value: err instanceof Error ? err.message : String(err) }) });
      return false;
    }
  }, [projectDir]);

  // ── Step 2: Import files ──
  const handleFileDrop = useCallback(
    async (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      e.currentTarget.classList.remove('drop-zone--over');
      if (!gtInputDir) return;
      const files = Array.from(e.dataTransfer.files);

      const directPaths = files
        .map((f) => (f as File & { path?: string }).path)
        .filter((p): p is string => Boolean(p && p.trim()));

      const parseDroppedUriList = () => {
        const uriData = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
        if (!uriData) return [] as string[];

        return uriData
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line && !line.startsWith('#'))
          .map(normalizeFileUriPath)
          .filter(isAbsolutePath);
      };

      const droppedPaths = directPaths.length > 0 ? directPaths : parseDroppedUriList();
      if (droppedPaths.length === 0) {
        setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_notReadFileSelectFileImport") });
        return;
      }
      await importPathsToInput(droppedPaths);
    },
    [gtInputDir, importPathsToInput],
  );

  const handleFilePick = useCallback(async () => {
    if (!gtInputDir) return;
    const selected = await open({ multiple: true });
    if (!selected) return;
    const paths = Array.isArray(selected) ? selected : [selected];
    await importPathsToInput(paths as string[]);
  }, [gtInputDir, importPathsToInput]);

  const handleOpenInputFolder = useCallback(async () => {
    if (!gtInputDir) return;
    try {
      await invoke('open_folder', { path: gtInputDir });
    } catch (err) {
      setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_openFileFailed", { value: err instanceof Error ? err.message : String(err) }) });
    }
  }, [gtInputDir]);

  // ── Step 3: Load backend profiles on entry ──
  useEffect(() => {
    if (currentStep !== 2) return;
    setBackendProfileNames(getBackendProfileNames());
  }, [currentStep]);

  useEffect(() => {
    const onDefaultBackendChange = () => {
      setDefaultBackendName(getDefaultBackendProfile());
    };
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onDefaultBackendChange);
    return () => window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onDefaultBackendChange);
  }, []);

  useEffect(() => {
    const onProfilesChange = () => {
      if (currentStep === 2) {
        setBackendProfileNames(getBackendProfileNames());
      }
    };
    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, onProfilesChange);
    return () => window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, onProfilesChange);
  }, [currentStep]);

  // ── Step 4: Load plugins on entry ──
  useEffect(() => {
    if (currentStep !== 3) return;
    fetchPlugins()
      .then(async (plugins) => {
        setFilePlugins(plugins.filter((p) => p.type === 'file'));
        if (!projectDir) return;
        const { config } = await fetchProjectConfig(encodeProjectDir(projectDir), 'config.yaml');
        const configured = (config.plugin as Record<string, unknown>) || {};
        setPluginOverrides((previous) => {
          const next = { ...previous };
          for (const plugin of plugins.filter((p) => p.type === 'file')) {
            const saved = configured[plugin.module || plugin.name.replace('(project_dir)', '')];
            if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
              next[plugin.name] = { ...(saved as Record<string, unknown>), ...previous[plugin.name] };
            }
          }
          return next;
        });
      })
      .catch(() => {});
    if (projectDir) {
      detectFilePlugin(encodeProjectDir(projectDir))
        .then((detection) => {
          setFileDetection(detection);
          if (detection.suggested && !filePluginTouchedRef.current) {
            setSelectedFilePlugin(detection.suggested);
          }
        })
        .catch(() => setFileDetection(null));
    }
    // 默认值由上面的语言相关 effect 挑选，这里只负责拉列表。
    fetchTranslationGuidelines()
      .then(setGuidelines)
      .catch(() => {});
  }, [currentStep, projectDir]);

  const handleSaveSettings = useCallback(async (): Promise<boolean> => {
    if (!projectDir) return false;
    try {
      const projectId = encodeProjectDir(projectDir);
      const res = await fetchProjectConfig(projectId, 'config.yaml');
      const config = { ...res.config };

      // Update common settings
      const common = { ...((config.common as Record<string, unknown>) || {}) };
      common.workersPerProject = workersPerProject;
      common.language = language;

      common['gpt.numPerRequestTranslate'] = numPerRequest;
      common['gpt.dynamicNumPerRequestTranslate'] = dynamicNumPerRequest;
      common['gpt.dynamicNumPerRequestTranslate.min'] = dynamicNumPerRequestMin;
      common['gpt.dynamicNumPerRequestTranslate.max'] = dynamicNumPerRequestMax;
      common['gpt.contextNum'] = 8;
      if (translationGuideline) {
        common['gpt.translation_guideline'] = translationGuideline;
      }

      config.common = common;

      const plugin: Record<string, unknown> = {
        ...((config.plugin as Record<string, unknown>) || {}),
        filePlugin: selectedFilePlugin,
      };
      if (!Array.isArray(plugin.textPlugins)) {
        plugin.textPlugins = [];
      }
      for (const [name, overrides] of Object.entries(pluginOverrides)) {
        const info = filePlugins.find((p) => p.name === name);
        const module = info?.module || name.replace('(project_dir)', '');
        plugin[module] = { ...((plugin[module] as Record<string, unknown>) || {}), ...overrides };
      }
      config.plugin = plugin;

      await updateProjectConfig(projectId, { config, config_file_name: 'config.yaml' });

      // Save backend profile selection
      const { setSelectedBackendProfile } = await import('../lib/api');
      setSelectedBackendProfile(projectDir, selectedBackend);

      setSettingsSaved(true);
      setFeedback({ type: 'success', message: uiMessage("projects:newProjectWizard.message_message_settingsDoneSave") });
      return true;
    } catch (err) {
      setFeedback({ type: 'error', message: uiMessage("projects:newProjectWizard.message_message_saveFailed", { value: err instanceof Error ? err.message : String(err) }) });
      return false;
    }
  }, [projectDir, workersPerProject, language, numPerRequest, dynamicNumPerRequest, dynamicNumPerRequestMin, dynamicNumPerRequestMax, selectedFilePlugin, selectedBackend, translationGuideline, pluginOverrides, filePlugins]);

  // ── Step 5: Auto-extract names on entry ──
  useEffect(() => {
    if (currentStep !== 4 || nameJobStatus !== 'idle' || !projectDir) return;

    const run = async () => {
      try {
        setNameJobStatus('running');
        // 空输入目录就直接给友好提示，不提交 dump-name 任务。判断以 gt_input 里的实际文件为准：
        // 文件可能是用户直接打开目录粘贴进去的（不经向导导入），只看 importedFiles 会误判成空
        let hasInputFiles = true; // 列目录失败时不拦，交给 dump-name 自己处理
        try {
          const res = await fetchProjectFiles(encodeProjectDir(projectDir));
          hasInputFiles = (res.input_files || []).some((entry) => entry.is_file);
        } catch {
          hasInputFiles = true;
        }
        if (!hasInputFiles) {
          setNameJobStatus('completed');
          setNameJobMessage(uiMessage("projects:newProjectWizard.run_setNameJobMessage_gtInputEmptyFileDoneNameTableExtract"));
          return;
        }

        const job = await submitJob({
          project_dir: projectDir,
          config_file_name: 'config.yaml',
          translator: 'dump-name',
        });

        const poll = async () => {
          try {
            const status = await fetchJob(job.job_id);
            if (status.status === 'completed') {
              setNameJobStatus('completed');
              setNameJobMessage(status.success ? uiMessage("projects:newProjectWizard.poll_setNameJobMessage_nameTableExtractComplete") : uiMessage("projects:newProjectWizard.poll_setNameJobMessage_extractComplete", { value: status.error || '' }));
            } else if (status.status === 'failed') {
              setNameJobStatus('failed');
              setNameJobMessage(status.error || uiMessage("projects:newProjectWizard.poll_setNameJobMessage_extractFailed"));
            } else {
              setTimeout(poll, 2000);
            }
          } catch {
            setTimeout(poll, 3000);
          }
        };
        poll();
      } catch (err) {
        setNameJobStatus('failed');
        setNameJobMessage(err instanceof Error ? err.message : String(err));
      }
    };
    run();
    // eslint-disable-next-line react-hooks/react-hooks
  }, [currentStep]); // intentionally only depend on currentStep

  const handleFinish = useCallback(() => {
    if (!projectDir) return;
    onOpenProject(projectDir, 'config.yaml');
    addProjectToHistory(projectDir, 'config.yaml');
    const projectId = encodeProjectDir(projectDir);
    navigate(`/project/${projectId}/translate`);
  }, [projectDir, navigate, onOpenProject]);

  // 「下一步」本身会完成创建项目 / 保存设置，不再要求先点单独的按钮
  const canNext = useMemo(() => {
    if (currentStep === 0) return projectCreated || Boolean(parentDir.trim() && projectName.trim());
    if (currentStep === 1) return true; // file import is optional
    if (currentStep === 2) return true; // backend selection is optional
    if (currentStep === 3) return true;
    if (currentStep === 4) return true; // 人名提取是后台任务，不拦着往后走
    return false;
  }, [currentStep, projectCreated, parentDir, projectName]);

  const stepProgress = useMemo(
    () => Math.round(((currentStep + 1) / STEPS.length) * 100),
    [currentStep],
  );

  useEffect(() => {
    if (!settingsSaved) return;
    setSettingsSaved(false);
  }, [selectedBackend, selectedFilePlugin, workersPerProject, numPerRequest, language]);

  // ── Step indicator ──
  const renderStepIndicator = () => (
    <ul className="wizard-steps">
      {STEPS.map((label, i) => (
        <li
          key={i}
          className={`wizard-step${i === currentStep ? ' wizard-step--active' : ''}${i < currentStep ? ' wizard-step--completed' : ''}`}
        >
          <span className="wizard-step__number">{i < currentStep ? <Icon name="check" /> : i + 1}</span>
          <span className="wizard-step__label">{translate(label)}</span>
        </li>
      ))}
    </ul>
  );

  // ── Step 1 ──
  const renderStep1 = () => (
    <Panel title={translate("projects:newProjectWizard.renderStep1_title_project")} description={translate("projects:newProjectWizard.renderStep1_description_selectProjectFileSaveProjectNameCreate")}>
      <div className="wizard-form-grid">
        <div className="field">
          <span className="field__label">{translate("projects:newProjectWizard.field_message_projectName")}</span>
          <input
            className="field__input"
            autoComplete="off"
            value={projectName}
            onChange={(e) => { setProjectName(e.target.value); setProjectCreated(false); }}
            placeholder={translate("projects:newProjectWizard.field_placeholder_myProject")}
          />
        </div>
        <div className="field">
          <span className="field__label">{translate("projects:newProjectWizard.field_message_directory")}</span>
          <div className="field__row">
            <input
              className="field__input"
              autoComplete="off"
              value={parentDir}
              onChange={(e) => { setParentDir(e.target.value); setParentDirTouched(true); setProjectCreated(false); }}
              placeholder={translate("projects:newProjectWizard.fieldRow_placeholder_homeUserGalTranslProjectsEGalTranslProjects")}
            />
            <Button className="field__browse-button" variant="secondary" onClick={() => void handleSelectParentDir()}>{translate("projects:newProjectWizard.fieldRow_message_text")}</Button>
          </div>
          <span className="field__hint">{translate("projects:newProjectWizard.field_message_defaultDirectoryEnglish")}</span>
        </div>
        <div className="wizard-path-preview">
          <span className="wizard-path-preview__label">{translate("projects:newProjectWizard.wizardPathPreview_message_createDirectory")}</span>
          <code className="wizard-path-preview__path">{projectDir || translate("projects:newProjectWizard.wizardPathPreviewPath_message_directoryProjectName")}</code>
          <div className="wizard-path-preview__meta">{translate("projects:newProjectWizard.wizardPathPreview_message_gtInputGtOutputTranslCacheConfig")}</div>
        </div>
      </div>
      {projectCreated ? (
        <div className="wizard-tip-card">
          <strong><Icon name="check" />{translate("projects:newProjectWizard.wizardTipCard_strong_projectDoneCreate")}</strong>
          <span>{translate("projects:newProjectWizard.wizardTipCard_message_nextImportFile")}</span>
        </div>
      ) : null}
    </Panel>
  );

  // ── Step 2 ──
  const renderStep2 = () => (
    <Panel title={translate("projects:newProjectWizard.renderStep2_title_importFile")} description={translate("projects:newProjectWizard.renderStep2_description_translationFileImportProjectGtInputDirectory")}>
      <div
        className={`drop-zone${importedFiles.length > 0 ? ' drop-zone--filled' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          e.currentTarget.classList.add('drop-zone--over');
        }}
        onDragLeave={(e) => {
          if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
          e.currentTarget.classList.remove('drop-zone--over');
        }}
        onDrop={(e) => void handleFileDrop(e)}
      >
        {importedFiles.length > 0 ? (
          <>
            <div className="drop-zone__files-header">
              <strong className="drop-zone__text">{translate("projects:newProjectWizard.dropZoneFilesHeader_message_doneImportCountFile", { count: importedFiles.length })}</strong>
              <span>{translate("projects:newProjectWizard.dropZoneFilesHeader_message_fileAdd")}</span>
            </div>
            <ul className="wizard-file-list" aria-label={translate("projects:newProjectWizard.wizardFileList_ariaLabel_doneImportFile")}>
              {importedFiles.map((file) => (
                <li key={file} className="wizard-file-list__item">{file}</li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <div className="drop-zone__icon"><Icon name="folder" /></div>
            <div className="drop-zone__text">{translate("projects:newProjectWizard.renderStep2_message_fileImport")}</div>
          </>
        )}
      </div>
      <div className="wizard-actions">
        <Button variant="secondary" onClick={() => void handleFilePick()}>{translate("projects:newProjectWizard.wizardActions_message_selectFile")}</Button>
        <Button variant="secondary" onClick={() => void handleOpenInputFolder()} disabled={!gtInputDir}>{translate("projects:newProjectWizard.wizardActions_message_openFile")}</Button>
      </div>
      <div className="wizard-tip-card">
        <strong>{translate("projects:newProjectWizard.wizardTipCard_message_file")}</strong>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_extractGalTranslMtoolJSONTranslatorXLSX")}</span>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_textTXTMarkdownMdMarkdownEPUBSRT")}</span>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_galgameKsScnAstAsbBgiCst")}</span>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_binMesTxtBGIFilePluginSettings")}</span>
        <span><UiTrans k="projects:newProjectWizard.wizardTipCard_message_countFile0GtInput0Directory" components={[<code />]} /></span>
      </div>
      <div className="wizard-tip-card">
        <strong>{translate("projects:newProjectWizard.wizardTipCard_message_galgameHint")}</strong>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_formatAutoExtractImportCompleteProjectCreate")}</span>
      </div>
    </Panel>
  );

  // ── Step 3 ──
  const renderStep3 = () => (
    <Panel title={translate("projects:newProjectWizard.renderStep3_title_translationBackend")} description={translate("projects:newProjectWizard.renderStep3_description_selectTranslationBackendConfigConfigEditSettings")}>
      <div className="field">
        <span className="field__label">{translate("projects:newProjectWizard.field_message_backendConfig")}</span>
        <CustomSelect value={selectedBackend} onChange={(e) => setSelectedBackend(e.target.value)}>
          <option value="__default__">{translate("projects:newProjectWizard.field_message_default")}</option>
          <option value="">{translate("projects:newProjectWizard.field_message_projectConfig")}</option>
          {backendProfileNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </CustomSelect>
        <span className="field__hint">
          {selectedBackend === '__default__'
            ? defaultBackendName
              ? translate("projects:newProjectWizard.fieldHint_message_currentDefaultConfigModelSettingsChange", { defaultBackendName: defaultBackendName })
              : translate("projects:newProjectWizard.fieldHint_message_notSettingsDefaultConfigModelSettingsSettings")
            : selectedBackend
              ? translate("projects:newProjectWizard.fieldHint_message_translationConfigProjectBackendSettings", { selectedBackend: selectedBackend })
              : translate("projects:newProjectWizard.fieldHint_message_configProjectBackendSettings")}
        </span>
      </div>
      {backendProfileNames.length === 0 ? (
        <div className="wizard-tip-card wizard-tip-card--warning">
          <strong><Icon name="warning" />{translate("projects:newProjectWizard.wizardTipCardWizardTipCardWarning_strong_emptyModelConfig")}</strong>
          <span>{translate("projects:newProjectWizard.wizardTipCardWizardTipCardWarning_message_emptyModelUnableTranslationCompleteModelSettings")}</span>
          <div>
            <Button variant="secondary" onClick={() => navigate('/backend-profiles')}>{translate("projects:newProjectWizard.wizardTipCardWizardTipCardWarning_message_modelSettings")}</Button>
          </div>
        </div>
      ) : (
        <div className="wizard-tip-card">
          <strong>{translate("projects:newProjectWizard.wizardTipCard_message_text")}</strong>
          <span>{translate("projects:newProjectWizard.wizardTipCard_message_defaultCountProjectModelSelectConfig")}</span>
        </div>
      )}
    </Panel>
  );

  // ── Step 4 ──
  const renderStep4 = () => (
    <Panel title={translate("common:actions.commonSettings")} description={translate("projects:newProjectWizard.renderStep4_description_settingsProjectTranslation")}>
      <div className="wizard-settings-grid">
      <div className="field wizard-settings-grid__full">
        <span className="field__label">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_filePlugin")}</span>
        <CustomSelect
          value={selectedFilePlugin}
          onChange={(e) => {
            filePluginTouchedRef.current = true;
            setSelectedFilePlugin(e.target.value);
          }}
        >
          <option value="auto">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_autoAuto")}</option>
          {filePlugins.length > 0 ? (
            filePlugins.map((p) => (
              <option key={p.name} value={p.name}>{localizePlugin(p).display_name} ({p.name})</option>
            ))
          ) : selectedFilePlugin !== 'auto' ? (
            <option value={selectedFilePlugin}>{selectedFilePlugin}</option>
          ) : null}
        </CustomSelect>
        {filePlugins.filter((p) => p.name === selectedFilePlugin && localizePlugin(p).description).map((plugin) => (
          <span key={plugin.name} className="field__hint" style={{ whiteSpace: 'pre-line' }}>{localizePlugin(plugin).description}</span>
        ))}
        <span className="field__hint">{describeFileDetection(fileDetection, filePlugins)}</span>
      </div>
      {filePlugins.filter((p) => p.name === selectedFilePlugin && Object.keys(p.settings || {}).length > 0).map((plugin) => (
        <div key={plugin.name} className="wizard-settings-grid__full">
          <PluginSettingsEditor
            plugin={plugin}
            overrides={pluginOverrides[plugin.name] || {}}
            onChange={(name, key, value) => {
              setPluginOverrides((previous) => ({
                ...previous,
                [name]: { ...(previous[name] || {}), [key]: value },
              }));
              setSettingsSaved(false);
            }}
          />
        </div>
      ))}
      <div className="field">
        <span className="field__label">{translate("projects:newProjectWizard.field_message_concurrencyFile")}</span>
        <input
          className="field__input"
          type="number"
          min={1}
          value={workersPerProject}
          onChange={(e) => setWorkersPerProject(Number(e.target.value))}
        />
        <span className="field__hint">{translate("projects:newProjectWizard.field_message_concurrencySource")}</span>
      </div>
      <div className="field">
        <span className="field__label">{translate("projects:newProjectWizard.field_message_translationSentence")}</span>
        <input
          className="field__input"
          type="number"
          min={1}
          value={numPerRequest}
          onChange={(e) => setNumPerRequest(Number(e.target.value))}
        />
        <span className="field__hint">{translate("projects:newProjectWizard.field_message_820")}</span>
      </div>
      <label className="field field--switch">
        <span className="field__label">{translate("projects:newProjectWizard.field_message_sentence")}</span>
        <Switch checked={dynamicNumPerRequest} onChange={setDynamicNumPerRequest} />
        <span className="field__hint">{translate("projects:newProjectWizard.field_message_errorAutoSentence")}</span>
      </label>
      {/* 关掉动态句数调整后，上下限没人用，收起来免得占地方、也免得误以为在生效 */}
      {dynamicNumPerRequest ? (
        <>
          <div className="field">
            <span className="field__label">{translate("projects:newProjectWizard.field_message_sentenceVariant2")}</span>
            <input
              className="field__input"
              type="number"
              min={1}
              value={dynamicNumPerRequestMin}
              onChange={(e) => setDynamicNumPerRequestMin(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <span className="field__label">{translate("projects:newProjectWizard.field_message_sentenceVariant3")}</span>
            <input
              className="field__input"
              type="number"
              min={1}
              value={dynamicNumPerRequestMax}
              onChange={(e) => setDynamicNumPerRequestMax(Number(e.target.value))}
            />
          </div>
        </>
      ) : null}
      <div className="field wizard-settings-grid__full">
        <span className="field__label">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_targetLanguage")}</span>
        <CustomSelect
          value={language}
          onChange={(e) => {
            languageTouchedRef.current = true;
            setLanguage(e.target.value);
          }}
        >
          <option value="zh-cn">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_simplifiedChinese")}</option>
          <option value="zh-tw">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_traditionalChinese")}</option>
          <option value="en">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_english")}</option>
          <option value="ja">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_text")}</option>
          <option value="ko">한국어</option>
        </CustomSelect>
      </div>
      <div className="field wizard-settings-grid__full">
        <span className="field__label">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_translationGuideline")}</span>
        <CustomSelect
          value={translationGuideline}
          onChange={(e) => {
            translationGuidelineTouchedRef.current = true;
            setTranslationGuideline(e.target.value);
          }}
        >
          {guidelines.length === 0 && translationGuideline === '' ? (
            <option value="">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_notTranslationGuidelineFile")}</option>
          ) : null}
          {translationGuideline && !guidelines.includes(translationGuideline) ? (
            <option value={translationGuideline}>{translationGuideline}</option>
          ) : null}
          {guidelines.map((g) => (
            <option key={g} value={g}>{g}</option>
          ))}
        </CustomSelect>
        <span className="field__hint">{translate("projects:newProjectWizard.fieldWizardSettingsGridFull_message_selectTranslationGuidelineFileTranslationGuidelinesFile")}</span>
      </div>
      </div>
    </Panel>
  );

  // ── Step 5 ──
  const renderStep5 = () => (
    <Panel title={translate("projects:newProjectWizard.renderStep5_title_extractNameTable")} description={translate("projects:newProjectWizard.renderStep5_description_autoProjectFileExtractNameTable")}>
      {nameJobStatus === 'running' && (
        <div className="wizard-progress">
          <div className="wizard-progress__bar">
            <div className="wizard-progress__fill" />
          </div>
          <div className="wizard-progress__text">{translate("projects:newProjectWizard.wizardProgress_message_pendingExtractNameTable")}</div>
        </div>
      )}
      {nameJobStatus === 'completed' && (
        <div className="wizard-message wizard-message--success">
          {nameJobMessage}
          <br />
          <span className="wizard-message__hint">{translate("projects:newProjectWizard.wizardMessageWizardMessageSuccess_message_projectNameTableTranslationAITranslationNameTable")}</span>
        </div>
      )}
      {nameJobStatus === 'failed' && (
        <div className="wizard-message wizard-message--error">{translate("projects:newProjectWizard.renderStep5_message_extractFailed", { nameJobMessage: nameJobMessage })}</div>
      )}
    </Panel>
  );

  // ── Step 6: 完成（后续翻译流程指引） ──
  const renderStep6 = () => (
    <Panel title={translate("projects:newProjectWizard.renderStep6_title_projectDone")} description={translate("projects:newProjectWizard.renderStep6_description_countTranslationProjectDictionaryProject")}>
      <ol className="wizard-flow">
        {FLOW_STEPS.map((step, index) => (
          <li key={index} className="wizard-flow__item">
            <span className="wizard-flow__index" aria-hidden="true">{index + 1}</span>
            <span className="wizard-flow__body">
              <strong>{step.title}</strong>
              <span>{step.description}</span>
            </span>
          </li>
        ))}
      </ol>
      <div className="wizard-tip-card">
        <strong>{translate("projects:newProjectWizard.wizardTipCard_message_hint")}</strong>
        <span>{translate("projects:newProjectWizard.wizardTipCard_message_completeOpenProjectOpenStartTranslationNameTable")}</span>
      </div>
    </Panel>
  );

  const stepRenderers = [renderStep1, renderStep2, renderStep3, renderStep4, renderStep5, renderStep6];

  const handlePrevStep = useCallback(() => {
    setStepDirection('backward');
    setCurrentStep((s) => Math.max(0, s - 1));
  }, []);

  const [advancing, setAdvancing] = useState(false);
  const handleNextStep = useCallback(async () => {
    if (advancing) return;
    setAdvancing(true);
    try {
      if (currentStep === 0 && !projectCreated && !(await handleCreateProject())) return;
      if (currentStep === 3 && !settingsSaved && !(await handleSaveSettings())) return;
      setStepDirection('forward');
      setCurrentStep((s) => Math.min(STEPS.length - 1, s + 1));
    } finally {
      setAdvancing(false);
    }
  }, [advancing, currentStep, projectCreated, settingsSaved, handleCreateProject, handleSaveSettings]);

  const nextLabel = currentStep === 0 && !projectCreated
    ? translate("projects:newProjectWizard.nextLabel_message_createProject")
    : currentStep === 3
      ? translate("projects:newProjectWizard.nextLabel_message_saveSettings")
      : translate("projects:newProjectWizard.nextLabel_message_next");

  return (
    <div className="wizard-page">
      <PageHeader
        title={translate("projects:newProjectWizard.wizardPage_title_newProject")}
        description={translate("projects:newProjectWizard.wizardPage_description_createCountTranslationProject")}
      />
      {renderStepIndicator()}
      <div className="wizard-content">
        <div className="wizard-step-summary">
          <div className="wizard-step-summary__top">
            <span>{translate("projects:newProjectWizard.wizardStepSummaryTop_message_text", { value: currentStep + 1, count: STEPS.length })}</span>
            <strong>{translate(STEPS[currentStep])}</strong>
          </div>
          <div className="wizard-step-summary__bar">
            <span style={{ width: `${stepProgress}%` }} />
          </div>
        </div>
        <div key={currentStep} className={`wizard-step-stage wizard-step-stage--${stepDirection}`}>
          {stepRenderers[currentStep]()}
        </div>
        {feedback && <InlineFeedback className={feedback.type === 'success' ? 'inline-alert--floating' : undefined} tone={feedback.type === 'error' ? 'error' : feedback.type === 'success' ? 'success' : 'info'} title={feedback.message} />}
      </div>
      <div className="wizard-nav">
        <Button variant="secondary" onClick={handlePrevStep} disabled={currentStep === 0}>{translate("projects:newProjectWizard.wizardNav_message_previous")}</Button>
        {currentStep < STEPS.length - 1 ? (
          <Button onClick={() => void handleNextStep()} disabled={!canNext || advancing}>
            {advancing ? translate("projects:newProjectWizard.wizardNav_message_processing") : nextLabel}
          </Button>
        ) : (
          <Button onClick={handleFinish}>{translate("projects:newProjectWizard.wizardNav_message_completeOpenProject")}</Button>
        )}
      </div>
    </div>
  );
}

function describeFileDetection(detection: FilePluginDetection | null, plugins: PluginInfo[]) {
  if (!detection || (Object.keys(detection.counts).length === 0 && detection.unknown.length === 0)) {
    return translate("projects:newProjectWizard.describeFileDetection_message_sourceFileFormatAutoCountFileSelect");
  }
  const label = (name: string) => localizePlugin(plugins.find((p) => p.name === name))?.display_name || name;
  const parts = Object.entries(detection.counts).map(([name, n]) => `${label(name)} ×${n}`);
  let text = translate("projects:newProjectWizard.text_message_doneGtInput", { value: parts.join('、') || translate("common:actions.none") });
  if (detection.unknown.length > 0) {
    text += translate("projects:newProjectWizard.describeFileDetection_message_countFileUnable", { count: detection.unknown.length });
  }
  if (detection.suggested === 'auto') {
    text += translate("projects:newProjectWizard.describeFileDetection_message_detectFormatDoneSelectAutoCountFile");
  } else if (detection.suggested) {
    text += translate("projects:newProjectWizard.describeFileDetection_message_doneAutoSelect", { value: label(detection.suggested) });
  }
  return text;
}
