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
                aria-label="筛选译文"
                aria-pressed={isSuccessFileFilterActive}
                className="runtime-event__file-name-btn"
                onClick={() => onToggleSuccessFileFilter(filterFilename)}
                title="筛选译文"
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

/** 小灯各阶段的文案：请求已发出、等第一个字 / 吐思考 / 出正文 / 上一次失败、退避等重试。
 *  停住（stalled）不改阶段文字：思考中还是思考中，只有灯的呼吸慢下来。 */
const LIVE_PHASE_LABEL: Record<FileActivity['phase'], string> = {
  waiting: '请求中',
  thinking: '思考中',
  writing: '翻译中',
  retrying: '重试中',
};

/**
 * 小灯的呼吸速度：CSS 里一个周期 1.2s 的倍数（倍率越大闪得越快）。
 *
 * 光晕幅度只决定「看不看得见」，快慢感知靠的是周期差，所以映射得让**常用速度区间**拉得开——
 * 一秒几十个字才是常态。之前用 0.6×+0.35×log2(1+cps/10)，10~80 字/秒的周期只从 1.26s 变到
 * 0.71s（1.8 倍），再叠上那点几乎看不见的光晕，看上去就是个不动的点。
 *
 * 现在取「倍率 ∝ 字/秒的开方」：0.5×√(cps/5)，封顶 2.8×，10~80 字/秒的周期差拉到 2.9 倍：
 *   5 字/秒 → 0.50×（2.40s）    10 字/秒 → 0.70×（1.71s）   20 字/秒 → 1.00×（1.20s）
 *   40 字/秒 → 1.40×（0.86s）   80 字/秒 → 2.00×（0.60s）   120 字/秒 → 2.45×（0.49s）
 *   ≈157 字/秒往上封顶 2.8×（0.43s）——再快就成频闪了
 * 5 字/秒以下（含 0）都按最慢档算：没怎么出字，本来就该慢慢呼吸。
 */
const LED_SLOWEST_RATE = 0.45; // 请求中/重试中：没在出字，比出字的最慢档（0.5）再慢一点
const LED_STALLED_RATE = 0.3; // 停住：阶段照旧（思考中/翻译中），灯再放慢一档（1.2/0.3 = 4s 一次呼吸）
const LED_FASTEST_RATE = 2.8;
const LED_ANCHOR_CPS = 5; // 到这个速度才脱离最慢档

function ledPulseRate(activity: FileActivity): number {
  // 停住优先：后端说这一行有请求却没出新字，就只放慢灯，不把阶段退回「请求中」
  if (activity.stalled) return LED_STALLED_RATE;
  if (activity.phase === 'waiting' || activity.phase === 'retrying') return LED_SLOWEST_RATE;
  const cps = Number.isFinite(activity.cps) && activity.cps > 0 ? activity.cps : 0;
  const rate = Math.min(
    LED_FASTEST_RATE,
    0.5 * Math.sqrt(Math.max(cps, LED_ANCHOR_CPS) / LED_ANCHOR_CPS),
  );
  // 取到 0.05 一档：字/秒每次轮询都在小幅抖动，没必要次次去改动画
  return Math.round(rate * 20) / 20;
}

