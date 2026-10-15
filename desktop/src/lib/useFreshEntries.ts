import { useEffect, useRef, useState } from 'react';
import { FRESH_HIGHLIGHT_MS } from './motion';

/** Track only the current snapshot, with deadlines that survive frequent polls. */
export function useFreshEntries(scope: string, entries: readonly { id: string }[] | undefined, active: boolean) {
  const state = useRef({ scope, seen: null as Set<string> | null, expires: new Map<string, number>() });
  const [fresh, setFresh] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    if (state.current.scope !== scope) {
      state.current = { scope, seen: null, expires: new Map() };
    }
    const current = state.current;
    const ids = new Set(entries?.map(entry => entry.id));
    const now = Date.now();
    for (const id of current.expires.keys()) {
      if (!active || !ids.has(id)) current.expires.delete(id);
    }
    // Loading an existing history should not animate every historical card.
    if (active && current.seen !== null) {
      for (const id of ids) {
        if (!current.seen.has(id)) current.expires.set(id, now + FRESH_HIGHLIGHT_MS);
      }
    }
    if (ids.size || current.seen !== null) current.seen = ids;

    let timer: number | undefined;
    const publish = () => {
      const now = Date.now();
      let nextDeadline = Infinity;
      for (const [id, deadline] of current.expires) {
        if (deadline <= now) current.expires.delete(id);
        else nextDeadline = Math.min(nextDeadline, deadline);
      }
      setFresh(previous => previous.size === current.expires.size
        && [...previous].every(id => current.expires.has(id))
        ? previous : new Set(current.expires.keys()));
      if (nextDeadline !== Infinity) {
        timer = window.setTimeout(publish, nextDeadline - now);
      }
    };
    publish();
    return () => window.clearTimeout(timer);
  }, [scope, entries, active]);

  return fresh;
}
