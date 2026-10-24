import { AnimatePresence, LazyMotion, MotionConfig, domAnimation, useIsPresent } from 'motion/react';
import * as m from 'motion/react-m';
import type { ReactNode } from 'react';
import { DUR } from '../lib/motion';
import { useUiAnimations } from '../lib/useUiAnimations';

export const UI_TRANSITION = { duration: DUR.fast / 1000, ease: [0.2, 0, 0, 1] as const };

export function UiMotionProvider({ children }: { children: ReactNode }) {
  return <LazyMotion features={domAnimation} strict>{children}</LazyMotion>;
}

type CollapseContent = ReactNode | (() => ReactNode);
const renderContent = (children: CollapseContent) => typeof children === 'function' ? children() : children;

function CollapseBody({ children, className, animated, id }: { children: CollapseContent; className?: string; animated: boolean; id?: string }) {
  const present = useIsPresent();
  return <m.div id={id} className={className} initial={animated ? { height: 0, opacity: 0 } : false}
    animate={{ height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }}
    exit={{ height: 0, opacity: 0, overflow: 'hidden' }} style={{ overflow: 'hidden' }}
    aria-hidden={!present || undefined} {...(!present ? { inert: '' } : {})}>
    {renderContent(children)}
  </m.div>;
}

export function AnimatedCollapse({ open, children, className, contentKey = 'body', keepMounted = false, id }: {
  open: boolean; children: CollapseContent; className?: string; contentKey?: string | number; keepMounted?: boolean; id?: string;
}) {
  const enabled = useUiAnimations();
  return <MotionConfig reducedMotion="never" transition={enabled ? UI_TRANSITION : { duration: 0 }}>
    {keepMounted ? <m.div id={id} className={className} initial={false}
      animate={open ? { height: 'auto', opacity: 1, transitionEnd: { overflow: 'visible' } }
        : { height: 0, opacity: 0, overflow: 'hidden' }}
      style={{ overflow: 'hidden' }}
      aria-hidden={!open || undefined} {...(!open ? { inert: '' } : {})}>
      {renderContent(children)}
    </m.div> : <AnimatePresence initial={false}>
      {open && <CollapseBody key={contentKey} id={id} className={className} animated={enabled}>{children}</CollapseBody>}
    </AnimatePresence>}
  </MotionConfig>;
}

export { AnimatePresence, MotionConfig, useIsPresent, m };
