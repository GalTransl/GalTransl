import type { JobStatus } from '../lib/api';

type StatusBadgeTone = JobStatus | 'online' | 'offline' | 'connecting';

type StatusBadgeProps = {
  label: string;
  tone: StatusBadgeTone;
};

export function StatusBadge({ label, tone }: StatusBadgeProps) {
  return <span className={`status-badge status-badge--${tone}`}>{label}</span>;
}
