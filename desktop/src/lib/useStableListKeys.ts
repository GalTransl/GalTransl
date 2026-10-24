import { useId, useRef } from 'react';

/** View-only identities: retain moved rows and edits without serializing IDs to config. */
export function useStableListKeys<T>(items: readonly T[]): string[] {
  const prefix = useId();
  const nextId = useRef(0);
  const previous = useRef<{ items: readonly T[]; keys: string[] }>({ items: [], keys: [] });
  if (previous.current.items === items) return previous.current.keys;
  const old = previous.current;
  const available = new Map<T, number[]>();
  old.items.forEach((item, index) => available.set(item, [...(available.get(item) ?? []), index]));
  const used = new Set<number>();
  const matches = items.map((item) => {
    const index = available.get(item)?.shift();
    if (index !== undefined) used.add(index);
    return index;
  });
  const keys = matches.map((match, index) => {
    // A controlled edit replaces its object; keep the same row when length is unchanged.
    if (match === undefined && items.length === old.items.length && !used.has(index)) {
      match = index;
      used.add(index);
    }
    return match === undefined ? `${prefix}-${nextId.current++}` : old.keys[match];
  });
  previous.current = { items, keys };
  return keys;
}
