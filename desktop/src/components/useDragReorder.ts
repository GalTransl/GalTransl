import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from 'react';
import { readProjectStorage, writeProjectStorage } from '../lib/projectStorage';
import { dragItemTransform, dragTargetIndex, moveKey, parseSavedOrder, type DragReorderState } from '../lib/dragReorder';

/** 按下后移动超过这个距离才算拖动；没移动就松开仍是普通点击。 */
const DRAG_START_DISTANCE = 4;
const DRAG_AUTOSCROLL_EDGE = 32;
const DRAG_AUTOSCROLL_STEP = 8;

/** 拖动结束后浏览器还会补发一次 click（WebView2 下可能晚于下一个宏任务才到），
 *  吞掉它以免误触展开/跳转；直到下一次按下（新手势）或超时才放行。 */
function suppressNextClick() {
  const swallow = (event: MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    release();
  };
  const release = () => {
    window.clearTimeout(timer);
    window.removeEventListener('click', swallow, { capture: true });
    window.removeEventListener('pointerdown', release, { capture: true });
  };
  const timer = window.setTimeout(release, 500);
  window.addEventListener('click', swallow, { capture: true });
  window.addEventListener('pointerdown', release, { capture: true });
}

/** 侧栏文件夹的按下拖动排序。主侧栏和两个 Agent 侧栏共用，手感保持一致。
 *  - handle：只有按在这些元素上才会拖（文件夹标题行）；ignore：标题行里的按钮等不触发拖动。
 *  - container：滚动容器，默认取被拖项的父元素；排列方向按它的 flex-direction 判断，
 *    窄屏下 Agent 侧栏变成横排时也能横向拖。 */