function liveActivityTitle(activity: FileActivity): string {
  const parts = [LIVE_PHASE_LABEL[activity.phase]];
  if (activity.phase === 'waiting') {
    parts.push('请求已发出，等模型开始输出');
  } else if (activity.phase === 'retrying') {
    parts.push('上一次请求失败，稍后重试（原因见「错误」）');
  } else if (activity.stalled) {
    // 阶段仍是思考中/翻译中，只是这一阵没出新字：说明白，别让人以为卡死了
    parts.push('这一阵没有新输出，灯已放慢');
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

/** 圆环进度的直径与线宽：窄面板里 10 来行排下来，一根横杠太吵，换成一个小环只占 18px。 */
const RING_SIZE = 18;
const RING_STROKE = 2;
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/** 环的颜色语义（见 project-translate-v2.css）：没在跑=灰、在跑=蓝、重试中=红、已完成=绿。
 *  环只表达「翻到哪了」，阶段（思考中/翻译中…）由旁边那颗会呼吸的状态灯表达——环要是也跟
 *  阶段换色，批与批之间就会在琥珀/蓝/绿之间闪。 */
type RingTone = 'idle' | 'active' | 'retrying' | 'done';

function ringToneOf(isComplete: boolean, live: FileActivity | null): RingTone {
  // 有请求在跑就一律按「在跑」画，哪怕计数已经满了：那种时候画成绿环说「已完成」是骗人的
  if (live) return live.phase === 'retrying' ? 'retrying' : 'active';
  return isComplete ? 'done' : 'idle';
}

/**
 * 「文件进度」里的圆环进度：一圈走完表示这个文件翻完了。
 *
 * 弧长直接由百分比算成 stroke-dashoffset（不做动画重算），过渡交给 CSS；0% 时 dashoffset
 * 等于整个周长、弧不可见，只剩底圈，所以「一点没翻」和「翻完了」一眼能分开。
 */
function FileProgressRing({ percent, tone }: { percent: number; tone: RingTone }) {
  const ratio = Math.max(0, Math.min(100, percent)) / 100;
  const center = RING_SIZE / 2;
  return (
    <svg
      className={`file-progress-row__ring file-progress-row__ring--${tone}`}
      width={RING_SIZE}
      height={RING_SIZE}
      viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
      aria-hidden="true"
    >
      <circle
        className="file-progress-row__ring-track"
        cx={center}
        cy={center}
        r={RING_RADIUS}
        fill="none"
        strokeWidth={RING_STROKE}
      />
      <circle
        className="file-progress-row__ring-arc"
        cx={center}
        cy={center}
        r={RING_RADIUS}
        fill="none"
        strokeWidth={RING_STROKE}
        strokeLinecap="round"
        strokeDasharray={RING_CIRCUMFERENCE}
        strokeDashoffset={RING_CIRCUMFERENCE * (1 - ratio)}
        // 转 -90° 让弧从 12 点开始顺时针长
        transform={`rotate(-90 ${center} ${center})`}
      />
    </svg>
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
  // 这个文件此刻有没有请求在跑（后端按请求登记/注销给的）。任务停了就不再认。
  // 注意别拿 isComplete 去按它：那个计数是「缓存里已经有多少句有译文」的口径（含上一轮跑出来的、
  // 也含正在被重翻的），完全可能出现 422/422 却还有请求在跑——那正是这一行最该说「翻译中」的时候。
  const active = isRunning;
  const lastLiveRef = useRef<{ activity: FileActivity; at: number } | null>(null);
  const now = Date.now();
  if (active && file.activity) lastLiveRef.current = { activity: file.activity, at: now };
  const held = lastLiveRef.current;
  const live = active
    ? file.activity ?? (held && now - held.at <= LIVE_HOLD_MS ? held.activity : null)
    : null;
  // 状态文案：**有请求在跑就先说它的阶段**（几个请求同时在跑时带上 ×n）——哪怕计数已经满了，
  // 这一行也还在动，不能报「已完成」；没在跑才轮到「已完成 / 未完成」（在等线程，或者这一轮
  // 不会再碰它），一点没翻的在任务里是「排队中」
  const stateLabel = live
    ? `${LIVE_PHASE_LABEL[live.phase]}${live.requests > 1 ? ` ×${live.requests}` : ''}`
    : isComplete
      ? '已完成'
      : percent > 0
        ? '未完成'
        : isRunning ? '排队中' : '未开始';

  return (
    <div
      className={`file-progress-row file-progress-row--runtime${live ? ' file-progress-row--live' : isComplete ? ' file-progress-row--done' : ''}`}
    >
      {/* 左列是两行文字（文件名 / 状态），右列是圆环组：它在整行里垂直居中，不挂在文件名那一行上 */}
      <div className="file-progress-row__main">
        <div className="file-progress-row__info">
          <span className="file-progress-row__name-wrap">
            <span className="file-progress-row__name">{file.filename}</span>
            <button
              aria-label="筛选译文"
              aria-pressed={isSuccessFileFilterActive}
              className={`file-progress-row__filter-toggle${isSuccessFileFilterActive ? ' file-progress-row__filter-toggle--active' : ''}`}
              onClick={() => onToggleSuccessFileFilter(file.filename)}
              title="筛选译文"
              type="button"
            >
              <FilterFunnelIcon className="file-progress-row__filter-icon" />
              <span className="file-progress-row__filter-tooltip">筛选译文</span>
              {isSuccessFileFilterActive ? <span className="file-progress-row__filter-check"><Icon name="check" /></span> : null}
            </button>
          </span>
        </div>
        {/* 状态行只剩灯与状态文字：进度收进右边那个圆环了，窄面板里不再铺一根横杠 */}
        <div className="file-progress-row__status">
          <span className="file-progress-row__state" title={live ? liveActivityTitle(live) : undefined}>
            {live ? <FileProgressLed phase={live.phase} rate={ledPulseRate(live)} /> : null}
            {stateLabel}
          </span>
        </div>
      </div>
      {/* 圆环＋计数绑一起：环放最右、贴齐行的右边缘，所以「186/186」和「86/86」的环都在同一条
          垂线上；计数在它左边，长短不一时只影响自己的左边缘 */}
      <span className="file-progress-row__meter">
        <span className="file-progress-row__count">
          {file.translated}/{file.total}
          {hasFailed ? <span className="file-progress-row__failed"> · {file.failed}失败</span> : null}
        </span>
        <FileProgressRing percent={percent} tone={ringToneOf(isComplete, live)} />
      </span>
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
