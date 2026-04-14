import { useEffect, useRef, useState, type ReactNode } from 'react';

type InlineFeedbackTone = 'error' | 'info' | 'success';

type InlineFeedbackProps = {
  title?: string;
  description?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  tone?: InlineFeedbackTone;
  className?: string;
  /** 自动消失延迟(ms)，设置后到时间会淡出并触发 onDismiss；success 默认 4000，error 不自动消失 */
  autoDismiss?: number;
  /** 淡出动画结束后回调，通常用来清除父组件的 info/error 状态 */
  onDismiss?: () => void;
};

const DEFAULT_AUTO_DISMISS: Record<InlineFeedbackTone, number | undefined> = {
  success: 4000,
  info: 4000,
  error: undefined,
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

  useEffect(() => {
    if (dismissMs == null) return;
    timerRef.current = setTimeout(() => {
      setFading(true);
      // 等淡出动画结束后再移除 DOM
      fadeRef.current = setTimeout(() => {
        setVisible(false);
        onDismiss?.();
      }, 400);
    }, dismissMs);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (fadeRef.current) clearTimeout(fadeRef.current);
    };
  }, [dismissMs, onDismiss]);

  if (!visible) return null;

  return (
    <div
      className={[classes, fading ? 'inline-alert--fading' : ''].filter(Boolean).join(' ')}
      role={role}
    >
      <div className="page-state-feedback__body">
        {title ? <strong className="page-state-feedback__title">{title}</strong> : null}
        {content ? <div className="page-state-feedback__description">{content}</div> : null}
      </div>
      {action ? <div className="page-state-feedback__action">{action}</div> : null}
    </div>
  );
}
