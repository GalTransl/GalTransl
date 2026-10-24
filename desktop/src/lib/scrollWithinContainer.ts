/** Scroll only this pane; scrollIntoView also moves its scrollable ancestors. */
export function scrollWithinContainer(container: HTMLElement, target: HTMLElement, align: 'center' | 'nearest' = 'center') {
  const viewportTop = container.getBoundingClientRect().top + container.clientTop;
  const rect = target.getBoundingClientRect();
  const top = rect.top - viewportTop;
  const bottom = rect.bottom - viewportTop;
  let delta = top - (container.clientHeight - rect.height) / 2;
  if (align === 'nearest') {
    if (top >= 0 && bottom <= container.clientHeight) return;
    delta = top < 0 ? top : bottom - container.clientHeight;
  }
  container.scrollTop = Math.max(0, Math.min(container.scrollTop + delta, container.scrollHeight - container.clientHeight));
}
