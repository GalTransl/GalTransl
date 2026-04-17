import type { PropsWithChildren, ReactNode } from 'react';

type PanelProps = PropsWithChildren<{
  title: string;
  description?: string;
  actions?: ReactNode;
  className?: string;
}>;

export function Panel({ actions, children, description, title, className = '' }: PanelProps) {
  return (
    <section className={`panel${className ? ` ${className}` : ''}`}>
      <header className="panel__header">
        <div>
          <h2>{title}</h2>
          {description ? <p>{description}</p> : null}
        </div>
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      <div className="panel__body">{children}</div>
    </section>
  );
}
