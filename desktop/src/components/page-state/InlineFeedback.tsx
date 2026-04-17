import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

type InlineFeedbackTone = 'error' | 'info' | 'success';

type InlineFeedbackProps = {
  title?: string;
  description?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  tone?: InlineFeedbackTone;
  className?: string;
  /** 自动消失延迟(ms)，设置后到时间会淡出并触发 onDismiss；success/info 默认 2200，error 不自动消失 */
  autoDismiss?: number;
  /** 淡出动画结束后回调，通常用来清除父组件的 info/error 状态 */
  onDismiss?: () => void;
};

const DEFAULT_AUTO_DISMISS: Record<InlineFeedbackTone, number | undefined> = {
  success: 2200,
  info: 2200,
  error: undefined,
};

const TONE_ICON: Record<InlineFeedbackTone, ReactNode> = {
  success: (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm4.4 7.9l-5.08 6.19a1 1 0 01-1.47.09l-2.7-2.53a1 1 0 111.37-1.46l1.92 1.8 4.42-5.38a1 1 0 111.54 1.29z" />
    </svg>
  ),
  error: (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 5.8a1.1 1.1 0 011.1 1.1v4.9a1.1 1.1 0 11-2.2 0V8.9A1.1 1.1 0 0112 7.8zm0 9.2a1.35 1.35 0 110-2.7 1.35 1.35 0 010 2.7z" />
    </svg>
  ),
  info: (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 4.5a1.3 1.3 0 110 2.6 1.3 1.3 0 010-2.6zm1.4 11.5h-2.8v-7h2.8v7z" />
    </svg>
  ),
};

export function InlineFeedback({
  title,
  description,
  children,
  action,
  tone = 'info',
  className,
  autoDismiss,
  onDismiss,
}: InlineFeedbackProps) {
  const content = children ?? description;
  const classes = ['inline-alert', `inline-alert--${tone}`, className].filter(Boolean).join(' ');
  const role = tone === 'error' ? 'alert' : 'status';

  const dismissMs = autoDismiss ?? DEFAULT_AUTO_DISMISS[tone];
  const [visible, setVisible] = useState(true);
  const [fading, setFading] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fadeRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const startDismiss = useCallback(() => {
    if (fading || !visible) {
      return;
    }
    setFading(true);
    if (fadeRef.current) {
      clearTimeout(fadeRef.current);
    }
    fadeRef.current = setTimeout(() => {
      setVisible(false);
      onDismiss?.();
    }, 400);
  }, [fading, visible, onDismiss]);

  useEffect(() => {
    if (dismissMs == null) return;
    timerRef.current = setTimeout(startDismiss, dismissMs);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [dismissMs, startDismiss]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (fadeRef.current) clearTimeout(fadeRef.current);
    };
  }, []);

  if (!visible) return null;

  return (
    <div
      className={[classes, fading ? 'inline-alert--fading' : ''].filter(Boolean).join(' ')}
      role={role}
    >
      <div className="inline-alert__icon">{TONE_ICON[tone]}</div>
      <div className="page-state-feedback__body">
        <div className="inline-alert__meta">
          <span className="inline-alert__app">GalTransl</span>
          <span className="inline-alert__dot" aria-hidden="true" />
          <span className="inline-alert__time">刚刚</span>
        </div>
        {title ? <strong className="page-state-feedback__title">{title}</strong> : null}
        {content ? <div className="page-state-feedback__description">{content}</div> : null}
      </div>
      {action ? <div className="page-state-feedback__action">{action}</div> : null}
      <button type="button" className="inline-alert__close" aria-label="关闭提示" onClick={startDismiss}>
        ×
      </button>
    </div>
  );
}
