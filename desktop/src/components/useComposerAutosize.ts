import { useLayoutEffect, type RefObject } from 'react';
import { usePageActive } from './PageActivity';

export function useComposerAutosize(textarea: RefObject<HTMLTextAreaElement>, value: string) {
  const active = usePageActive();
  useLayoutEffect(() => {
    const input = textarea.current;
    if (!input || !active) return;
    const resize = () => {
      input.style.height = 'auto';
      input.style.height = `${input.scrollHeight}px`;
    };
    resize();
    let width = input.clientWidth;
    const observer = new ResizeObserver(() => {
      if (input.clientWidth === width) return;
      width = input.clientWidth;
      resize();
    });
    observer.observe(input);
    return () => observer.disconnect();
  }, [active, value, textarea]);
}
