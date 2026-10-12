import type { JobStatus } from '../lib/api';

type StatusBadgeTone = JobStatus | 'online' | 'offline' | 'connecting' | 'checking-availability';

type StatusBadgeProps = {
  label: string;
  tone: StatusBadgeTone;
  celebrate?: boolean;
  working?: boolean;
};

export function StatusBadge({ label, tone, celebrate, working = false }: StatusBadgeProps) {
  return <span className={`status-badge status-badge--${tone}${working ? ' status-badge--working' : ''}${celebrate ? ' status-badge--celebrate' : ''}`}>{label}</span>;
}
