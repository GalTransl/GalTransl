import type { CacheEntry } from './api';
import { filterProblemText } from './problemFilter';

/** A filter instance belongs to one set of rules. Unedited entries retain
 *  their view objects so card memoization also works with problem filters. */
export function createCacheProblemFilter(keys: string[]): (entry: CacheEntry) => CacheEntry {
  const views = new WeakMap<CacheEntry, CacheEntry>();
  return (entry) => {
    if (keys.length === 0) return entry;
    const cached = views.get(entry);
    if (cached) return cached;
    const problem = filterProblemText(entry.problem, keys);
    const view = problem === (entry.problem || '') ? entry : { ...entry, problem };
    views.set(entry, view);
    return view;
  };
}

/** Compare cache contents without serializing the whole file. Deleted false
 *  and an absent deleted flag both mean the entry is present. */
export function entriesMatch(left: CacheEntry[], right: CacheEntry[]): boolean {
  if (left.length !== right.length) return false;
  for (let i = 0; i < left.length; i += 1) {
    if (!entryMatches(left[i], right[i])) return false;
  }
  return true;
}

function entryMatches(a: CacheEntry, b: CacheEntry): boolean {
  if (a === b) return true;
  if (!!a.deleted !== !!b.deleted) return false;
  const aKeys = Object.keys(a).filter((key) => key !== 'deleted');
  if (aKeys.length !== Object.keys(b).filter((key) => key !== 'deleted').length) return false;
  for (const key of aKeys) {
    if (!Object.prototype.hasOwnProperty.call(b, key)) return false;
    const av = a[key as keyof CacheEntry];
    const bv = b[key as keyof CacheEntry];
    if (Array.isArray(av) || Array.isArray(bv)) {
      if (!Array.isArray(av) || !Array.isArray(bv) || av.length !== bv.length) return false;
      for (let j = 0; j < av.length; j += 1) if (av[j] !== bv[j]) return false;
    } else if (av !== bv) {
      return false;
    }
  }
  return true;
}

/** Revalidate from disk while keeping unchanged cards memoized on page return. */
export function mergeCacheEntries(previous: CacheEntry[], incoming: CacheEntry[]): CacheEntry[] {
  const byIndex = new Map(previous.map(entry => [entry.index, entry]));
  const merged = incoming.map(entry => {
    const old = byIndex.get(entry.index);
    return old && entryMatches(old, entry) ? old : entry;
  });
  return merged.length === previous.length && merged.every((entry, i) => entry === previous[i]) ? previous : merged;
}
