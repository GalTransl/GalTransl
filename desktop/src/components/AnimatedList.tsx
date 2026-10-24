import { Children, useLayoutEffect, useState, type HTMLAttributes } from 'react';
import autoAnimate from '@formkit/auto-animate';
import { DUR, EASE } from '../lib/motion';
import { useUiAnimations } from '../lib/useUiAnimations';

/** Opt in only for small lists; long editors and live output keep their own positioning. */
export function AnimatedList({ active = true, children, className = '', ...props }: HTMLAttributes<HTMLDivElement> & { active?: boolean }) {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const enabled = useUiAnimations(active && Children.count(children) <= 40);
  useLayoutEffect(() => {
    if (!element || !enabled) return;
    const controller = autoAnimate(element, {
      duration: DUR.fast, easing: EASE.decel,
      // The existing desktop preference is controlled by the app's animation switch.
      disrespectUserMotionPreference: true,
    });
    return () => { controller.disable(); controller.destroy?.(); };
  }, [element, enabled]);
  return <div {...props} ref={setElement} className={`animated-list ${className}`.trim()}>{children}</div>;
}
