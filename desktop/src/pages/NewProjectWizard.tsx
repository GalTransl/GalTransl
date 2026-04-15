import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '../components/Button';
import { Panel } from '../components/Panel';
import { PageHeader } from '../components/PageHeader';
import { InlineFeedback } from '../components/page-state';
import {
  type PluginInfo,
  fetchBackendProfiles,
  fetchPlugins,
  fetchProjectConfig,
  updateProjectConfig,
  submitJob,
  fetchJob,
  encodeProjectDir,
} from '../lib/api';
import { addProjectToHistory } from './HomePage';

const STEPS = ['项目位置', '导入文件', '翻译后端', '常用设置', '提取人名'];

const DEFAULT_CONFIG_YAML = `common:
  workersPerProject: 16
  language: "zh-cn"
  gpt:
    numPerRequestTranslate: 10
    contextNum: 8
  plugin:
    filePlugin: "file_galtransl_json"
    textPlugins: []
`;

export function NewProjectWizard() {
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);

  // Step 1 state
  const [parentDir, setParentDir] = useState('');
  const [projectName, setProjectName] = useState('');
  const [projectCreated, setProjectCreated] = useState(false);

  // Step 2 state
  const [importedFiles, setImportedFiles] = useState<string[]>([]);

  // Step 3 state
  const [backendProfileNames, setBackendProfileNames] = useState<string[]>([]);
  const [selectedBackend, setSelectedBackend] = useState('');

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
      await invoke('create_dir', { path: projectDir });
      await invoke('create_dir', { path: `${projectDir}${sep}gt_input` });
      await invoke('create_dir', { path: `${projectDir}${sep}gt_output` });
      await invoke('create_dir', { path: `${projectDir}${sep}transl_cache` });
      await invoke('write_text_file', { path: `${projectDir}${sep}config.yaml`, content: DEFAULT_CONFIG_YAML });
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
      if (!gtInputDir) return;
      const files = Array.from(e.dataTransfer.files);
      if (files.length === 0) return;
      try {
        // In Tauri, file.path is available for drag-and-dropped files
        const paths = files.map((f) => (f as File & { path?: string }).path || f.name);
        await invoke('copy_files', { sources: paths, destinationDir: gtInputDir });
        const names = files.map((f) => f.name);
        setImportedFiles((prev) => [...prev, ...names]);
        setFeedback({ type: 'success', message: `已导入 ${files.length} 个文件` });
      } catch (err) {
        setFeedback({ type: 'error', message: `导入失败: ${err instanceof Error ? err.message : String(err)}` });
      }
    },
    [gtInputDir],
  );

  const handleFilePick = useCallback(async () => {
    if (!gtInputDir) return;
    const selected = await open({ multiple: true });
    if (!selected) return;
    const paths = Array.isArray(selected) ? selected : [selected];
    try {
      await invoke('copy_files', { sources: paths, destinationDir: gtInputDir });
      const names = paths.map((p: string) => p.split(/[/\\]/).pop() || p);
      setImportedFiles((prev) => [...prev, ...names]);
      setFeedback({ type: 'success', message: `已导入 ${paths.length} 个文件` });
    } catch (err) {
      setFeedback({ type: 'error', message: `导入失败: ${err instanceof Error ? err.message : String(err)}` });
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

      const gpt = { ...((common.gpt as Record<string, unknown>) || {}) };
      gpt.numPerRequestTranslate = numPerRequest;
      common.gpt = gpt;

      const plugin = { ...((common.plugin as Record<string, unknown>) || {}) };
      plugin.filePlugin = selectedFilePlugin;
      common.plugin = plugin;

      config.common = common;

      await updateProjectConfig(projectId, { config, config_file_name: 'config.yaml' });

      // Save backend profile selection
      if (selectedBackend) {
        const { setSelectedBackendProfile } = await import('../lib/api');
        setSelectedBackendProfile(projectDir, selectedBackend);
      }

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
    addProjectToHistory(projectDir, 'config.yaml');
    const projectId = encodeProjectDir(projectDir);
    navigate(`/project/${projectId}/translate`);
  }, [projectDir, navigate]);

  const canNext = useMemo(() => {
    if (currentStep === 0) return projectCreated;
    if (currentStep === 1) return true; // file import is optional
    if (currentStep === 2) return true; // backend selection is optional
    if (currentStep === 3) return settingsSaved;
    return false;
  }, [currentStep, projectCreated, settingsSaved]);

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
          <Button variant="secondary" onClick={() => void handleSelectParentDir()}>浏览</Button>
        </div>
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
        onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add('drop-zone--over'); }}
        onDragLeave={(e) => { e.currentTarget.classList.remove('drop-zone--over'); }}
        onDrop={(e) => void handleFileDrop(e)}
      >
        <div className="drop-zone__icon">📁</div>
        <div className="drop-zone__text">拖放文件到此处导入</div>
      </div>
      <div className="wizard-actions">
        <Button variant="secondary" onClick={() => void handleFilePick()}>选择文件</Button>
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
          <option value="">-- 不使用 --</option>
          {backendProfileNames.map((name) => (
            <option key={name} value={name}>{name}</option>
          ))}
        </select>
        <span className="field__hint">如需添加新配置，请前往翻译后端配置页面</span>
      </div>
    </Panel>
  );

  // ── Step 4 ──
  const renderStep4 = () => (
    <Panel title="常用设置" description="设置项目的基本翻译参数。">
      <div className="field">
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
      </div>
      <div className="field">
        <span className="field__label">目标语言</span>
        <select className="field__select" value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="zh-cn">简体中文</option>
          <option value="zh-tw">繁体中文</option>
          <option value="en">English</option>
          <option value="ja">日本語</option>
          <option value="ko">한국어</option>
        </select>
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

  return (
    <div className="wizard-page">
      <PageHeader
        title="新建项目"
        description="按照向导创建一个新的翻译项目。"
      />
      {renderStepIndicator()}
      <div className="wizard-content">
        {stepRenderers[currentStep]()}
        {feedback && <InlineFeedback tone={feedback.type === 'error' ? 'error' : feedback.type === 'success' ? 'success' : 'info'} title={feedback.message} />}
      </div>
      <div className="wizard-nav">
        <Button variant="secondary" onClick={() => setCurrentStep((s) => s - 1)} disabled={currentStep === 0}>
          上一步
        </Button>
        {currentStep < 4 ? (
          <Button onClick={() => setCurrentStep((s) => s + 1)} disabled={!canNext}>
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
