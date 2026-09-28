import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import { speakerStyle } from '../lib/speaker';
import { resolveSpeakerName } from '../lib/useNameDict';
import type {
  FileActivity,
  FileProgress,
  Job,
  ProjectRuntimeErrorEntry,
  ProjectRuntimeSuccessEntry,
  RuntimeJob,
} from '../lib/api';

export function RuntimeErrorRow({ entry }: { entry: ProjectRuntimeErrorEntry }) {
  const [copied, setCopied] = useState(false);
  const [isMessageTruncated, setIsMessageTruncated] = useState(false);
  const messageRef = useRef<HTMLParagraphElement | null>(null);
  const messageText = (entry.message || '').trim();
  const kindLabel = getErrorKindLabel(entry.kind);
  const modelLabel = compactModelLabel(entry.model);

  useEffect(() => {
    const el = messageRef.current;
    if (!el) {
      setIsMessageTruncated(false);
      return;
    }

    const updateTruncation = () => {
      const truncated = el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
      setIsMessageTruncated(truncated);
    };

    updateTruncation();

    if (typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver(() => {
      updateTruncation();
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
    };
  }, [messageText]);

  const handleCopyMessage = async () => {
    if (!messageText) return;
    const ok = await copyTextToClipboard(messageText);
    if (!ok) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <article className="runtime-event runtime-event--error">
      <div className="runtime-event__header">
        <div className="runtime-event__badges">
          <span className="runtime-event__pill runtime-event__pill--danger">{kindLabel}</span>
          {(entry.retry_count ?? 0) > 0 ? <span className="runtime-event__pill">重试 {entry.retry_count}</span> : null}
        </div>
        <div className="runtime-event__header-right">
          <div className="runtime-event__error-time-action">
            <time className="runtime-event__timestamp runtime-event__timestamp--error">{formatTime(entry.ts)}</time>
            <button
              type="button"
              className={`icon-btn runtime-event__copy-btn runtime-event__time-copy${copied ? ' runtime-event__copy-btn--copied' : ''}`}
              onClick={() => void handleCopyMessage()}
              disabled={!messageText}
              title={!messageText ? '无可复制内容' : (copied ? '已复制' : '复制错误信息')}
              aria-label={!messageText ? '无可复制内容' : '复制错误信息'}
            >
              {copied ? (
                <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
                  <path d="M3.2 8.6l3.1 3.1 6.5-6.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : (
                <svg viewBox="0 0 16 16" width="15" height="15" fill="none" aria-hidden="true">
                  <rect x="6" y="2.5" width="7.5" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.4" />
                  <path d="M4.5 5.5H3.9A1.4 1.4 0 0 0 2.5 6.9v5.2a1.4 1.4 0 0 0 1.4 1.4h4.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                </svg>
              )}
            </button>
          </div>
        </div>
      </div>
      <p
        ref={messageRef}
        className="runtime-event__message"
        title={isMessageTruncated && messageText ? messageText : undefined}
      >
        {entry.message || '未提供错误详情。'}
      </p>
      <dl className="runtime-event__meta">
        {entry.kind !== 'api' && (
          <div>
            <dd>{`${stripAllExtensions(entry.filename) || '—'}: ${entry.index_range || '—'}`}</dd>
          </div>
        )}
        <div className="runtime-event__meta-model">
          <dd>{modelLabel || '—'}</dd>
        </div>
        {(entry.sleep_seconds ?? 0) > 0 ? <span className="runtime-event__pill">退避 {Number(entry.sleep_seconds).toFixed(3)}s</span> : null}
      </dl>
    </article>
  );
}

export function RuntimeSuccessRow({
  entry,
  isFresh,
  isSuccessFileFilterActive,
  onToggleSuccessFileFilter,
  nameDict }: {
  entry: ProjectRuntimeSuccessEntry;
  isFresh: boolean;
  isSuccessFileFilterActive: boolean;
  onToggleSuccessFileFilter: (filename: string) => void;
  nameDict: Map<string, string>;
}) {
  const rawSpeakerLabel = Array.isArray(entry.speaker) ? entry.speaker.join(' / ') : entry.speaker;
  const speakerLabel = rawSpeakerLabel
    ? (Array.isArray(entry.speaker)
        ? entry.speaker.map((s) => resolveSpeakerName(s, nameDict)).join(' / ')
        : resolveSpeakerName(rawSpeakerLabel, nameDict))
    : rawSpeakerLabel;
  const speakerStyleVal = rawSpeakerLabel ? speakerStyle(rawSpeakerLabel) : undefined;
  const entryFilename = entry.filename || '未命名文件';
  const filterFilename = entry.filename;
  const translatorLabel = compactModelLabel(entry.trans_by);

  return (
    <article className={`runtime-event runtime-event--success${isFresh ? ' runtime-event--fresh' : ''}`}>
      <div className="runtime-event__header">
        <div className="runtime-event__badges">
          <span className="runtime-event__pill runtime-event__pill--success">#{entry.index}</span>
          <span
            className={`runtime-event__pill runtime-event__pill--file${filterFilename ? ' runtime-event__pill--file-clickable' : ''}${isSuccessFileFilterActive ? ' runtime-event__pill--file-active' : ''}`}
            title={entryFilename}
          >
            {filterFilename ? (
              <button
                aria-label="筛选句流"
                aria-pressed={isSuccessFileFilterActive}
                className="runtime-event__file-name-btn"
                onClick={() => onToggleSuccessFileFilter(filterFilename)}
                title="筛选句流"
                type="button"
              >
                {entryFilename}
              </button>
            ) : (
              <span className="runtime-event__file-text">{entryFilename}</span>
            )}
          </span>
        </div>
        <div className="runtime-event__header-right">
          {translatorLabel ? <span className="runtime-event__pill runtime-event__pill--translator">{translatorLabel}</span> : null}
          <time className="runtime-event__timestamp">{formatTime(entry.ts)}</time>
        </div>
      </div>
      <div className="runtime-success-compact">
        <p className="runtime-success-compact__line">
          <span className="runtime-success-compact__label">SRC</span>
          {speakerLabel ? <span className="runtime-success-compact__speaker-inline" style={speakerStyleVal}>{speakerLabel}</span> : null}
          <span title={entry.source_preview || undefined}>{entry.source_preview || '—'}</span>
        </p>
        <p className="runtime-success-compact__line">
          <span className="runtime-success-compact__label">DST</span>
          {speakerLabel ? <span className="runtime-success-compact__speaker-inline" style={speakerStyleVal}>{speakerLabel}</span> : null}
          <span title={entry.translation_preview || undefined}>{entry.translation_preview || '—'}</span>
        </p>
      </div>
    </article>
  );
}

/** 小灯各阶段的文案：请求已发出、等第一个字 / 吐思考 / 出正文 / 上一次失败、退避等重试 */
const LIVE_PHASE_LABEL: Record<FileActivity['phase'], string> = {
  waiting: '请求中',
  thinking: '思考中',
  writing: '翻译中',
  retrying: '重试中',
};

/**
 * 小灯光晕的呼吸速度：CSS 里一个周期 1.2s 的倍数，跟着输出的字/秒走。
 * 按对数取——实际输出从每秒几个字到几百字都有，线性映射要么全挤在最快一档、要么看不出差别：
 * 出字时 0 字/秒 0.6× → 30 字/秒 1.3× → 150 字/秒 2× → 封顶 2.2×（约 0.55s 一个周期，再快就成频闪了）；
 * 请求中/重试中没有字，慢慢呼吸（0.5×，2.4s 一个周期）。
 */
function ledPulseRate(activity: FileActivity): number {
  if (activity.phase === 'waiting' || activity.phase === 'retrying') return 0.5;
  const cps = Number.isFinite(activity.cps) && activity.cps > 0 ? activity.cps : 0;
  const rate = Math.min(2.2, 0.6 + 0.35 * Math.log2(1 + cps / 10));
  // 取到 0.05 一档：字/秒每次轮询都在小幅抖动，没必要次次去改动画
  return Math.round(rate * 20) / 20;
}

function liveActivityTitle(activity: FileActivity): string {
  const parts = [LIVE_PHASE_LABEL[activity.phase]];
  if (activity.phase === 'waiting') {
    parts.push('请求已发出，等模型开始输出');
  } else if (activity.phase === 'retrying') {
    parts.push('上一次请求失败，稍后重试（原因见「错误」）');
  } else {
    const cps = Number.isFinite(activity.cps) && activity.cps > 0 ? activity.cps : 0;
    parts.push(`${cps.toFixed(cps >= 10 ? 0 : 1)} 字/秒`);
  }
  if (activity.requests > 1) parts.push(`${activity.requests} 个请求同时在跑`);
  return parts.join(' · ');
}

/**
 * 「文件进度」里的状态灯：点常亮、外圈光晕呼吸（样式见 project-translate-v2.css）。
 * 呼吸快慢用 Web Animations 的 playbackRate 调，而不是改 animation-duration——后者会让正在跑的
 * 动画按新时长重算进度，每次轮询（1s）灯都会跳一下；playbackRate 从当前进度接着走，只是变快变慢。
 */
function FileProgressLed({ phase, rate }: { phase: FileActivity['phase']; rate: number }) {
  const ref = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof el.getAnimations !== 'function') return;
    for (const animation of el.getAnimations()) {
      if (typeof animation.updatePlaybackRate === 'function') animation.updatePlaybackRate(rate);
      else animation.playbackRate = rate;
    }
  }, [phase, rate]);
  return (
    <span
      ref={ref}
      className={`file-progress-row__led file-progress-row__led--${phase}`}
      aria-hidden="true"
    />
  );
}

