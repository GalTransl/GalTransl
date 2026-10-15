import { useLayoutEffect, useRef } from 'react';

const BOTTOM_THRESHOLD_PX = 24;

/** Native smooth scrolling, with no animation loop while the stream is idle. */
export function createSuccessTail(viewport: HTMLElement) {
  let following = true;
  let active = false;
  let initialized = false;
  let latestId: string | undefined;
  let lastTop = viewport.scrollTop;
  const nearBottom = () => viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= BOTTOM_THRESHOLD_PX;
  const scroll = (top: number, behavior: ScrollBehavior = 'auto') => {
    lastTop = viewport.scrollTop;
    viewport.scrollTo({ top, behavior });
    if (behavior !== 'smooth') lastTop = viewport.scrollTop;
  };
  const onScroll = () => {
    if (!active) return;
    const top = viewport.scrollTop;
    // Intermediate events from smooth scrolling must not turn following off.
    if (!following || top < lastTop - 0.5) following = nearBottom();
    lastTop = top;
  };
  const interrupt = () => {
    if (!active) return;
    following = nearBottom();
    scroll(viewport.scrollTop); // Cancel any native smooth scroll immediately.
  };
  const onWheel = (event: WheelEvent) => {
    if (event.deltaY < 0 && active) {
      interrupt();
      following = false;
    }
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey)) {
      interrupt();
      following = false;
    }
  };
  viewport.addEventListener('scroll', onScroll, { passive: true });
  viewport.addEventListener('wheel', onWheel, { passive: true });
  viewport.addEventListener('pointerdown', interrupt, { passive: true });
  viewport.addEventListener('keydown', onKeyDown);

  return {
    update(nextLatestId: string | undefined, nextActive: boolean) {
      const wasActive = active;
      const changed = nextLatestId !== latestId;
      latestId = nextLatestId;
      active = nextActive;
      if (!active) return;
      if (!initialized || !wasActive) {
        scroll(following ? viewport.scrollHeight : lastTop);
        initialized = true;
        return;
      }
      // Removing old rows at the 100-card limit changes scrollTop through native
      // scroll anchoring. Read that position before handling queued scroll events.
      lastTop = viewport.scrollTop;
      if (changed && following) scroll(viewport.scrollHeight, 'smooth');
    },
    jump() {
      following = true;
      if (active) scroll(viewport.scrollHeight);
    },
    dispose() {
      viewport.removeEventListener('scroll', onScroll);
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('pointerdown', interrupt);
      viewport.removeEventListener('keydown', onKeyDown);
    },
  };
}

export function useSuccessTail(scope: string, entries: readonly { id: string }[], active: boolean) {
  const ref = useRef<HTMLDivElement | null>(null);
  const controller = useRef<ReturnType<typeof createSuccessTail> | null>(null);
  const hasEntries = entries.length > 0;
  useLayoutEffect(() => {
    if (!ref.current) return;
    const tail = createSuccessTail(ref.current);
    controller.current = tail;
    return () => {
      tail.dispose();
      controller.current = null;
    };
  }, [scope, hasEntries]);
  useLayoutEffect(() => {
    controller.current?.update(entries[entries.length - 1]?.id, active);
  }, [scope, entries, active]);
  return { ref, jumpToBottom: () => controller.current?.jump() };
}
