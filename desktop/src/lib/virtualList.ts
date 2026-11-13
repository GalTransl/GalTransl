import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';

/**
 * 只渲染可视区附近的条目，其余用上下两个占位块撑出滚动高度。
 *
 * 浏览文本动辄几千句，每句一张带输入框和折叠动画的卡片；全量挂载时 DOM 有十万级节点，
 * 打开文件、回到页面、改一个字都要整表协调与重算样式，整个程序都会跟着发卡。
 * content-visibility 只能省掉排版绘制，省不掉节点本身，所以大列表改为窗口化渲染。
 */

export type VirtualRange = { start: number; end: number };

/** offsets[i] 是第 i 项顶边到列表顶的距离（含之前每项后面的 gap），offsets[count] 是总长。 */
export function computeOffsets(keys: readonly string[], heights: ReadonlyMap<string, number>, estimate: number, gap: number): number[] {
  const offsets = new Array<number>(keys.length + 1);
  offsets[0] = 0;
  for (let i = 0; i < keys.length; i += 1) {
    offsets[i + 1] = offsets[i] + (heights.get(keys[i]) ?? estimate) + gap;
  }
  return offsets;
}

/** 第一个底边越过 y 的项（二分）。 */
function firstEndingAfter(offsets: readonly number[], y: number): number {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (offsets[mid + 1] > y) high = mid;
    else low = mid + 1;
  }
  return low;
}

export function pickRange(offsets: readonly number[], top: number, height: number, overscan: number, minItems: number): VirtualRange {
  const count = offsets.length - 1;
  if (count <= 0) return { start: 0, end: 0 };
  const start = Math.min(firstEndingAfter(offsets, Math.max(0, top - overscan)), count - 1);
  const end = Math.max(firstEndingAfter(offsets, top + height + overscan) + 1, start + minItems);
  return { start, end: Math.min(end, count) };
}

/** 当前窗口是否还盖得住视口上下各 margin；盖得住就不重渲染，滚动时只有越界才换窗口。 */
export function rangeCovers(range: VirtualRange, offsets: readonly number[], top: number, height: number, margin: number): boolean {
  const count = offsets.length - 1;
  if (range.end > count || range.start >= range.end && count > 0) return false;
  if (range.start > 0 && offsets[range.start] > Math.max(0, top - margin)) return false;
  if (range.end < count && offsets[range.end] < top + height + margin) return false;
  return true;
}

const ESTIMATE = 96;
const OVERSCAN = 1200;
const MIN_ITEMS = 30;

