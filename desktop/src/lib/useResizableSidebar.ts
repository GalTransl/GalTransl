import { useCallback, useRef, useState, useSyncExternalStore, type PointerEvent as ReactPointerEvent } from 'react';

// 「浏览文本」和字典页的文件栏默认一样宽；用户拖过之后按 storageKey 各记各的。
// 同一个 key 的多个实例（项目字典页和通用字典页、后台保活的页面）共享一份宽度，拖一边另一边立即跟上。
const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 560;
const SIDEBAR_DEFAULT = 240;

const widths = new Map<string, number>();
const listeners = new Map<string, Set<() => void>>();

function readWidth(key: string): number {
  const cached = widths.get(key);
  if (cached !== undefined) return cached;
  let width = SIDEBAR_DEFAULT;
  try {
    const stored = Number(localStorage.getItem(key));
    if (Number.isFinite(stored) && stored >= SIDEBAR_MIN && stored <= SIDEBAR_MAX) width = stored;
  } catch {}
  widths.set(key, width);
  return width;
}

function setWidth(key: string, width: number) {
  widths.set(key, width);
  listeners.get(key)?.forEach((listener) => listener());
}

/** 可拖拽的文件侧栏宽度：layoutRef 挂在外框上（按它的左边缘算宽度），onResizerPointerDown 挂在分隔线上 */
export function useResizableSidebar<T extends HTMLElement = HTMLDivElement>(storageKey: string) {
  const subscribe = useCallback((listener: () => void) => {
    let set = listeners.get(storageKey);
    if (!set) listeners.set(storageKey, set = new Set());
    set.add(listener);
    return () => { set.delete(listener); };
  }, [storageKey]);
  const getSnapshot = useCallback(() => readWidth(storageKey), [storageKey]);
  const width = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const [resizing, setResizing] = useState(false);
  const layoutRef = useRef<T | null>(null);
  const onResizerPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
    const layoutEl = layoutRef.current;
    if (!layoutEl) return;
    setResizing(true);
    const onMove = (ev: PointerEvent) => {
      const rect = layoutEl.getBoundingClientRect();
      setWidth(storageKey, Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, ev.clientX - rect.left)));
    };
    const onUp = () => {
      setResizing(false);
      try { localStorage.setItem(storageKey, String(readWidth(storageKey))); } catch {}
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }, [storageKey]);
  return { width, resizing, layoutRef, onResizerPointerDown };
}
