import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Routes, type Location } from 'react-router-dom';
import { PageActivityContext, PageRetentionContext } from './PageActivity';

export const RECENT_PAGE_LIMIT = 10;

type Entry = { location: Location };

function CachedPage({ entry, visible, active, retain, children }: {
  entry: Entry;
  visible: boolean;
  active: boolean;
  retain: (key: string) => () => void;
  children: ReactNode;
}) {
  const key = entry.location.pathname;
  const register = useCallback(() => retain(key), [retain, key]);
  const scrollPositions = useRef(new Map<HTMLElement, { top: number; left: number }>());
  useLayoutEffect(() => {
    if (!visible) return;
    for (const [element, position] of scrollPositions.current) {
      if (!element.isConnected) {
        scrollPositions.current.delete(element);
        continue;
      }
      element.scrollTop = position.top;
      element.scrollLeft = position.left;
    }
  }, [visible]);
  return (
    <div
      className="app-layout__page"
      hidden={!visible}
      style={!visible ? { display: 'none' } : undefined}
      onScrollCapture={(event) => {
        const element = event.target as HTMLElement;
        // WebViews can reset scroll offsets on display:none. Ignore those events;
        // live chat scrollers manage their own following/reading position.
        if (visible && !element.hasAttribute('data-page-scroll-managed')) {
          scrollPositions.current.set(element, { top: element.scrollTop, left: element.scrollLeft });
        }
      }}
    >
      <PageActivityContext.Provider value={active}>
        <PageRetentionContext.Provider value={register}>
          {/* Each mounted route owns its location/params, including while hidden. */}
          <Routes location={entry.location}>{children}</Routes>
        </PageRetentionContext.Provider>
      </PageActivityContext.Provider>
    </div>
  );
}

export function CachedRoutes({ location, currentPath, children, isAvailable, limit = RECENT_PAGE_LIMIT }: {
  location: Location;
  currentPath: string;
  children: ReactNode;
  isAvailable: (pathname: string) => boolean;
  limit?: number;
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [retained, setRetained] = useState<ReadonlyMap<string, number>>(() => new Map());
  const retain = useCallback((key: string) => {
    setRetained((prev) => new Map(prev).set(key, (prev.get(key) || 0) + 1));
    return () => setRetained((prev) => {
      const next = new Map(prev);
      const count = (next.get(key) || 1) - 1;
      if (count) next.set(key, count);
      else next.delete(key);
      return next;
    });
  }, []);

  const next = useMemo(() => {
    // The creation wizard has its own lifetime and is not a recent-page entry.
    const cacheCurrent = location.pathname !== '/new-project';
    const kept = entries.filter((entry) =>
      (entry.location.pathname === location.pathname && cacheCurrent)
      || (!/^\/project\/[^/]+\/?$/.test(entry.location.pathname) && isAvailable(entry.location.pathname)),
    );
    const previous = kept.find((entry) => entry.location.pathname === location.pathname);
    // Only navigation changes recency; a background task finishing must not touch it.
    if (cacheCurrent && previous?.location !== location) {
      const index = previous ? kept.indexOf(previous) : -1;
      if (index >= 0) kept.splice(index, 1);
      kept.push({ location });
    }
    const recent = new Set(kept.slice(-Math.max(1, limit)));
    return kept.filter((entry) => recent.has(entry) || retained.has(entry.location.pathname));
  }, [entries, location, isAvailable, limit, retained]);

  // Reconcile before rendering children so new pages never mount twice and stale
  // project routes disappear in the same commit as a project is closed.
  if (next.length !== entries.length || next.some((entry, i) => entry !== entries[i])) {
    setEntries(next);
  }

  return next.map((entry) => {
    const key = entry.location.pathname;
    const visible = key === location.pathname;
    return (
      <CachedPage key={key} entry={entry} visible={visible} active={visible && key === currentPath} retain={retain}>
        {children}
      </CachedPage>
    );
  });
}
