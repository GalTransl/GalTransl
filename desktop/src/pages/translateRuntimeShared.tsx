import { useEffect, useRef, useState } from 'react';
import { speakerStyle } from '../lib/speaker';
import { resolveSpeakerName } from '../lib/useNameDict';
import type {
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

export function FileProgressRow({
  file,
  isSuccessFileFilterActive,
  onToggleSuccessFileFilter }: {
  file: FileProgress;
  isSuccessFileFilterActive: boolean;
  onToggleSuccessFileFilter: (filename: string) => void;
}) {
  const percent = file.total > 0 ? Math.round((file.translated / file.total) * 100) : 0;
  const isComplete = file.translated === file.total && file.total > 0;
  const hasFailed = file.failed > 0;

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
              {isSuccessFileFilterActive ? <span className="file-progress-row__filter-check">✓</span> : null}
            </button>
          </span>
          <span className="file-progress-row__state">{isComplete ? '已完成' : percent > 0 ? '处理中' : '排队中'}</span>
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

export function formatSpeed(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 行/分';
  return `${value.toFixed(value >= 10 ? 0 : 1)} 行/分`;
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
