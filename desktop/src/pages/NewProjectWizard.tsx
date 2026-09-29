import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
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

const STEPS = ['项目位置', '导入文件', '翻译后端', '常用设置', '提取人名', '完成'];
const LAST_PARENT_DIR_KEY = 'galtransl-new-project-last-parent-dir';

// 最后一步的后续流程指引：从生成字典到取回译文的正经顺序
const FLOW_STEPS: { title: string; description: string }[] = [
  {
    title: '先生成 GPT 字典',
    description: '在项目的「项目字典」里点「AI生成GPT字典」：GenDic 读原文提取人名、地名与专有名词并统一译名（结果并入项目GPT字典-生成.txt）。',
  },
  {
    title: '检查字典',
    description: '核对译名、删掉不该收的普通词。字典按类目分区、可在卡片里直接改，改完记得保存。',
  },
  {
    title: '启动翻译',
    description: '回到「开始翻译」选好模板启动。正式全量前建议先试译一两个文件，确认文风与术语没问题。',
  },
  {
    title: '查看结果与翻译问题',
    description: '在「浏览文本」里看译文和检测出的问题句（残留日文、缺控制符、比日文长等），据此补字典或改译文。',
  },
  {
    title: '构建输出',
    description: '改完字典后点「构建输出」（rebuilda）用字典重刷缓存与结果，最终译文在 gt_output 文件夹取回。',
  },
];

type NewProjectWizardProps = {
  onOpenProject: (projectDir: string, config: string) => void;
};