export function useVirtualList({ containerRef, keys, enabled, resetKey, keyAttribute }: {
  containerRef: RefObject<HTMLElement | null>;
  /** 每项的稳定标识，与渲染出的元素上 keyAttribute 的值一致。 */
  keys: readonly string[];
  enabled: boolean;
  /** 换文件时清掉量过的高度：不同文件里同一个序号的句子高度无关。 */
  resetKey: string;
  keyAttribute: string;
}) {
  const heightsRef = useRef(new Map<string, number>());
  const heightsVersionRef = useRef(0);
  const resetKeyRef = useRef(resetKey);
  if (resetKeyRef.current !== resetKey) {
    resetKeyRef.current = resetKey;
    heightsRef.current = new Map();
    heightsVersionRef.current += 1;
  }
  const gapRef = useRef(0);
  const keysRef = useRef(keys);
  keysRef.current = keys;
  const offsetsCacheRef = useRef<{ keys: readonly string[]; version: number; gap: number; offsets: number[] } | null>(null);
  const getOffsets = useCallback(() => {
    const cached = offsetsCacheRef.current;
    const currentKeys = keysRef.current;
    if (cached && cached.keys === currentKeys && cached.version === heightsVersionRef.current && cached.gap === gapRef.current) return cached.offsets;
    const offsets = computeOffsets(currentKeys, heightsRef.current, ESTIMATE, gapRef.current);
    offsetsCacheRef.current = { keys: currentKeys, version: heightsVersionRef.current, gap: gapRef.current, offsets };
    return offsets;
  }, []);

  const [range, setRange] = useState<VirtualRange>({ start: 0, end: 0 });
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const updateRange = useCallback(() => {
    const container = containerRef.current;
    if (!enabledRef.current || !container) return;
    const offsets = getOffsets();
    const top = container.scrollTop;
    const height = container.clientHeight;
    setRange((current) => rangeCovers(current, offsets, top, height, OVERSCAN / 4)
      ? current
      : pickRange(offsets, top, height, OVERSCAN, MIN_ITEMS));
  }, [containerRef, getOffsets]);

  // 列表内容变了（换文件、过滤、增删）先按当前滚动位置校正窗口，再绘制。
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (enabled && container && typeof getComputedStyle === 'function') {
      gapRef.current = Number.parseFloat(getComputedStyle(container).rowGap) || 0;
    }
    updateRange();
  }, [containerRef, enabled, keys, resetKey, updateRange]);

  // 视口尺寸变化（窗口缩放、页面从 display:none 回来）也要重算窗口。
  useEffect(() => {
    const container = containerRef.current;
    if (!enabled || !container || typeof ResizeObserver !== 'function') return;
    const observer = new ResizeObserver(() => updateRange());
    observer.observe(container);
    return () => observer.disconnect();
  }, [containerRef, enabled, updateRange]);

  // 量出已渲染项的真实高度，供它们离开窗口后折算进占位块。窗口内的高度变化
  // （展开卡片、动画）不影响占位块，所以这里只记数，不触发重渲染。
  const itemObserverRef = useRef<ResizeObserver | null>(null);
  const observedRef = useRef(new Set<Element>());
  useEffect(() => () => {
    itemObserverRef.current?.disconnect();
    itemObserverRef.current = null;
    observedRef.current.clear();
  }, []);
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!enabled || !container || typeof ResizeObserver !== 'function') return;
    if (!itemObserverRef.current) {
      itemObserverRef.current = new ResizeObserver((records) => {
        let changed = false;
        for (const record of records) {
          const element = record.target as HTMLElement;
          if (!element.isConnected) continue;
          const key = element.getAttribute(keyAttribute);
          if (key === null) continue;
          const height = record.borderBoxSize?.[0]?.blockSize ?? element.offsetHeight;
          if (Math.abs((heightsRef.current.get(key) ?? -1) - height) > 0.5) {
            heightsRef.current.set(key, height);
            changed = true;
          }
        }
        if (changed) heightsVersionRef.current += 1;
      });
    }
    const observer = itemObserverRef.current;
    const observed = observedRef.current;
    for (const element of observed) {
      if (!element.isConnected) {
        observer.unobserve(element);
        observed.delete(element);
      }
    }
    for (const element of Array.from(container.children)) {
      if (!element.hasAttribute(keyAttribute) || observed.has(element)) continue;
      observer.observe(element);
      observed.add(element);
    }
  });

  /** 把第 position 项挪进窗口并大致居中；真正对准由调用方在下一次提交后做。 */
  const reveal = useCallback((position: number) => {
    const container = containerRef.current;
    if (!container) return;
    const offsets = getOffsets();
    const top = Math.max(0, offsets[position] - container.clientHeight / 2);
    container.scrollTop = top;
    setRange(pickRange(offsets, top, container.clientHeight, OVERSCAN, MIN_ITEMS));
  }, [containerRef, getOffsets]);

  if (!enabled) {
    return { start: 0, end: keys.length, topSpacer: 0, bottomSpacer: 0, onScroll: updateRange, reveal };
  }
  const offsets = getOffsets();
  const count = keys.length;
  const start = Math.min(range.start, count);
  const end = Math.min(Math.max(range.end, start), count);
  const gap = gapRef.current;
  // 占位块本身也是 flex 子项，会和相邻卡片之间多出一个 gap，这里扣掉。
  const topSpacer = start > 0 ? Math.max(0, offsets[start] - gap) : 0;
  const bottomSpacer = end < count ? Math.max(0, offsets[count] - offsets[end] - gap) : 0;
  return { start, end, topSpacer, bottomSpacer, onScroll: updateRange, reveal };
}
