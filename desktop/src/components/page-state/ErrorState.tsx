import type { ReactNode } from 'react';
import { InlineFeedback } from './InlineFeedback';

type ErrorStateProps = {
  title?: string;
  description: ReactNode;
  action?: ReactNode;
  className?: string;
};

export function ErrorState({
  title = '加载失败',
  description,
  action,
  className,
}: ErrorStateProps) {
  return (
    <InlineFeedback
      tone="error"
      title={title}
      description={description}
      action={action}
      className={className}
    />
  );
}
