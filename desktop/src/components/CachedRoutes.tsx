import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Routes, type Location } from 'react-router-dom';
import { PageActivityContext, PageRetentionContext } from './PageActivity';

export const RECENT_PAGE_LIMIT = 5;

type Entry = { location: Location };

const CachedPage = memo(function CachedPage({ entry, visible, active, leaving, retain, children }: {
  entry: Entry;
  visible: boolean;
  /** Page is visible but on its way out (cross-fade): render on top, inert. */
  leaving: boolean;
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
      className={`app-layout__page${leaving
        ? ' app-layout__page--leaving'
        : visible
          ? ' app-layout__page--entering'
          : ''}`}
      hidden={!visible}
      aria-hidden={!visible || leaving}
      {...(leaving ? { inert: '' } : {})}
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
});

export function CachedRoutes({ location, currentPath, leavingPath = null, children, isAvailable, limit = RECENT_PAGE_LIMIT }: {
  location: Location;
  currentPath: string;
  /** Pathname of the outgoing page during a cross-fade, if any. */
  leavingPath?: string | null;
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
      // A navigation can change state/key without changing the URL. Always
      // publish its location; hidden entries still keep their own identity.
      kept.push({ location });
    }
    const recent = new Set(kept.slice(-Math.max(1, limit)));
    // Retention protects unsaved drafts and ongoing workflows. They may only
    // be evicted once released or explicitly closed, regardless of the limit.
    return kept.filter((entry) => recent.has(entry)
      || retained.has(entry.location.pathname)
      || entry.location.pathname === leavingPath);
  }, [entries, location, isAvailable, limit, retained, leavingPath]);

  // Reconcile before rendering children so new pages never mount twice and stale
  // project routes disappear in the same commit as a project is closed.
  if (next.length !== entries.length || next.some((entry, i) => entry !== entries[i])) {
    setEntries(next);
  }

  return (
    <div className="cached-routes">
      {next.map((entry) => {
        const key = entry.location.pathname;
        const leaving = leavingPath !== null && key === leavingPath && key !== location.pathname;
        const visible = key === location.pathname || leaving;
        return (
          <CachedPage
            key={key}
            entry={entry}
            visible={visible}
            leaving={leaving}
            active={visible && !leaving && key === currentPath}
            retain={retain}
          >
            {children}
          </CachedPage>
        );
      })}
    </div>
  );
}