export function NewProjectWizard({ onOpenProject }: NewProjectWizardProps) {
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [stepDirection, setStepDirection] = useState<'forward' | 'backward'>('forward');
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);

  // Step 1 state
  const [parentDir, setParentDir] = useState(() => {
    try {
      return localStorage.getItem(LAST_PARENT_DIR_KEY) || '';
    } catch {
      return '';
    }
  });
  const [projectName, setProjectName] = useState('');
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

  // Step 5 state
  const [nameJobStatus, setNameJobStatus] = useState<'idle' | 'running' | 'completed' | 'failed'>('idle');
  const [nameJobMessage, setNameJobMessage] = useState('');

  const projectDir = useMemo(() => {
    if (!parentDir || !projectName) return '';
    const sep = parentDir.includes('/') ? '/' : '\\';
    return `${parentDir}${sep}${projectName}`;
  }, [parentDir, projectName]);

  const gtInputDir = useMemo(() => {
    if (!projectDir) return '';
    const sep = projectDir.includes('/') ? '/' : '\\';
    return `${projectDir}${sep}gt_input`;
  }, [projectDir]);

  const importPathsToInput = useCallback(
    async (paths: string[]) => {
      if (!gtInputDir || paths.length === 0) return;

      const existingNames = new Set(importedFiles.map((name) => name.toLowerCase()));
      const namesInBatch = new Set<string>();
      const pathsToImport: string[] = [];
      const acceptedNames: string[] = [];

      for (const p of paths) {
        const name = p.split(/[/\\]/).pop() || p;
        const key = name.toLowerCase();
        if (existingNames.has(key) || namesInBatch.has(key)) {
          continue;
        }
        namesInBatch.add(key);
        pathsToImport.push(p);
        acceptedNames.push(name);
      }

      if (pathsToImport.length === 0) {
        setFeedback({ type: 'info', message: '已过滤重复文件，本次无新增导入。' });
        return;
      }

      try {
        await invoke('copy_files', { sources: pathsToImport, destinationDir: gtInputDir });
        setImportedFiles((prev) => [...prev, ...acceptedNames]);
        const filteredCount = paths.length - pathsToImport.length;
        setFeedback({
          type: 'success',
          message: filteredCount > 0
            ? `已导入 ${pathsToImport.length} 个文件，已过滤 ${filteredCount} 个重复文件`
            : `已导入 ${pathsToImport.length} 个文件`,
        });
      } catch (err) {
        setFeedback({ type: 'error', message: `导入失败: ${err instanceof Error ? err.message : String(err)}` });
      }
    },
    [gtInputDir, importedFiles],
  );

  useEffect(() => {
    const currentWindow = getCurrentWebviewWindow();
    let disposed = false;

    const unlistenPromise = currentWindow.onDragDropEvent((event: unknown) => {
      if (currentStep !== 1) return;
      const payload = (event as { payload?: { type?: string; paths?: string[] } })?.payload;
      if (payload?.type !== 'drop') return;
      const paths = Array.isArray(payload.paths) ? payload.paths : [];
      if (paths.length === 0) {
        setFeedback({ type: 'error', message: '未能读取拖拽文件路径，请改用“选择文件”导入。' });
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
  }, [currentStep, importPathsToInput]);

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
    if (currentStep !== 1 || !projectDir) return;
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
  }, [currentStep, projectDir]);

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
    if (selected) {
      // Normalize to backslash on Windows
      const path = typeof selected === 'string' ? selected.replace(/\//g, '\\') : selected;
      setParentDir(path);
      setParentDirTouched(true);
    }
  }, []);

  const handleCreateProject = useCallback(async (): Promise<boolean> => {
    if (!projectDir) {
      setFeedback({ type: 'error', message: '请选择目录并输入项目名称' });
      return false;
    }
    // 目标目录里已有 config.yaml 时不覆盖，避免把已有项目的配置冲掉
    const alreadyExists = await fetchProjectConfig(encodeProjectDir(projectDir), 'config.yaml')
      .then(() => true)
      .catch(() => false);
    if (alreadyExists) {
      setFeedback({ type: 'error', message: '该目录下已存在 config.yaml，请换一个项目名称，或回到首页用「打开项目」打开它。' });
      return false;
    }
    try {
      const sep = projectDir.includes('/') ? '/' : '\\';
      const configYaml = await fetchDefaultProjectConfigTemplate();
      await invoke('create_dir', { path: projectDir });
      await invoke('create_dir', { path: `${projectDir}${sep}gt_input` });
      await invoke('create_dir', { path: `${projectDir}${sep}gt_output` });
      await invoke('create_dir', { path: `${projectDir}${sep}transl_cache` });
      await invoke('write_text_file', { path: `${projectDir}${sep}config.yaml`, content: configYaml });
      setProjectCreated(true);
      // 先记入历史：中途离开向导（比如去模型设置）也能从首页找回这个项目
      addProjectToHistory(projectDir, 'config.yaml');
      setFeedback({ type: 'success', message: '项目创建成功！' });
      return true;
    } catch (err) {
      setFeedback({ type: 'error', message: `创建失败: ${err instanceof Error ? err.message : String(err)}` });
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
          .map((line) => {
            try {
              if (line.startsWith('file://')) {
                const url = new URL(line);
                const decoded = decodeURIComponent(url.pathname || '');
                const normalized = /^\/[A-Za-z]:/.test(decoded) ? decoded.slice(1) : decoded;
                return normalized.replace(/\//g, '\\');
              }
              return decodeURIComponent(line).replace(/\//g, '\\');
            } catch {
              return line.replace(/\//g, '\\');
            }
          })
          .filter((p) => /^[A-Za-z]:\\/.test(p) || p.startsWith('\\\\'));
      };

      const droppedPaths = directPaths.length > 0 ? directPaths : parseDroppedUriList();
      if (droppedPaths.length === 0) {
        setFeedback({ type: 'error', message: '未能读取拖拽文件路径，请改用“选择文件”导入。' });
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
      setFeedback({ type: 'error', message: `打开输入文件夹失败: ${err instanceof Error ? err.message : String(err)}` });
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
    fetchTranslationGuidelines()
      .then((list) => {
        setGuidelines(list);
        setTranslationGuideline((prev) => {
          if (prev) return prev;
          // 默认挑「日译中_增强v2」：先把首选、再退到上一代增强、最后才退到列表首位。
          // 名字要带 .md——接口给的是文件名，少写扩展名会一个都匹配不上，静默落到 list[0]
          // （按 Unicode 排序多半是 Basic.md），看起来就像"默认值没生效"。
          for (const preferred of ['日译中_增强v2.md', '日译中_增强.md']) {
            if (list.includes(preferred)) return preferred;
          }
          return list[0] || '';
        });
      })
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
      setFeedback({ type: 'success', message: '设置已保存' });
      return true;
    } catch (err) {
      setFeedback({ type: 'error', message: `保存失败: ${err instanceof Error ? err.message : String(err)}` });
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
          setNameJobMessage('gt_input 中没有文件，已跳过人名提取。可返回上一步导入文件，或稍后手动添加。');
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
              setNameJobMessage(status.success ? '人名提取完成！' : `提取完成但有警告: ${status.error || ''}`);
            } else if (status.status === 'failed') {
              setNameJobStatus('failed');
              setNameJobMessage(status.error || '提取失败');
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
          <span className="wizard-step__label">{label}</span>
        </li>
      ))}
    </ul>
  );

  // ── Step 1 ──
  const renderStep1 = () => (
    <Panel title="项目位置" description="选择项目文件夹的保存位置和项目名称，然后创建项目结构。">
      <div className="wizard-form-grid">
        <div className="field">
          <span className="field__label">项目名称</span>
          <input
            className="field__input"
            autoComplete="off"
            value={projectName}
            onChange={(e) => { setProjectName(e.target.value); setProjectCreated(false); }}
            placeholder="例如：MyProject"
          />
        </div>
        <div className="field">
          <span className="field__label">父目录</span>
          <div className="field__row">
            <input
              className="field__input"
              autoComplete="off"
              value={parentDir}
              onChange={(e) => { setParentDir(e.target.value); setParentDirTouched(true); setProjectCreated(false); }}
              placeholder="例如：E:\GalTransl\projects"
            />
            <Button className="field__browse-button" variant="secondary" onClick={() => void handleSelectParentDir()}>
              浏览
            </Button>
          </div>
          <span className="field__hint">默认是程序所在目录；建议用英文路径，避免空格与特殊字符。</span>
        </div>
        <div className="wizard-path-preview">
          <span className="wizard-path-preview__label">将创建目录</span>
          <code className="wizard-path-preview__path">{projectDir || '请先填写父目录与项目名称'}</code>
          <div className="wizard-path-preview__meta">包含 `gt_input` / `gt_output` / `transl_cache` 与 `config.yaml`</div>
        </div>
      </div>
      {projectCreated ? (
        <div className="wizard-tip-card">
          <strong><Icon name="check" /> 项目已创建</strong>
          <span>点击「下一步」继续导入文件。</span>
        </div>
      ) : null}
    </Panel>
  );

  // ── Step 2 ──
  const renderStep2 = () => (
    <Panel title="导入文件" description="将待翻译的文件导入到项目的 gt_input 目录中，也可以跳过此步骤稍后手动添加。">
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
              <strong className="drop-zone__text">已导入 {importedFiles.length} 个文件</strong>
              <span>可继续拖放文件到此处添加</span>
            </div>
            <ul className="wizard-file-list" aria-label="已导入文件">
              {importedFiles.map((file) => (
                <li key={file} className="wizard-file-list__item">{file}</li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <div className="drop-zone__icon"><Icon name="folder" /></div>
            <div className="drop-zone__text">拖放文件到此处导入</div>
          </>
        )}
      </div>
      <div className="wizard-actions">
        <Button variant="secondary" onClick={() => void handleFilePick()}>选择文件</Button>
        <Button variant="secondary" onClick={() => void handleOpenInputFolder()} disabled={!gtInputDir}>打开输入文件夹</Button>
      </div>
      <div className="wizard-tip-card">
        <strong>支持的文件类型</strong>
        <span>文本与电子书：TXT、EPUB；字幕：SRT、LRC、VTT。</span>
        <span>翻译数据：GalTransl / Mtool JSON、Translator++ XLSX。</span>
        <span>Galgame 脚本直接提取：.ks、.scn、.ast、.asb、bgi、.cst、.cstl、.srcxml、.csx、.rld、.hcb、.soc、.tjs、.pbd、.sc、.s、.src、.ws2、.ybn。</span>
        <span>部分脚本（如 .bin、.mes、.txt）需在文件插件设置中指定对应引擎。</span>
        <span>无后缀的 BGI 脚本支持按文件头识别；若未识别，可在「常用设置」选择 msg-tool，并将脚本引擎设为 bgi。</span>
        <span>支持拖拽多个文件；若暂时跳过，可后续手动复制到 <code>gt_input</code> 目录。</span>
      </div>
      <div className="wizard-tip-card">
        <strong>Galgame 脚本兼容性提示</strong>
        <span>游戏脚本格式多变，自动提取不一定兼容所有游戏。建议导入并完成项目创建后，在「浏览文本」中确认文本与人名是否正确、是否有遗漏，再开始翻译。</span>
      </div>
    </Panel>
  );

  // ── Step 3 ──
  const renderStep3 = () => (
    <Panel title="翻译后端" description="选择翻译后端配置，也可以跳过此步骤在配置编辑中设置。">
      <div className="field">
        <span className="field__label">后端配置</span>
        <CustomSelect value={selectedBackend} onChange={(e) => setSelectedBackend(e.target.value)}>
          <option value="__default__">跟随全局默认</option>
          <option value="">不使用（使用项目自身配置）</option>
          {backendProfileNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </CustomSelect>
        <span className="field__hint">
          {selectedBackend === '__default__'
            ? defaultBackendName
              ? `当前默认配置为「${defaultBackendName}」，可在「模型设置」页面修改`
              : '尚未设置默认配置，请在「模型设置」页面设置'
            : selectedBackend
              ? `翻译时将使用全局配置「${selectedBackend}」覆盖项目后端设置`
              : '将忽略全局配置，使用项目自身后端设置'}
        </span>
      </div>
      {backendProfileNames.length === 0 ? (
        <div className="wizard-tip-card wizard-tip-card--warning">
          <strong><Icon name="warning" /> 还没有任何模型配置</strong>
          <span>
            没有模型就无法翻译。可以先继续完成向导，之后在「模型设置」中新建配置（第一个配置会自动设为默认）；
            项目已保存在首页的历史项目中，随时可以回来。
          </span>
          <div>
            <Button variant="secondary" onClick={() => navigate('/backend-profiles')}>前往模型设置</Button>
          </div>
        </div>
      ) : (
        <div className="wizard-tip-card">
          <strong>推荐策略</strong>
          <span>一般保持「跟随全局默认」即可；需要为这个项目单独换模型时再选择具体配置。</span>
        </div>
      )}
    </Panel>
  );

  // ── Step 4 ──
  const renderStep4 = () => (
    <Panel title="常用设置" description="设置项目的基本翻译参数。">
      <div className="wizard-settings-grid">
      <div className="field wizard-settings-grid__full">
        <span className="field__label">文件插件</span>
        <CustomSelect
          value={selectedFilePlugin}
          onChange={(e) => {
            filePluginTouchedRef.current = true;
            setSelectedFilePlugin(e.target.value);
          }}
        >
          <option value="auto">自动识别 (auto)</option>
          {filePlugins.length > 0 ? (
            filePlugins.map((p) => (
              <option key={p.name} value={p.name}>{p.display_name} ({p.name})</option>
            ))
          ) : selectedFilePlugin !== 'auto' ? (
            <option value={selectedFilePlugin}>{selectedFilePlugin}</option>
          ) : null}
        </CustomSelect>
        {filePlugins.filter((p) => p.name === selectedFilePlugin && p.description).map((plugin) => (
          <span key={plugin.name} className="field__hint" style={{ whiteSpace: 'pre-line' }}>{plugin.description}</span>
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
        <span className="field__label">并发文件数</span>
        <input
          className="field__input"
          type="number"
          min={1}
          value={workersPerProject}
          onChange={(e) => setWorkersPerProject(Number(e.target.value))}
        />
        <span className="field__hint">并发越高速度越快，但更吃资源。</span>
      </div>
      <div className="field">
        <span className="field__label">单次翻译句数</span>
        <input
          className="field__input"
          type="number"
          min={1}
          value={numPerRequest}
          onChange={(e) => setNumPerRequest(Number(e.target.value))}
        />
        <span className="field__hint">建议 8~20，兼顾质量和成本。</span>
      </div>
      <div className="field">
        <span className="field__label">动态句数调整</span>
        <CustomSelect value={String(dynamicNumPerRequest)} onChange={(e) => setDynamicNumPerRequest(e.target.value === 'true')}>
          <option value="false">关闭</option>
          <option value="true">开启</option>
        </CustomSelect>
        <span className="field__hint">根据解析错误自动降低句数，稳定后逐步提升。</span>
      </div>
      {/* 关掉动态句数调整后，上下限没人用，收起来免得占地方、也免得误以为在生效 */}
      {dynamicNumPerRequest ? (
        <>
          <div className="field">
            <span className="field__label">动态最小句数</span>
            <input
              className="field__input"
              type="number"
              min={1}
              value={dynamicNumPerRequestMin}
              onChange={(e) => setDynamicNumPerRequestMin(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <span className="field__label">动态最大句数</span>
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
        <span className="field__label">目标语言</span>
        <CustomSelect value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="zh-cn">简体中文</option>
          <option value="zh-tw">繁体中文</option>
          <option value="en">English</option>
          <option value="ja">日本語</option>
          <option value="ko">한국어</option>
        </CustomSelect>
      </div>
      <div className="field wizard-settings-grid__full">
        <span className="field__label">翻译规范</span>
        <CustomSelect
          value={translationGuideline}
          onChange={(e) => setTranslationGuideline(e.target.value)}
        >
          {guidelines.length === 0 && translationGuideline === '' ? (
            <option value="">（未找到翻译规范文件）</option>
          ) : null}
          {translationGuideline && !guidelines.includes(translationGuideline) ? (
            <option value={translationGuideline}>{translationGuideline}</option>
          ) : null}
          {guidelines.map((g) => (
            <option key={g} value={g}>{g}</option>
          ))}
        </CustomSelect>
        <span className="field__hint">选择使用的翻译规范文件（位于 translation_guidelines 文件夹），高端模型日译中推荐"增强"规范</span>
      </div>
      </div>
    </Panel>
  );

  // ── Step 5 ──
  const renderStep5 = () => (
    <Panel title="提取人名" description="自动从项目文件中提取人名表。">
      {nameJobStatus === 'running' && (
        <div className="wizard-progress">
          <div className="wizard-progress__bar">
            <div className="wizard-progress__fill" />
          </div>
          <div className="wizard-progress__text">正在提取人名...</div>
        </div>
      )}
      {nameJobStatus === 'completed' && (
        <div className="wizard-message wizard-message--success">
          {nameJobMessage}
          <br />
          <span className="wizard-message__hint">可在项目的「人名翻译」菜单中使用 AI 翻译人名。</span>
        </div>
      )}
      {nameJobStatus === 'failed' && (
        <div className="wizard-message wizard-message--error">
          提取失败: {nameJobMessage}
        </div>
      )}
    </Panel>
  );

  // ── Step 6: 完成（后续翻译流程指引） ──
  const renderStep6 = () => (
    <Panel title="项目已就绪，接下来这样走" description="按这个顺序走完一轮翻译；每一步都能在项目左侧菜单里随时进去，中途改字典不用重建项目。">
      <ol className="wizard-flow">
        {FLOW_STEPS.map((step, index) => (
          <li key={step.title} className="wizard-flow__item">
            <span className="wizard-flow__index" aria-hidden="true">{index + 1}</span>
            <span className="wizard-flow__body">
              <strong>{step.title}</strong>
              <span>{step.description}</span>
            </span>
          </li>
        ))}
      </ol>
      <div className="wizard-tip-card">
        <strong>提示</strong>
        <span>点「完成并打开项目」会打开「开始翻译」；想先把名字定下来，也可以先去「人名翻译」用 AI 译人名。</span>
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
    ? '创建项目并继续'
    : currentStep === 3
      ? '保存设置并继续'
      : '下一步';

  return (
    <div className="wizard-page">
      <PageHeader
        title="新建项目"
        description="按照向导创建一个新的翻译项目。"
      />
      {renderStepIndicator()}
      <div className="wizard-content">
        <div className="wizard-step-summary">
          <div className="wizard-step-summary__top">
            <span>第 {currentStep + 1} / {STEPS.length} 步</span>
            <strong>{STEPS[currentStep]}</strong>
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
        <Button variant="secondary" onClick={handlePrevStep} disabled={currentStep === 0}>
          上一步
        </Button>
        {currentStep < STEPS.length - 1 ? (
          <Button onClick={() => void handleNextStep()} disabled={!canNext || advancing}>
            {advancing ? '处理中…' : nextLabel}
          </Button>
        ) : (
          <Button onClick={handleFinish}>
            完成并打开项目
          </Button>
        )}
      </div>
    </div>
  );
}

function describeFileDetection(detection: FilePluginDetection | null, plugins: PluginInfo[]) {
  if (!detection || (Object.keys(detection.counts).length === 0 && detection.unknown.length === 0)) {
    return '用于识别与解析源文件格式；选「自动识别」会按每个文件的类型分别选择插件。';
  }
  const label = (name: string) => plugins.find((p) => p.name === name)?.display_name || name;
  const parts = Object.entries(detection.counts).map(([name, n]) => `${label(name)} ×${n}`);
  let text = `已识别 gt_input：${parts.join('、') || '无'}`;
  if (detection.unknown.length > 0) {
    text += `；${detection.unknown.length} 个文件无法识别（将被跳过）`;
  }
  if (detection.suggested === 'auto') {
    text += '。检测到多种格式，已选择「自动识别」，每个文件使用各自的插件。';
  } else if (detection.suggested) {
    text += `。已自动选择「${label(detection.suggested)}」。`;
  }
  return text;
}
