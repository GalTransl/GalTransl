/** 跟随内容实际高度（包括折叠动画）；用户上滚或查看记录时让出滚动权。 */
export function followTail(viewport: HTMLElement, content: HTMLElement, onBottom: (value: boolean) => void) {
  let following = true;
  let active = true;
  let lastTop = viewport.scrollTop;
  let disposed = false;
  let finalReply: HTMLElement | null = null;
  let userScrollPending = false;
  let draggingScrollbar = false;
  let touchY: number | null = null;
  let touchScrolling = false;
  const pointerRoot = viewport.ownerDocument ?? viewport;
  const nearBottom = () => viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
  const follow = () => {
    if (disposed || !active) return;
    if (finalReply?.isConnected === false) finalReply = null;
    if (finalReply) {
      // 折叠工作块时回复的实际位置逐帧变化，保持其开头在视口内。
      viewport.scrollTop += finalReply.getBoundingClientRect().top
        - viewport.getBoundingClientRect().top - viewport.clientTop - 16;
      lastTop = viewport.scrollTop;
      return;
    }
    if (!following) {
      onBottom(nearBottom());
      return;
    }
    viewport.scrollTop = viewport.scrollHeight;
    lastTop = viewport.scrollTop;
    onBottom(true);
  };
  const onScroll = () => {
    if (!active) return;
    const top = viewport.scrollTop;
    if (finalReply) {
      lastTop = top;
      return;
    }
    // 终态折叠会把 scrollTop 钳制变小，这不是用户上滚。只有真实输入才能改跟随意图。
    if (userScrollPending || draggingScrollbar || touchScrolling) following = nearBottom();
    userScrollPending = false;
    lastTop = top;
    onBottom(following || nearBottom());
  };
  const releaseReply = () => { finalReply = null; };
  const userScroll = (up: boolean) => {
    if (!active) return;
    releaseReply();
    userScrollPending = true;
    // 输入先于 scroll/ResizeObserver：与 finish 同帧上滚时也必须由用户优先。
    if (up && viewport.scrollTop > 0) {
      following = false;
      onBottom(false);
    }
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
    following = false;
    onBottom(nearBottom());
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
    revealFinal(reply: HTMLElement) {
      if (disposed || !active || !following) return;
      // 短回复仍留在底部；仅当正文无法完整放进视口时切到开头阅读。
      if (reply.getBoundingClientRect().height <= viewport.clientHeight - 32) return;
      following = false;
      finalReply = reply;
      follow();
      onBottom(false);
    },
    setActive(value: boolean) {
      active = value;
      userScrollPending = draggingScrollbar = touchScrolling = false;
      if (!active) releaseReply();
      if (active) {
        if (following) follow();
        else viewport.scrollTop = lastTop;
      }
    },
    jump(behavior: ScrollBehavior = 'auto') {
      releaseReply();
      following = true;
      lastTop = viewport.scrollTop;
      onBottom(true);
      if (active) viewport.scrollTo({ top: viewport.scrollHeight, behavior });
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
