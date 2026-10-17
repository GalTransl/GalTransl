/** 跟随内容实际高度（包括折叠动画），只在用户向上滚动时退出跟随。 */
export function followTail(viewport: HTMLElement, content: HTMLElement, onBottom: (value: boolean) => void) {
  let following = true;
  let active = true;
  let lastTop = viewport.scrollTop;
  let disposed = false;
  let finalReply: HTMLElement | null = null;
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
    if (!following) return;
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
    // 高度变化产生的 scroll 事件不等于用户离开底部；平滑回底的中途也继续跟随。
    if (!following || top < lastTop) following = nearBottom();
    lastTop = top;
    onBottom(following);
  };
  const releaseReply = () => { finalReply = null; };
  const onKeyDown = (event: KeyboardEvent) => {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) releaseReply();
  };
  const observer = new ResizeObserver(follow);
  observer.observe(content);
  observer.observe(viewport);
  viewport.addEventListener('scroll', onScroll);
  viewport.addEventListener('wheel', releaseReply, { passive: true });
  viewport.addEventListener('pointerdown', releaseReply);
  viewport.addEventListener('touchstart', releaseReply, { passive: true });
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
      viewport.removeEventListener('wheel', releaseReply);
      viewport.removeEventListener('pointerdown', releaseReply);
      viewport.removeEventListener('touchstart', releaseReply);
      viewport.removeEventListener('keydown', onKeyDown);
      releaseReply();
    },
  };
}
