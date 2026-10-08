import { type ReactNode, useEffect, useState } from 'react';

/** Keep the body only through the closing animation, then release its DOM and effects. */
export function AnimatedDisclosure({ open, className, children }: {
  open: boolean;
  className: string;
  children: () => ReactNode;
}) {
  const [mounted, setMounted] = useState(open);
  useEffect(() => {
    if (open) {
      setMounted(true);
      return;
    }
    if (!mounted) return;
    // Reduced motion and hidden windows may not dispatch transitionend.
    const timer = window.setTimeout(() => setMounted(false), 400);
    return () => window.clearTimeout(timer);
  }, [open, mounted]);
  return (
    <div
      className={className}
      aria-hidden={!open}
      onTransitionEnd={(event) => {
        if (!open && event.target === event.currentTarget && event.propertyName === 'grid-template-rows') {
          setMounted(false);
        }
      }}
    >
      {open || mounted ? children() : null}
    </div>
  );
}
