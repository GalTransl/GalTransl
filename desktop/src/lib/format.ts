import type { Job } from './api';

const formatter = new Intl.DateTimeFormat('zh-CN', {
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  month: '2-digit',
  second: '2-digit',
  year: 'numeric',
});

export function formatTimestamp(value: string) {
  if (!value) {
    return '—';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return formatter.format(parsed);
}

export function formatJobResult(job: Job) {
  if (job.status === 'completed' && job.success) {
    return 'Completed successfully';
  }

  if (job.status === 'failed') {
    return 'Failed';
  }

  if (job.status === 'cancelled') {
    return 'Cancelled';
  }

  return 'In progress';
}