/** 两批之间（上一批写完缓存、下一批刚要发）会有一小段没有请求在跑，轮询正好落在这里时灯会灭一下、
 *  标签掉回「未完成」。刚才还亮着的话，就沿用上一个状态这么久。 */
const LIVE_HOLD_MS = 2000;

export function FileProgressRow({
  file,
  isRunning,
  isSuccessFileFilterActive,
  onToggleSuccessFileFilter }: {
  file: FileProgress;
  /** 任务是否还在跑（pending/running）：没在跑就不再亮灯、也不说「排队中」 */
  isRunning: boolean;
  isSuccessFileFilterActive: boolean;
  onToggleSuccessFileFilter: (filename: string) => void;
}) {
  const percent = file.total > 0 ? Math.round((file.translated / file.total) * 100) : 0;
  const isComplete = file.translated === file.total && file.total > 0;
  const hasFailed = file.failed > 0;
  // 这个文件此刻有没有请求在跑（后端按请求登记/注销给的）。任务停了、文件已完成就不再认
  const active = isRunning && !isComplete;
  const lastLiveRef = useRef<{ activity: FileActivity; at: number } | null>(null);
  const now = Date.now();
  if (active && file.activity) lastLiveRef.current = { activity: file.activity, at: now };
  const held = lastLiveRef.current;
  const live = active
    ? file.activity ?? (held && now - held.at <= LIVE_HOLD_MS ? held.activity : null)
    : null;
  // 状态文案：有请求在跑就是它的阶段（几个请求同时在跑时带上 ×n）；没在跑的未完成文件只是
  // 「未完成」（在等线程，或者这一轮不会再碰它），一点没翻的在任务里是「排队中」
  const stateLabel = isComplete
    ? '已完成'
    : live
      ? `${LIVE_PHASE_LABEL[live.phase]}${live.requests > 1 ? ` ×${live.requests}` : ''}`
      : percent > 0
        ? '未完成'
        : isRunning ? '排队中' : '未开始';

  return (
    <div className="file-progress-row file-progress-row--runtime">
      <div className="file-progress-row__info">
        <div className="file-progress-row__identity">
          <span className="file-progress-row__name-wrap">
            <span className="file-progress-row__name">{file.filename}</span>
            <button
              aria-label="筛选句流"
              aria-pressed={isSuccessFileFilterActive}
              className={`file-progress-row__filter-toggle${isSuccessFileFilterActive ? ' file-progress-row__filter-toggle--active' : ''}`}
              onClick={() => onToggleSuccessFileFilter(file.filename)}
              title="筛选句流"
              type="button"
            >
              <FilterFunnelIcon className="file-progress-row__filter-icon" />
              <span className="file-progress-row__filter-tooltip">筛选句流</span>
              {isSuccessFileFilterActive ? <span className="file-progress-row__filter-check"><Icon name="check" /></span> : null}
            </button>
          </span>
          <span className="file-progress-row__state" title={live ? liveActivityTitle(live) : undefined}>
            {live ? <FileProgressLed phase={live.phase} rate={ledPulseRate(live)} /> : null}
            {stateLabel}
          </span>
        </div>
        <span className="file-progress-row__count">
          {file.translated}/{file.total}
          {hasFailed ? <span className="file-progress-row__failed"> · {file.failed}失败</span> : null}
        </span>
      </div>
      <div className="progress-bar progress-bar--small">
        <div className="progress-bar__fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function FilterFunnelIcon({ className }: { className: string }) {
  return (
    <svg aria-hidden="true" className={className} viewBox="0 0 24 24">
      <path d="M3 5h18l-7 8v5.5l-4 1.9V13L3 5z" fill="currentColor" />
    </svg>
  );
}

export function toRuntimeJob(job: Job): RuntimeJob {
  return {
    job_id: job.job_id,
    status: job.status,
    translator: job.translator,
    created_at: job.created_at,
    started_at: job.started_at,
    finished_at: job.finished_at,
    error: job.error,
    gendic_added_entries: job.gendic_added_entries,
    gendic_duplicated_entries: job.gendic_duplicated_entries,
  };
}

export function getStatusLabel(status?: RuntimeJob['status']) {
  switch (status) {
    case 'running':
      return '翻译中';
    case 'pending':
      return '等待中';
    case 'completed':
      return '已完成';
    case 'failed':
      return '失败';
    case 'cancelled':
      return '已取消';
    default:
      return '空闲';
  }
}

export function getErrorKindLabel(kind: string): string {
  const normalized = (kind || '').trim().toLowerCase();
  if (normalized === 'parse') return '解析';
  if (normalized === 'api') return '后端';
  return kind || 'error';
}

function compactModelLabel(value: string | null | undefined): string {
  const text = (value || '').trim();
  if (!text) return '';
  const idx = text.lastIndexOf('/');
  if (idx < 0) return text;
  const tail = text.slice(idx + 1).trim();
  return tail || text;
}

async function copyTextToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fallback below
  }

  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', 'true');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(textarea);
    return copied;
  } catch {
    return false;
  }
}

