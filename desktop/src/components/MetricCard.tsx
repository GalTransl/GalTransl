import type { ReactNode } from 'react';

type MetricCardTone = 'default' | 'accent' | 'success' | 'warning' | 'danger';

type MetricCardProps = {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  tone?: MetricCardTone;
  icon?: ReactNode;
  className?: string;
  active?: boolean;
  onClick?: () => void;
};

export function MetricCard({
  label,
  value,
  hint,
  tone = 'default',
  icon,
  className,
  active = false,
  onClick,
}: MetricCardProps) {
  const classes = [
    'metric-card',
    `metric-card--${tone}`,
    active ? 'metric-card--active' : '',
    onClick ? 'metric-card--interactive' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const content = (
    <>
      <div className="metric-card__header">
        <span className="metric-card__label">{label}</span>
        {icon ? <span className="metric-card__icon">{icon}</span> : null}
      </div>
      <strong className="metric-card__value">{value}</strong>
      {hint ? <span className="metric-card__hint">{hint}</span> : null}
    </>
  );

  if (onClick) {
    return (
      <button className={classes} onClick={onClick} type="button">
        {content}
      </button>
    );
  }

  return <div className={classes}>{content}</div>;
}
