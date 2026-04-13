import type { ReactNode } from 'react';

type InlineFeedbackTone = 'error' | 'info' | 'success';

type InlineFeedbackProps = {
  title?: string;
  description?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  tone?: InlineFeedbackTone;
  className?: string;
};

export function InlineFeedback({
  title,
  description,
  children,
  action,
  tone = 'info',
  className,
}: InlineFeedbackProps) {
  const content = children ?? description;
  const classes = ['inline-alert', `inline-alert--${tone}`, className].filter(Boolean).join(' ');
  const role = tone === 'error' ? 'alert' : 'status';

  return (
    <div className={classes} role={role}>
      <div className="page-state-feedback__body">
        {title ? <strong className="page-state-feedback__title">{title}</strong> : null}
        {content ? <div className="page-state-feedback__description">{content}</div> : null}
      </div>
      {action ? <div className="page-state-feedback__action">{action}</div> : null}
    </div>
  );
}
