import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { InlineFeedback } from '../components/page-state';
import {
  encodeProjectDir,
  getBackendProfile,
  getBackendProfileNames,
  getDefaultBackendProfile,
  stopAgent,
  startAgent,
  subscribeAgentStream,
  fetchAgentStatus,
  type AgentEvent,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

const OPEN_PROJECTS_KEY = 'galtransl-open-projects';
const CONFIG_FILE_KEY = 'galtransl-config-file';
const HISTORY_KEY = 'galtransl-project-history';

type HistoryEntry = {
  projectDir: string;
  configFileName: string;
  lastOpened: string;
};

function readOpenProjects(): string[] {
  try {
    const raw = localStorage.getItem(OPEN_PROJECTS_KEY);
    const parsed = raw ? (JSON.parse(raw) as string[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function readHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? (JSON.parse(raw) as HistoryEntry[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function readConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
  }
}

function shortName(projectDir: string): string {
  return projectDir.replace(/[\\/]/g, '/').split('/').filter(Boolean).pop() || projectDir;
}

export function AgentPage() {
  const navigate = useNavigate();

  // 项目候选：已打开项目 + 历史，去重保序
  const projectOptions = useMemo(() => {
    const seen = new Set<string>();
    const list: string[] = [];
    for (const d of readOpenProjects()) {
      if (!seen.has(d)) {
        seen.add(d);
        list.push(d);
      }
    }
    for (const h of readHistory()) {
      if (!seen.has(h.projectDir)) {
        seen.add(h.projectDir);
        list.push(h.projectDir);
      }
    }
    return list;
  }, []);

  const [projectDir, setProjectDir] = useState<string>(() => projectOptions[0] || '');
  const [configFileName, setConfigFileName] = useState<string>(() =>
    projectOptions[0] ? readConfigFileName(projectOptions[0]) : 'config.yaml',
  );
  const [backendProfileNames] = useState<string[]>(() => getBackendProfileNames());
  const [backendProfileName, setBackendProfileName] = useState<string>(
    () => getDefaultBackendProfile() || getBackendProfileNames()[0] || '',
  );
  const [goal, setGoal] = useState('按标准流程完成本项目的翻译：先准备字典，再启动翻译，最后复核结果与问题。');

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<string>('idle');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resumeChecked, setResumeChecked] = useState(false);

  const abortRef = useRef<(() => void) | null>(null);
  const timelineRef = useRef<HTMLDivElement | null>(null);

  const effectiveProject = projectDir;
  const isTerminal = (s: string) => s === 'done' || s === 'stopped' || s === 'failed' || s === 'idle';

  // 首次进入：若该项目已有 agent 在运行（后端重启场景），恢复时间线
  useEffect(() => {
    let cancelled = false;
    setResumeChecked(false);
    if (!effectiveProject) {
      setResumeChecked(true);
      return;
    }
    fetchAgentStatus(effectiveProject)
      .then((snap) => {
        if (cancelled) return;
        if (snap.events && snap.events.length) {
          setEvents(snap.events);
        }
        setStatus(snap.status);
        setRunning(snap.status === 'running');
      })
      .catch(() => {
        // 忽略：后端未就绪等
      })
      .finally(() => {
        if (!cancelled) setResumeChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [effectiveProject]);

  // 切换项目时同步配置文件名
  useEffect(() => {
    if (projectDir) {
      setConfigFileName(readConfigFileName(projectDir));
    }
  }, [projectDir]);

  // 自动滚动时间线到底部
  useEffect(() => {
    if (timelineRef.current) {
      timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
    }
  }, [events]);

  // 卸载时断开 SSE
  useEffect(() => {
    return () => {
      abortRef.current?.();
      abortRef.current = null;
    };
  }, []);

  // 订阅某项目的 SSE 事件流
  const subscribeStream = useCallback(
    (dir: string) => {
      abortRef.current?.();
      abortRef.current = subscribeAgentStream(
        dir,
        (ev) => {
          if (ev.type === 'close') return;
          if (ev.type === 'status') {
            if (ev.status) setStatus(ev.status);
            if (ev.status && isTerminal(ev.status)) {
              setRunning(false);
              abortRef.current?.();
              abortRef.current = null;
            }
            return;
          }
          setEvents((prev) => [...prev, ev]);
          if (ev.type === 'finish' || ev.type === 'error' || ev.type === 'stopped') {
            setRunning(false);
          }
        },
        (err) => {
          setError(normalizeError(err, 'Agent 事件流中断'));
          setRunning(false);
        },
      );
    },
    [],
  );

  const handleStart = useCallback(async () => {
    setError(null);
    if (!effectiveProject) {
      setError('请先选择一个项目');
      return;
    }
    const profile = getBackendProfile(backendProfileName);
    if (!profile) {
      setError('请先选择一个翻译后端配置（并在「翻译后端配置」页填写 token/模型）');
      return;
    }
    setEvents([]);
    setStatus('running');
    setRunning(true);
    try {
      await startAgent({
        project_dir: effectiveProject,
        config_file_name: configFileName || 'config.yaml',
        backend_profile_data: profile,
        goal,
      });
      subscribeStream(effectiveProject);
    } catch (err) {
      setError(normalizeError(err, '启动 Agent 失败'));
      setRunning(false);
      setStatus('failed');
    }
  }, [effectiveProject, backendProfileName, configFileName, goal, subscribeStream]);

  const handleStop = useCallback(async () => {
    if (!effectiveProject) return;
    try {
      await stopAgent(effectiveProject);
      setStatus('stopped');
      setRunning(false);
      abortRef.current?.();
      abortRef.current = null;
    } catch (err) {
      setError(normalizeError(err, '停止 Agent 失败'));
    }
  }, [effectiveProject]);

  const handleOpenProject = useCallback(async () => {
    try {
      const selected = await openDialog({ directory: true, multiple: false });
      if (typeof selected === 'string' && selected) {
        setProjectDir(selected);
        setConfigFileName(readConfigFileName(selected));
      }
    } catch {
      // 用户取消
    }
  }, []);

  const stepCount = events.length;
  const terminal = isTerminal(status) && !running && resumeChecked;

  return (
    <div className="agent-page">
      <PageHeader
        title="🤖 Agent 模式"
        description={'选择项目与后端配置，Agent 会自主驱动“先写字典后启动翻译”的标准流程，并在下方实时展示每一步的思考、工具调用与结果。'}
        status={error ? <InlineFeedback tone="error" description={error} /> : null}
      />

      <div className="agent-page__setup">
        <Panel title="配置" description="选择要操作的项目与复用的翻译后端配置">
          <div className="agent-setup__grid">
            <label className="field">
              <span>项目</span>
              <div className="agent-setup__row">
                <select
                  value={projectDir}
                  onChange={(e) => setProjectDir(e.target.value)}
                  disabled={running}
                >
                  {projectOptions.length === 0 ? <option value="">（暂无，点击右侧打开）</option> : null}
                  {projectOptions.map((d) => (
                    <option key={d} value={d}>
                      {shortName(d)}
                    </option>
                  ))}
                  {projectDir && !projectOptions.includes(projectDir) ? (
                    <option value={projectDir}>{shortName(projectDir)}</option>
                  ) : null}
                </select>
                <Button variant="secondary" onClick={handleOpenProject} disabled={running}>
                  打开…
                </Button>
              </div>
            </label>

            <label className="field">
              <span>配置文件</span>
              <input
                type="text"
                value={configFileName}
                onChange={(e) => setConfigFileName(e.target.value)}
                disabled={running}
              />
            </label>

            <label className="field">
              <span>翻译后端配置</span>
              <select
                value={backendProfileName}
                onChange={(e) => setBackendProfileName(e.target.value)}
                disabled={running}
              >
                {backendProfileNames.length === 0 ? (
                  <option value="">（未配置，请在「翻译后端配置」页添加）</option>
                ) : (
                  backendProfileNames.map((n) => <option key={n} value={n}>{n}</option>)
                )}
              </select>
            </label>

            <label className="field agent-setup__goal">
              <span>目标</span>
              <textarea
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                rows={2}
                disabled={running}
              />
            </label>
          </div>

          <div className="form-actions">
            {running ? (
              <Button variant="secondary" onClick={handleStop}>停止</Button>
            ) : (
              <Button onClick={handleStart} disabled={!projectDir || !backendProfileName}>
                启动 Agent
              </Button>
            )}
            {projectDir && (
              <Button
                variant="secondary"
                onClick={() => navigate(`/project/${encodeProjectDir(projectDir)}/translate`)}
              >
                在工作台查看
              </Button>
            )}
          </div>
        </Panel>
      </div>

      <div className="agent-page__main">
        <div className="agent-page__timeline" ref={timelineRef}>
          {events.length === 0 ? (
            <div className="agent-empty">
              {terminal && status !== 'idle'
                ? `Agent 已结束（${status}）`
                : '启动 Agent 后，这里会实时显示它的每一步思考、工具调用与结果。'}
            </div>
          ) : (
            events.map((ev, idx) => <AgentEventCard key={idx} ev={ev} />)
          )}
        </div>

        <aside className="agent-page__status">
          <Panel title="运行状态">
            <div className="agent-status__grid">
              <div className="agent-status__item">
                <span className="agent-status__label">状态</span>
                <span className={`agent-status__value agent-status__value--${status}`}>
                  {statusLabel(status, running)}
                </span>
              </div>
              <div className="agent-status__item">
                <span className="agent-status__label">事件数</span>
                <span className="agent-status__value">{stepCount}</span>
              </div>
              <div className="agent-status__item">
                <span className="agent-status__label">项目</span>
                <span className="agent-status__value agent-status__value--truncate" title={projectDir}>
                  {projectDir ? shortName(projectDir) : '—'}
                </span>
              </div>
              <div className="agent-status__item">
                <span className="agent-status__label">后端配置</span>
                <span className="agent-status__value agent-status__value--truncate" title={backendProfileName}>
                  {backendProfileName || '—'}
                </span>
              </div>
            </div>
          </Panel>
        </aside>
      </div>
    </div>
  );
}

function statusLabel(status: string, running: boolean): string {
  if (running && status === 'running') return '运行中…';
  switch (status) {
    case 'running':
      return '运行中';
    case 'done':
      return '已完成';
    case 'stopped':
      return '已停止';
    case 'failed':
      return '出错';
    case 'idle':
    default:
      return '空闲';
  }
}

function AgentEventCard({ ev }: { ev: AgentEvent }) {
  const [expanded, setExpanded] = useState(false);

  if (ev.type === 'thought') {
    return (
      <div className="agent-event agent-event--thought">
        <div className="agent-event__head">
          <span className="agent-event__icon">💭</span>
          <span className="agent-event__title">思考 #{ev.step}</span>
        </div>
        <div className="agent-event__content">{ev.content}</div>
      </div>
    );
  }

  if (ev.type === 'tool_call') {
    return (
      <div className="agent-event agent-event--call">
        <div className="agent-event__head">
          <span className="agent-event__icon">🔧</span>
          <span className="agent-event__title">工具调用 #{ev.step}</span>
        </div>
        <div className="agent-event__line">
          <span className="agent-event__name">{ev.name}</span>
        </div>
        <pre className="agent-event__code">{formatArgs(ev.arguments)}</pre>
      </div>
    );
  }

  if (ev.type === 'tool_result') {
    const ok = ev.ok !== false;
    const payload = ok ? ev.result : ev.error;
    const text = formatPayload(payload);
    const long = text.length > 400;
    return (
      <div className={`agent-event agent-event--result${ok ? '' : ' agent-event--result-fail'}`}>
        <div className="agent-event__head">
          <span className="agent-event__icon">{ok ? '✅' : '❌'}</span>
          <span className="agent-event__title">
            工具结果 #{ev.step} · {ev.name}
            {typeof ev.duration_ms === 'number' ? ` · ${ev.duration_ms}ms` : ''}
          </span>
        </div>
        <pre className="agent-event__code">
          {long && !expanded ? text.slice(0, 400) + '…' : text}
        </pre>
        {long ? (
          <button
            type="button"
            className="agent-event__toggle"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? '收起' : '展开全部'}
          </button>
        ) : null}
      </div>
    );
  }

  if (ev.type === 'finish') {
    return (
      <div className="agent-event agent-event--finish">
        <div className="agent-event__head">
          <span className="agent-event__icon">🏁</span>
          <span className="agent-event__title">完成 · {ev.total_steps ?? ev.step} 步</span>
        </div>
        <div className="agent-event__content">{ev.summary}</div>
      </div>
    );
  }

  if (ev.type === 'error') {
    return (
      <div className="agent-event agent-event--error">
        <div className="agent-event__head">
          <span className="agent-event__icon">⚠️</span>
          <span className="agent-event__title">出错 #{ev.step}</span>
        </div>
        <div className="agent-event__content">{ev.message}</div>
      </div>
    );
  }

  if (ev.type === 'stopped') {
    return (
      <div className="agent-event agent-event--stopped">
        <div className="agent-event__head">
          <span className="agent-event__icon">⏹️</span>
          <span className="agent-event__title">已停止 #{ev.step}</span>
        </div>
        <div className="agent-event__content">{ev.reason || '用户停止'}</div>
      </div>
    );
  }

  return null;
}

function formatArgs(args: unknown): string {
  if (args === undefined || args === null) return '';
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

function formatPayload(payload: unknown): string {
  if (payload === undefined || payload === null) return '';
  if (typeof payload === 'string') return payload;
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}
