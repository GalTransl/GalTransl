import type { ReactNode } from 'react';

type EmptyStateProps = {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({ title, description, action, className }: EmptyStateProps) {
  const classes = ['empty-state', 'page-state', className].filter(Boolean).join(' ');

  return (
    <div className={classes} role="status">
      <div className="page-state__body">
        <strong>{title}</strong>
        {description ? <span>{description}</span> : null}
      </div>
      {action ? <div className="page-state__action">{action}</div> : null}
    </div>
  );
}
