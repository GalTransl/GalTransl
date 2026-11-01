import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useUiAnimations } from '../lib/useUiAnimations';
import { m, MotionConfig, UI_TRANSITION, useIsPresent } from './UiMotion';

/** Mount inside AnimatePresence so the closing animation can finish. */
export function AnimatedDialog({ children, className, backdropClassName, labelledBy, close }: {
  children: ReactNode;
  className: string;
  backdropClassName: string;
  labelledBy: string;
  close: () => void;
}) {
  const animated = useUiAnimations();
  const present = useIsPresent();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(close);
  useEffect(() => { closeRef.current = close; }, [close]);
  useEffect(() => {
    if (!present) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex="0"]') || [])
        .filter(element => !element.hasAttribute('disabled') && element.getClientRects().length > 0);
      const next = event.shiftKey ? controls[controls.length - 1] : controls[0];
      const edge = event.shiftKey ? controls[0] : controls[controls.length - 1];
      if (document.activeElement === edge || !dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        next?.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previousFocus?.focus();
    };
  }, [present]);

  return <MotionConfig reducedMotion="never" transition={animated ? UI_TRANSITION : { duration: 0 }}>
    {createPortal(<m.div className={backdropClassName}
      initial={animated ? { opacity: 0 } : false} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      style={{ pointerEvents: present ? 'auto' : 'none' }}
      aria-hidden={!present || undefined} {...(!present ? { inert: '' } : {})}
      onClick={event => { if (present && event.target === event.currentTarget) close(); }}>
      <m.section ref={dialogRef} className={className} role={present ? 'dialog' : undefined} aria-modal={present ? 'true' : undefined} aria-labelledby={labelledBy}
        initial={animated ? { opacity: 0, scale: 0.96, y: 10 } : false}
        animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96, y: 8 }}>
        {children}
      </m.section>
    </m.div>, document.body)}
  </MotionConfig>;
}
