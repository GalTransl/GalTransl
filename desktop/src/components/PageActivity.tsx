import { createContext, useContext, useLayoutEffect } from 'react';

export const PageActivityContext = createContext(true);
export const PageRetentionContext = createContext<(() => () => void) | null>(null);

/** Hidden cached pages keep their state, but must suspend view-only effects. */
export function usePageActive() {
  return useContext(PageActivityContext);
}

/** Keep unfinished work alive even when the page falls outside the recent-page limit. */
export function useRetainPage(retain: boolean) {
  const register = useContext(PageRetentionContext);
  useLayoutEffect(() => {
    if (retain) return register?.();
  }, [register, retain]);
}
