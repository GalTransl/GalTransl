import type { ReadingPosition } from './readingMemory';

/** 跟随内容实际高度（包括折叠动画）；用户上滚或查看记录时让出滚动权。 */
export function followTail(viewport: HTMLElement, content: HTMLElement, onBottom: (value: boolean) => void, options: {
  initial?: ReadingPosition;
  active?: boolean;
  onPosition?: (position: ReadingPosition) => void;
} = {}) {
  let following = options.initial?.following ?? true;
  let active = options.active ?? true;
  let lastTop = options.initial?.scrollTop ?? viewport.scrollTop;
  // 缓存可能比权威历史短：高度不足时保留原目标，等后续布局恢复，不能记成被钳制的 0。
  let restoreTop: number | null = following ? null : lastTop;
  let disposed = false;
  let finalReply: HTMLElement | null = null;
  let userScrollPending = false;
  let draggingScrollbar = false;
  let touchY: number | null = null;
  let touchScrolling = false;
  const pointerRoot = viewport.ownerDocument ?? viewport;
  // 已上报过的跟随态 / 阅读位置：follow 会被 ResizeObserver 每帧调用，
  // 值没变就不该穿透到 React（setState）和阅读位置存储。
  let lastBottom: boolean | null = null;
  let lastRemembered = '';
  const notifyBottom = (value: boolean) => {
    if (value === lastBottom) return;
    lastBottom = value;
    onBottom(value);
  };
  const nearBottom = () => viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
  const remember = () => {
    const position = { scrollTop: restoreTop ?? lastTop, following };
    const key = `${position.scrollTop}|${position.following}`;
    if (key === lastRemembered) return;
    lastRemembered = key;
    options.onPosition?.(position);
  };
  const follow = () => {
    if (disposed || !active) return;
    if (finalReply?.isConnected === false) finalReply = null;
    if (finalReply) {
      // 折叠工作块时回复的实际位置逐帧变化，保持其开头在视口内。
      // 亚像素抖动不值得写一次 scrollTop：每次写入都会再派发一个 scroll 事件。
      const delta = finalReply.getBoundingClientRect().top
        - viewport.getBoundingClientRect().top - viewport.clientTop - 16;
      if (Math.abs(delta) >= 0.5) viewport.scrollTop += delta;
      lastTop = viewport.scrollTop;
      remember();
      return;
    }
    if (!following) {
      if (restoreTop !== null) {
        viewport.scrollTop = restoreTop;
        if (viewport.scrollHeight - viewport.clientHeight >= restoreTop) restoreTop = null;
      }
      lastTop = viewport.scrollTop;
      notifyBottom(nearBottom());
      remember();
      return;
    }
    // 已经在底部就不写：同值赋值不改变滚动位置，却仍会被当成一次滚动。
    const bottom = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
    if (viewport.scrollTop !== bottom) viewport.scrollTop = bottom;
    lastTop = viewport.scrollTop;
    notifyBottom(true);
    remember();
  };
  const onScroll = () => {
    if (!active) return;
    const top = viewport.scrollTop;
    // 自己写 scrollTop 引起的回声：位置与上次记下的一致，说明这次滚动不是用户产生的。
    // 折叠/流式时 follow 每帧写一次，这类事件占绝大多数——直接放行，既不重算跟随态，
    // 也不触发一次 React 更新。
    if (top === lastTop) return;
    lastTop = top;
    if (finalReply) {
      remember();
      return;
    }
    // 终态折叠会把 scrollTop 钳制变小，这不是用户上滚。只有真实输入才能改跟随意图。
    const near = nearBottom();
    if (userScrollPending || draggingScrollbar || touchScrolling) following = near;
    userScrollPending = false;
    notifyBottom(following || near);
    remember();
  };
  const releaseReply = () => { finalReply = null; };
  const userScroll = (up: boolean) => {
    if (!active) return;
    releaseReply();
    restoreTop = null;
    userScrollPending = true;
    // 输入先于 scroll/ResizeObserver：与 finish 同帧上滚时也必须由用户优先。
    if (up && viewport.scrollTop > 0) {
      following = false;
      notifyBottom(false);
    }
    lastTop = viewport.scrollTop;
    remember();
  };
  const onWheel = (event: WheelEvent) => {
    if (event.deltaY) userScroll(event.deltaY < 0);
  };
  const onPointerDown = (event: PointerEvent) => {
    if (!active || event.target !== viewport) return;
    draggingScrollbar = true;
    userScroll(true);
  };
  const onPointerUp = () => { draggingScrollbar = false; };
  const onTouchStart = (event: TouchEvent) => { touchY = event.touches[0]?.clientY ?? null; };
  const onTouchMove = (event: TouchEvent) => {
    const nextY = event.touches[0]?.clientY ?? null;
    if (nextY !== null && touchY !== null && nextY !== touchY) {
      touchScrolling = true;
      userScroll(nextY > touchY);
    }
    touchY = nextY;
  };
  const onTouchEnd = () => { touchY = null; touchScrolling = false; };
  const onDisclosureClick = (event: MouseEvent) => {
    if (!active || !(event.target as HTMLElement | null)?.closest?.('button[aria-expanded]')) return;
    // 主动查看记录时停止吸底，避免展开的工具行把触发器推出视口。
    releaseReply();
    restoreTop = null;
    following = false;
    lastTop = viewport.scrollTop;
    notifyBottom(nearBottom());
    remember();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    if (event.defaultPrevented || target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    if (event.key === ' ' && target?.closest?.('button, a')) return;
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) {
      userScroll(['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey));
    }
  };
  const observer = new ResizeObserver(follow);
  observer.observe(content);
  observer.observe(viewport);
  viewport.addEventListener('scroll', onScroll);
  viewport.addEventListener('wheel', onWheel, { passive: true });
  viewport.addEventListener('pointerdown', onPointerDown);
  pointerRoot.addEventListener('pointerup', onPointerUp);
  pointerRoot.addEventListener('pointercancel', onPointerUp);
  viewport.addEventListener('touchstart', onTouchStart, { passive: true });
  viewport.addEventListener('touchmove', onTouchMove, { passive: true });
  viewport.addEventListener('touchend', onTouchEnd);
  viewport.addEventListener('touchcancel', onTouchEnd);
  viewport.addEventListener('click', onDisclosureClick, true);
  viewport.addEventListener('keydown', onKeyDown);
  follow();
  return {
    follow,
    isFollowing: () => !disposed && active && following,
    restoreReading(top: number) {
      if (disposed || !active) return;
      releaseReply();
      following = false;
      restoreTop = top;
      follow();
    },
    revealFinal(reply: HTMLElement) {
      if (disposed || !active || !following) return;
      // 短回复仍留在底部；仅当正文无法完整放进视口时切到开头阅读。
      if (reply.getBoundingClientRect().height <= viewport.clientHeight - 32) return;
      following = false;
      finalReply = reply;
      follow();
      notifyBottom(false);
    },
    setActive(value: boolean) {
      active = value;
      userScrollPending = draggingScrollbar = touchScrolling = false;
      if (!active) releaseReply();
      if (active) {
        if (!following && restoreTop === null) restoreTop = lastTop;
        follow();
      }
    },
    jump(behavior: ScrollBehavior = 'auto') {
      releaseReply();
      restoreTop = null;
      following = true;
      lastTop = viewport.scrollTop;
      notifyBottom(true);
      if (active) viewport.scrollTo({ top: viewport.scrollHeight, behavior });
      lastTop = viewport.scrollTop;
      remember();
    },
    dispose() {
      disposed = true;
      observer.disconnect();
      viewport.removeEventListener('scroll', onScroll);
      viewport.removeEventListener('wheel', onWheel);
      viewport.removeEventListener('pointerdown', onPointerDown);
      pointerRoot.removeEventListener('pointerup', onPointerUp);
      pointerRoot.removeEventListener('pointercancel', onPointerUp);
      viewport.removeEventListener('touchstart', onTouchStart);
      viewport.removeEventListener('touchmove', onTouchMove);
      viewport.removeEventListener('touchend', onTouchEnd);
      viewport.removeEventListener('touchcancel', onTouchEnd);
      viewport.removeEventListener('click', onDisclosureClick, true);
      viewport.removeEventListener('keydown', onKeyDown);
      releaseReply();
    },
  };
}
