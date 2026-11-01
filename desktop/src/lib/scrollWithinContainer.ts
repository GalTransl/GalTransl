/** Scroll only this pane; scrollIntoView also moves its scrollable ancestors. */
export function scrollWithinContainer(container: HTMLElement, target: HTMLElement, align: 'center' | 'nearest' = 'center') {
  const viewport = container.getBoundingClientRect();
  // Mini windows use CSS zoom; rects are scaled but scrollTop remains in layout pixels.
  const scale = viewport.height / container.offsetHeight || 1;
  const viewportTop = viewport.top + container.clientTop * scale;
  const rect = target.getBoundingClientRect();
  const top = (rect.top - viewportTop) / scale;
  const bottom = (rect.bottom - viewportTop) / scale;
  let delta = top - (container.clientHeight - rect.height / scale) / 2;
  if (align === 'nearest') {
    if (top >= 0 && bottom <= container.clientHeight) return;
    delta = top < 0 ? top : bottom - container.clientHeight;
  }
  container.scrollTop = Math.max(0, Math.min(container.scrollTop + delta, container.scrollHeight - container.clientHeight));
}