export function useDragReorder({ keys, onReorder, onStart, handle, ignore, container }: {
  keys: readonly string[];
  onReorder: (key: string, toIndex: number) => void;
  onStart?: () => void;
  handle: string;
  ignore?: string;
  container?: RefObject<HTMLElement>;
}) {
  const [drag, setDrag] = useState<DragReorderState | null>(null);
  const items = useRef(new Map<string, HTMLElement>());
  const cleanupRef = useRef<(() => void) | null>(null);
  const latest = useRef({ onReorder, onStart });
  latest.current = { onReorder, onStart };
  const keysSignature = keys.join('\n');

  // 列表在拖动中途变了（别处关了项目、会话刷新）就放弃这次拖动；卸载时也一样。
  useEffect(() => () => cleanupRef.current?.(), [keysSignature]);

  const itemRef = useCallback((key: string) => (node: HTMLElement | null) => {
    if (node) items.current.set(key, node);
    else items.current.delete(key);
  }, []);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>, key: string) => {
    if (event.button !== 0 || !event.isPrimary || keys.length < 2) return;
    const target = event.target as HTMLElement;
    if (!event.currentTarget.contains(target) || !target.closest(handle) || (ignore && target.closest(ignore))) return;
    const scroller = container?.current ?? event.currentTarget.parentElement;
    const order = [...keys];
    const fromIndex = order.indexOf(key);
    if (!scroller || fromIndex < 0) return;

    cleanupRef.current?.();
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startY = event.clientY;
    let pointerX = startX;
    let pointerY = startY;
    let axis: DragReorderState['axis'] = 'y';
    let startScroll = 0;
    let dragging = false;
    let mids: number[] = [];
    let shift = 0;
    let toIndex = fromIndex;
    let autoScrollFrame = 0;
    let prevUserSelect = '';

    const scrollPos = () => (axis === 'x' ? scroller.scrollLeft : scroller.scrollTop);
    const pointerPos = () => (axis === 'x' ? pointerX : pointerY);

    const update = () => {
      const offset = pointerPos() - (axis === 'x' ? startX : startY) + (scrollPos() - startScroll);
      toIndex = dragTargetIndex(mids, fromIndex, mids[fromIndex] + offset);
      setDrag({ key, fromIndex, toIndex, offset, shift, axis });
    };

    const autoScroll = () => {
      autoScrollFrame = 0;
      const rect = scroller.getBoundingClientRect();
      const [low, high] = axis === 'x' ? [rect.left, rect.right] : [rect.top, rect.bottom];
      const pos = pointerPos();
      const delta = pos < low + DRAG_AUTOSCROLL_EDGE ? -DRAG_AUTOSCROLL_STEP
        : pos > high - DRAG_AUTOSCROLL_EDGE ? DRAG_AUTOSCROLL_STEP : 0;
      if (!delta) return;
      const before = scrollPos();
      if (axis === 'x') scroller.scrollLeft += delta;
      else scroller.scrollTop += delta;
      if (scrollPos() === before) return;
      update();
      autoScrollFrame = window.requestAnimationFrame(autoScroll);
    };

    const begin = () => {
      const rects = order.map((item) => items.current.get(item)?.getBoundingClientRect());
      if (rects.some((rect) => !rect)) {
        cleanup();
        return;
      }
      const style = getComputedStyle(scroller);
      axis = style.flexDirection.startsWith('row') ? 'x' : 'y';
      startScroll = scrollPos();
      mids = rects.map((rect) => (axis === 'x' ? rect!.left + rect!.width / 2 : rect!.top + rect!.height / 2));
      const gap = parseFloat(axis === 'x' ? style.columnGap : style.rowGap) || 0;
      shift = (axis === 'x' ? rects[fromIndex]!.width : rects[fromIndex]!.height) + gap;
      dragging = true;
      prevUserSelect = document.body.style.userSelect;
      document.body.style.userSelect = 'none';
      window.getSelection()?.removeAllRanges();
      scroller.setAttribute('data-reordering', '');
      latest.current.onStart?.();
      update();
    };

    const handleMove = (e: PointerEvent) => {
      if (e.pointerId !== pointerId) return;
      pointerX = e.clientX;
      pointerY = e.clientY;
      if (!dragging) {
        if (Math.hypot(e.clientX - startX, e.clientY - startY) < DRAG_START_DISTANCE) return;
        begin();
        if (!dragging) return;
      }
      e.preventDefault();
      update();
      if (!autoScrollFrame) autoScrollFrame = window.requestAnimationFrame(autoScroll);
    };

    const handleUp = (e: PointerEvent) => {
      if (e.pointerId !== pointerId) return;
      const wasDragging = dragging;
      cleanup();
      if (!wasDragging) return;
      suppressNextClick();
      if (toIndex !== fromIndex) latest.current.onReorder(key, toIndex);
    };

    const handleCancel = (e: PointerEvent) => {
      if (e.pointerId === pointerId) cleanup();
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && dragging) {
        e.preventDefault();
        cleanup();
        suppressNextClick();
      }
    };

    const handleScroll = () => {
      if (dragging) update();
    };

    const blockNativeDrag = (e: DragEvent) => e.preventDefault();

    function cleanup() {
      window.cancelAnimationFrame(autoScrollFrame);
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleCancel);
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('dragstart', blockNativeDrag);
      scroller!.removeEventListener('scroll', handleScroll);
      if (dragging) {
        document.body.style.userSelect = prevUserSelect;
        scroller!.removeAttribute('data-reordering');
        dragging = false;
      }
      setDrag(null);
      if (cleanupRef.current === cleanup) cleanupRef.current = null;
    }

    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleCancel);
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('dragstart', blockNativeDrag);
    scroller.addEventListener('scroll', handleScroll);
    cleanupRef.current = cleanup;
    // 依赖用 keysSignature 而不是 keys：keys 数组每次渲染都可能是新的，内容没变就不必重建
  }, [keysSignature, handle, ignore, container]);

  const itemStyle = (index: number) => (drag ? { transform: dragItemTransform(drag, index) } : undefined);

  return { drag, itemRef, onPointerDown, itemStyle };
}

/** Agent 侧栏里用户拖出来的文件夹顺序，存在本地（和折叠状态一样，不进后端）。 */
export function useSavedOrder(storageKey: string) {
  const [order, setOrder] = useState<string[]>(() => parseSavedOrder(readProjectStorage(storageKey)));
  const reorder = useCallback((current: readonly string[], key: string, toIndex: number) => {
    const next = moveKey(current, key, toIndex);
    setOrder(next);
    writeProjectStorage(storageKey, JSON.stringify(next));
  }, [storageKey]);
  return { order, reorder };
}