export function formatDate(isoString: string): string {
  if (!isoString) return '—';
  try {
    const date = new Date(isoString);
    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit' });
  } catch {
    return isoString;
  }
}

export function formatTime(isoString: string): string {
  if (!isoString) return '—';
  try {
    return new Date(isoString).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit' });
  } catch {
    return isoString;
  }
}

/** unit 是速度的单位，跟进度同一口径：普通翻译是「句」，GenDic（分片/批次）是「项」 */
export function formatSpeed(value: number, unit = '行'): string {
  if (!Number.isFinite(value) || value <= 0) return `0 ${unit}/分`;
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${unit}/分`;
}

export function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return `${hours} 时 ${minutes} 分`;
}

export function formatElapsedTime(job: RuntimeJob | null, nowMs: number): string {
  if (!job?.started_at) {
    return job?.status === 'pending' ? '等待开始' : '—';
  }

  const startMs = Date.parse(job.started_at);
  if (Number.isNaN(startMs)) return '—';

  const endMs = job.finished_at ? Date.parse(job.finished_at) : nowMs;
  const safeEndMs = Number.isNaN(endMs) ? nowMs : endMs;
  const elapsedSeconds = Math.max(0, Math.floor((safeEndMs - startMs) / 1000));

  if (elapsedSeconds < 60) return `${elapsedSeconds} 秒`;
  if (elapsedSeconds < 3600) {
    const minutes = Math.floor(elapsedSeconds / 60);
    const seconds = elapsedSeconds % 60;
    return `${minutes} 分 ${seconds} 秒`;
  }

  const hours = Math.floor(elapsedSeconds / 3600);
  const minutes = Math.floor((elapsedSeconds % 3600) / 60);
  return `${hours} 时 ${minutes} 分`;
}

export function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, value));
}

export function formatPercentDisplay(value: number): string {
  const clamped = clampPercent(value);
  const rounded = Number(clamped.toFixed(1));
  return Number.isInteger(rounded) ? `${rounded}` : rounded.toFixed(1);
}

function stripAllExtensions(filename: string): string {
  return filename.replace(/(\.[^.]+)+$/, '');
}
