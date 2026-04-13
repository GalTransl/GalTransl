import type { ReactNode } from 'react';

type PageHeaderProps = {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  status?: ReactNode;
  className?: string;
};

export function PageHeader({ title, description, actions, status, className }: PageHeaderProps) {
  const classes = ['page-header', className].filter(Boolean).join(' ');

  return (
    <header className={classes}>
      <div className="page-header__topline">
        <div className="page-header__copy">
          <h1>{title}</h1>
          {description ? <p>{description}</p> : null}
        </div>
        {actions ? <div className="page-header__actions">{actions}</div> : null}
      </div>
      {status ? <div className="page-header__status">{status}</div> : null}
    </header>
  );
}
