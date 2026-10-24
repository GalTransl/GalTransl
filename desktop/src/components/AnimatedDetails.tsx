import { useId, useState, type ReactNode } from 'react';
import { AnimatedCollapse } from './UiMotion';

/** A keyboard-accessible disclosure that retains form drafts while closed. */
export function AnimatedDetails({ summary, children, className, summaryClassName = '', defaultOpen = false }: {
  summary: ReactNode; children: ReactNode; className?: string; summaryClassName?: string; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  return <div className={className} data-open={open}>
    <button type="button" className={`animated-details__summary ${summaryClassName}`.trim()}
      aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(value => !value)}>
      {summary}
    </button>
    <AnimatedCollapse open={open} keepMounted id={bodyId}>
      <div className="animated-details__content">{children}</div>
    </AnimatedCollapse>
  </div>;
}
