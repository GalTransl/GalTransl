import type { ReactNode } from 'react';

type StatsGridProps = {
  children: ReactNode;
  className?: string;
  compact?: boolean;
};

export function StatsGrid({ children, className, compact = false }: StatsGridProps) {
  const classes = ['stats-grid', compact ? 'stats-grid--compact' : '', className].filter(Boolean).join(' ');
  return <div className={classes}>{children}</div>;
}
