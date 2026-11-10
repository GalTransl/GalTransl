import { useLayoutEffect, useRef, useState } from 'react';

/** 单行文本是否被横向裁掉了。用来只在真溢出时才加淡出遮罩 / tooltip。
 *  `content` 变化或元素尺寸变化时重新测量。 */
export function useIsTruncated<T extends HTMLElement>(content: unknown) {
  const ref = useRef<T | null>(null);
  const [truncated, setTruncated] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) {
      setTruncated(false);
      return;
    }
    const measure = () => setTruncated(el.scrollWidth > el.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [content]);

  return [ref, truncated] as const;
}
