import { useEffect, useRef, useState } from 'react';
import {
  encodeProjectDir,
  fetchJobs,
  fetchProjectRuntime,
  type ProjectRuntimeResponse,
  type RuntimeJob,
} from '../../../lib/api';
import { Icon } from '../../../components/Icon';
import {
  clampPercent,
  formatElapsedTime,
  formatEta,
  formatPercentDisplay,
  formatSpeed,
} from '../../translateRuntimeShared';
import { ToolBlock } from './ToolRow';
import type { ActivityItem } from '../timeline';
import { asArgs, formatPayload, str } from '../toolMeta';

/* ── 启动翻译卡片（开始翻译顶部卡的迷你版） ──
   原本这里是「启动翻译 · ForGal-json · 仅 1 个文件」加一段原始参数/结果 JSON。
   现在换成工作台那张顶部卡的瘦身版：百分比 + 进度条 + 已译/总数，外加实时速度、
   预计剩余、已用时长三个关键数字。翻译期间每秒拉一次运行时快照并保持展开，
   跑完自动折叠（用户自己点过就听用户的）；原始参数/结果收在开关后面。 */

const JOB_CARD_POLL_MS = 1000;

export function TranslationJobCard({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  const args = asArgs(item.arguments);
  const result =
    item.result && typeof item.result === 'object' ? (item.result as Record<string, unknown>) : {};
  const jobId = str(result.job_id);
  const translator = str(args?.translator) || str(result.translator);
  const fileCount = Array.isArray(args?.files) ? args.files.length : 0;

  const [runtime, setRuntime] = useState<ProjectRuntimeResponse | null>(null);
  const [lastStatus, setLastStatus] = useState<string>(str(result.status));
  const [open, setOpen] = useState(true);
  const [rawOpen, setRawOpen] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const userToggledRef = useRef(false);
  const seenJobRef = useRef(false);
  const fallbackRef = useRef(false);

  // 快照里"当前任务"就是这张卡的任务时才认它（换了新任务就不再跟）
  const snapJob: RuntimeJob | null = runtime && jobId && runtime.job?.job_id === jobId ? runtime.job : null;
  const status = snapJob?.status ?? lastStatus;
  const isRunning = status === 'pending' || status === 'running';

  // 翻译期间每秒拉一次运行时快照（进度/速度/剩余/时长都从它来）；不在跑了就停。
  useEffect(() => {
    if (!jobId || !projectDir || !isRunning) return undefined;
    const projectId = encodeProjectDir(projectDir);
    let cancelled = false;
    const pull = async () => {
      try {
        const data = await fetchProjectRuntime(projectId);
        if (cancelled) return;
        setRuntime(data);
        if (data.job && data.job.job_id === jobId) {
          seenJobRef.current = true;
          setLastStatus(data.job.status);
          return;
        }
        // 快照里的"当前任务"不是这张卡的（刷新页面后回放旧转录、或工作台又开了新任务）：
        // 去任务列表里查一次它的最终状态，别一直按"翻译中"空转轮询。
        if (!seenJobRef.current && !fallbackRef.current) {
          fallbackRef.current = true;
          const jobs = await fetchJobs();
          if (cancelled) return;
          const mine = jobs.find((j) => j.job_id === jobId);
          setLastStatus(mine ? mine.status : 'completed');
        }
      } catch {
        // 后端不可达：静默跳过，下一拍再试（卡片保持上一帧数据）
      }
    };
    void pull();
    const poll = window.setInterval(() => void pull(), JOB_CARD_POLL_MS);
    const tick = window.setInterval(() => setNowMs(Date.now()), JOB_CARD_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, [jobId, projectDir, isRunning]);

  // 翻译期间自动展开、跑完自动折叠
  useEffect(() => {
    if (userToggledRef.current) return;
    setOpen(isRunning);
  }, [isRunning]);

  const summary = runtime?.summary ?? null;
  const percent = clampPercent(summary?.percent ?? 0);
  const translated = summary?.translated ?? 0;
  const total = summary?.total ?? 0;
  const remaining = Math.max(total - translated, 0);
  const failed = item.ok === false || status === 'failed';
  const tone = isRunning ? 'running' : failed ? 'error' : 'done';
  const stateLabel = isRunning
    ? status === 'pending'
      ? '等待中'
      : '翻译中'
    : failed
      ? '失败'
      : status === 'cancelled'
        ? '已取消'
        : '已完成';
  const resultText = formatPayload(item.ok === false ? item.error : item.result);
  // GenDic 跑的是分片/批次而不是句子：进度与速度的单位都跟着它换（与工作台一致）
  const progressUnit = translator === 'GenDic' || (runtime?.stage ?? '').startsWith('GenDic') ? '项' : '句';

  return (
    <div className={`agent-tjob${open ? ' is-open' : ''}${isRunning ? ' is-live' : ''}`}>
      <button
        type="button"
        className="agent-tjob__header"
        onClick={() => {
          userToggledRef.current = true;
          setOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-tjob__icon"><Icon name="play" /></span>
        <span className="agent-tjob__action">启动翻译</span>
        <span className="agent-tjob__summary">
          {[translator, fileCount ? `仅 ${fileCount} 个文件` : ''].filter(Boolean).join(' · ')}
        </span>
        {total > 0 ? (
          <span className="agent-tjob__count">
            {translated} / {total} {progressUnit}
          </span>
        ) : null}
        <span className={`agent-tjob__state is-${tone}`}>
          <span className="agent-tjob__state-dot" />
          {stateLabel}
        </span>
        <span className="agent-tjob__caret">›</span>
      </button>

      {open ? (
        <div className="agent-tjob__body">
          <div className="agent-tjob__gauge">
            <span className="agent-tjob__percent">
              {formatPercentDisplay(summary?.percent ?? 0)}
              <span className="agent-tjob__percent-sign">%</span>
            </span>
            <span className="agent-tjob__frac">
              已译 <b>{translated}</b> / {total} 句
              <span className="agent-tjob__frac-remain">剩余 {remaining}</span>
            </span>
          </div>

          <div className="agent-tjob__bar" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
            <div className="agent-tjob__bar-fill" style={{ width: `${percent}%` }} />
          </div>

          <div className="agent-tjob__stats">
            <span className="agent-tjob__stat">
              <b>{formatSpeed(summary?.translation_speed_lpm ?? 0, progressUnit)}</b>
              <i>实时速度</i>
            </span>
            <span className="agent-tjob__stat">
              <b>{formatEta(summary?.eta_seconds ?? 0)}</b>
              <i>预计剩余</i>
            </span>
            <span className="agent-tjob__stat">
              <b>{formatElapsedTime(snapJob, nowMs)}</b>
              <i>已用时长</i>
            </span>
          </div>

          <button
            type="button"
            className="agent-tjob__raw-toggle"
            onClick={() => setRawOpen((v) => !v)}
            aria-expanded={rawOpen}
          >
            {rawOpen ? '收起原始数据' : '原始参数 / 结果'}
          </button>
          {rawOpen ? (
            <>
              {item.arguments !== undefined ? (
                <ToolBlock title="参数" content={formatPayload(item.arguments)} mono />
              ) : null}
              {resultText ? (
                <ToolBlock
                  title={item.ok === false ? '错误' : '结果'}
                  content={resultText}
                  markdown={item.ok !== false && typeof item.result === 'string'}
                  mono
                  truncate={1200}
                  tone={item.ok === false ? 'error' : 'default'}
                  durationMs={item.durationMs}
                />
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
