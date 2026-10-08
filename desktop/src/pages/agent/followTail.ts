/** 跟随内容实际高度（包括折叠动画），只在用户向上滚动时退出跟随。 */
export function followTail(viewport: HTMLElement, content: HTMLElement, onBottom: (value: boolean) => void) {
  let following = true;
  let lastTop = viewport.scrollTop;
  let disposed = false;
  const nearBottom = () => viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
  const follow = () => {
    if (disposed || !following) return;
    viewport.scrollTop = viewport.scrollHeight;
    lastTop = viewport.scrollTop;
    onBottom(true);
  };
  const onScroll = () => {
    const top = viewport.scrollTop;
    // 高度变化产生的 scroll 事件不等于用户离开底部；平滑回底的中途也继续跟随。
    if (!following || top < lastTop) following = nearBottom();
    lastTop = top;
    onBottom(following);
  };
  const observer = new ResizeObserver(follow);
  observer.observe(content);
  observer.observe(viewport);
  viewport.addEventListener('scroll', onScroll);
  follow();
  return {
    follow,
    jump(behavior: ScrollBehavior = 'auto') {
      following = true;
      lastTop = viewport.scrollTop;
      onBottom(true);
      viewport.scrollTo({ top: viewport.scrollHeight, behavior });
    },
    dispose() {
      disposed = true;
      observer.disconnect();
      viewport.removeEventListener('scroll', onScroll);
    },
  };
}
