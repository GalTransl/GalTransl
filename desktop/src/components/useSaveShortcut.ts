import { useEffect } from 'react';
import { usePageActive } from './PageActivity';

/** Bind only the visible editor; hidden cached editors must not consume Ctrl+S. */
export function useSaveShortcut(save: () => void) {
  const active = usePageActive();
  useEffect(() => {
    if (!active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== 's') return;
      event.preventDefault();
      if (!event.repeat && !event.isComposing) save();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [active, save]);
}
