import { useEffect, useState } from 'react';
import { usePageActive } from '../components/PageActivity';
import { usePrefersReducedMotion } from './motion';

/** Both animation libraries follow the app setting and cached-page lifecycle. */
export function useUiAnimations(active = true) {
  const pageActive = usePageActive();
  const reduced = usePrefersReducedMotion();
  const [visible, setVisible] = useState(() => typeof document !== 'undefined' && !document.hidden);
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const sync = () => setVisible(!document.hidden);
    sync();
    document.addEventListener('visibilitychange', sync);
    return () => document.removeEventListener('visibilitychange', sync);
  }, []);
  return active && pageActive && visible && !reduced;
}
