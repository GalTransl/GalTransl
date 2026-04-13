import type { JobStatus } from '../lib/api';

type StatusBadgeTone = JobStatus | 'online' | 'offline' | 'connecting';

type StatusBadgeProps = {
  label: string;
  tone: StatusBadgeTone;
  celebrate?: boolean;
};

export function StatusBadge({ label, tone, celebrate }: StatusBadgeProps) {
  return <span className={`status-badge status-badge--${tone}${celebrate ? ' status-badge--celebrate' : ''}`}>{label}</span>;
}
