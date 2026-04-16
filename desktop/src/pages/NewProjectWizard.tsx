import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import { PageHeader } from '../components/PageHeader';
import { InlineFeedback } from '../components/page-state';
import {
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  type PluginInfo,
  getDefaultBackendProfile,
  fetchBackendProfiles,
  fetchPlugins,
  fetchDefaultProjectConfigTemplate,
  fetchProjectConfig,
  updateProjectConfig,
  submitJob,
  fetchJob,
  encodeProjectDir,
} from '../lib/api';
import { addProjectToHistory } from './HomePage';

const STEPS = ['项目位置', '导入文件', '翻译后端', '常用设置', '提取人名'];
const LAST_PARENT_DIR_KEY = 'galtransl-new-project-last-parent-dir';

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

  // Step 2 state
  const [importedFiles, setImportedFiles] = useState<string[]>([]);

  // Step 3 state
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedBackend, setSelectedBackend] = useState('__default__');
  const [defaultBackendName, setDefaultBackendName] = useState(() => getDefaultBackendProfile());

  // Step 4 state
  const [filePlugins, setFilePlugins] = useState<PluginInfo[]>([]);
  const [selectedFilePlugin, setSelectedFilePlugin] = useState('file_galtransl_json');
  const [workersPerProject, setWorkersPerProject] = useState(16);
  const [numPerRequest, setNumPerRequest] = useState(10);
  const [language, setLanguage] = useState('zh-cn');
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
      if (parentDir.trim()) {
        localStorage.setItem(LAST_PARENT_DIR_KEY, parentDir);
      }
    } catch {
      // ignore storage errors
    }
  }, [parentDir]);

  // ── Step 1: Create project ──
  const handleSelectParentDir = useCallback(async () => {
    const selected = await open({ directory: true });
    if (selected) {
      // Normalize to backslash on Windows
      const path = typeof selected === 'string' ? selected.replace(/\//g, '\\') : selected;
      setParentDir(path);
    }
  }, []);

  const handleCreateProject = useCallback(async () => {
    if (!projectDir) {
      setFeedback({ type: 'error', message: '请选择目录并输入项目名称' });
      return;
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
      setFeedback({ type: 'success', message: '项目创建成功！' });
    } catch (err) {
      setFeedback({ type: 'error', message: `创建失败: ${err instanceof Error ? err.message : String(err)}` });
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
    fetchBackendProfiles()
      .then((res) => {
        const names = Object.keys(res.profiles);
        setBackendProfileNames(names);
      })
      .catch(() => {});
  }, [currentStep]);

  useEffect(() => {
    const onDefaultBackendChange = () => {
      setDefaultBackendName(getDefaultBackendProfile());
    };
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onDefaultBackendChange);
    return () => window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onDefaultBackendChange);
  }, []);

  // ── Step 4: Load plugins on entry ──
  useEffect(() => {
    if (currentStep !== 3) return;
    fetchPlugins()
      .then((plugins) => {
        setFilePlugins(plugins.filter((p) => p.type === 'file'));
      })
      .catch(() => {});
  }, [currentStep]);

  const handleSaveSettings = useCallback(async () => {
    if (!projectDir) return;
    try {
      const projectId = encodeProjectDir(projectDir);
      const res = await fetchProjectConfig(projectId, 'config.yaml');
      const config = { ...res.config };

      // Update common settings
      const common = { ...((config.common as Record<string, unknown>) || {}) };
      common.workersPerProject = workersPerProject;
      common.language = language;

      common['gpt.numPerRequestTranslate'] = numPerRequest;
      common['gpt.contextNum'] = 8;

      config.common = common;

      const plugin: Record<string, unknown> = {
        ...((config.plugin as Record<string, unknown>) || {}),
        filePlugin: selectedFilePlugin,
      };
      if (!Array.isArray(plugin.textPlugins)) {
        plugin.textPlugins = [];
      }
      config.plugin = plugin;

      await updateProjectConfig(projectId, { config, config_file_name: 'config.yaml' });

      // Save backend profile selection
      const { setSelectedBackendProfile } = await import('../lib/api');
      setSelectedBackendProfile(projectDir, selectedBackend);

      setSettingsSaved(true);
      setFeedback({ type: 'success', message: '设置已保存' });
    } catch (err) {
      setFeedback({ type: 'error', message: `保存失败: ${err instanceof Error ? err.message : String(err)}` });
    }
  }, [projectDir, workersPerProject, language, numPerRequest, selectedFilePlugin, selectedBackend]);

  // ── Step 5: Auto-extract names on entry ──
  useEffect(() => {
    if (currentStep !== 4 || nameJobStatus !== 'idle' || !projectDir) return;

    const run = async () => {
      try {
        setNameJobStatus('running');
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

  const canNext = useMemo(() => {
    if (currentStep === 0) return projectCreated;
    if (currentStep === 1) return true; // file import is optional
    if (currentStep === 2) return true; // backend selection is optional
    if (currentStep === 3) return settingsSaved;
    return false;
  }, [currentStep, projectCreated, settingsSaved]);

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
          <span className="wizard-step__number">{i < currentStep ? '✓' : i + 1}</span>
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
          <span className="field__label">父目录</span>
          <div className="field__row">
            <input
              className="field__input"
              autoComplete="off"
              value={parentDir}
              onChange={(e) => { setParentDir(e.target.value); setProjectCreated(false); }}
              placeholder="例如：E:\GalTransl\projects"
            />
            <Button className="field__browse-button" variant="secondary" onClick={() => void handleSelectParentDir()}>
              浏览
            </Button>
          </div>
          <span className="field__hint">建议选择英文路径，避免空格与特殊字符。</span>
        </div>
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
        <div className="wizard-path-preview">
          <span className="wizard-path-preview__label">将创建目录</span>
          <code className="wizard-path-preview__path">{projectDir || '请先填写父目录与项目名称'}</code>
          <div className="wizard-path-preview__meta">包含 `gt_input` / `gt_output` / `transl_cache` 与 `config.yaml`</div>
        </div>
      </div>
      <div className="wizard-actions">
        <Button disabled={projectCreated || !parentDir || !projectName} onClick={() => void handleCreateProject()}>
          {projectCreated ? '已创建 ✓' : '创建项目'}
        </Button>
      </div>
    </Panel>
  );

  // ── Step 2 ──
  const renderStep2 = () => (
    <Panel title="导入文件" description="将待翻译的文件导入到项目的 gt_input 目录中，也可以跳过此步骤稍后手动添加。">
      <div
        className="drop-zone"
        onDragOver={(e) => {
          e.preventDefault();
          e.currentTarget.classList.add('drop-zone--over');
        }}
        onDragLeave={(e) => { e.currentTarget.classList.remove('drop-zone--over'); }}
        onDrop={(e) => void handleFileDrop(e)}
      >
        <div className="drop-zone__icon">📁</div>
        <div className="drop-zone__text">拖放文件到此处导入</div>
      </div>
      <div className="wizard-actions">
        <Button variant="secondary" onClick={() => void handleFilePick()}>选择文件</Button>
        <Button variant="secondary" onClick={() => void handleOpenInputFolder()} disabled={!gtInputDir}>打开输入文件夹</Button>
      </div>
      <div className="wizard-tip-card">
        <strong>导入提示</strong>
        <span>支持拖拽多个文件；若暂时跳过，可后续手动复制到 `gt_input` 目录。</span>
      </div>
      {importedFiles.length > 0 && (
        <ul className="wizard-file-list">
          {importedFiles.map((f, i) => (
            <li key={i} className="wizard-file-list__item">{f}</li>
          ))}
        </ul>
      )}
    </Panel>
  );

  // ── Step 3 ──
  const renderStep3 = () => (
    <Panel title="翻译后端" description="选择翻译后端配置，也可以跳过此步骤在配置编辑中设置。">
      <div className="field">
        <span className="field__label">后端配置</span>
        <select className="field__select" value={selectedBackend} onChange={(e) => setSelectedBackend(e.target.value)}>
          <option value="__default__">跟随全局默认</option>
          <option value="">不使用（使用项目自身配置）</option>
          {backendProfileNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
        <span className="field__hint">
          {selectedBackend === '__default__'
            ? defaultBackendName
              ? `当前默认配置为「${defaultBackendName}」，可在「翻译后端配置」页面修改`
              : '尚未设置默认配置，请在「翻译后端配置」页面设置'
            : selectedBackend
              ? `翻译时将使用全局配置「${selectedBackend}」覆盖项目后端设置`
              : '将忽略全局配置，使用项目自身后端设置'}
        </span>
      </div>
      <div className="wizard-tip-card">
        <strong>推荐策略</strong>
        <span>新项目建议先使用稳定配置，后续可在项目内按章节切换不同后端。</span>
      </div>
    </Panel>
  );

  // ── Step 4 ──
  const renderStep4 = () => (
    <Panel title="常用设置" description="设置项目的基本翻译参数。">
      <div className="wizard-settings-grid">
      <div className="field wizard-settings-grid__full">
        <span className="field__label">文件插件</span>
        <select className="field__select" value={selectedFilePlugin} onChange={(e) => setSelectedFilePlugin(e.target.value)}>
          {filePlugins.length > 0 ? (
            filePlugins.map((p) => (
              <option key={p.name} value={p.name}>{p.display_name} ({p.name})</option>
            ))
          ) : (
            <option value={selectedFilePlugin}>{selectedFilePlugin}</option>
          )}
        </select>
        <span className="field__hint">用于识别与解析源文件格式。</span>
      </div>
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
      <div className="field wizard-settings-grid__full">
        <span className="field__label">目标语言</span>
        <select className="field__select" value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="zh-cn">简体中文</option>
          <option value="zh-tw">繁体中文</option>
          <option value="en">English</option>
          <option value="ja">日本語</option>
          <option value="ko">한국어</option>
        </select>
      </div>
      </div>
      <div className="wizard-actions">
        <Button disabled={settingsSaved} onClick={() => void handleSaveSettings()}>
          {settingsSaved ? '已保存 ✓' : '保存设置'}
        </Button>
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

  const stepRenderers = [renderStep1, renderStep2, renderStep3, renderStep4, renderStep5];

  const handlePrevStep = useCallback(() => {
    setStepDirection('backward');
    setCurrentStep((s) => Math.max(0, s - 1));
  }, []);

  const handleNextStep = useCallback(() => {
    setStepDirection('forward');
    setCurrentStep((s) => Math.min(STEPS.length - 1, s + 1));
  }, []);

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
        {feedback && <InlineFeedback tone={feedback.type === 'error' ? 'error' : feedback.type === 'success' ? 'success' : 'info'} title={feedback.message} />}
      </div>
      <div className="wizard-nav">
        <Button variant="secondary" onClick={handlePrevStep} disabled={currentStep === 0}>
          上一步
        </Button>
        {currentStep < 4 ? (
          <Button onClick={handleNextStep} disabled={!canNext}>
            下一步
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
