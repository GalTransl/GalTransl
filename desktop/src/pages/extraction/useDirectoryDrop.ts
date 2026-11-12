import { useEffect, useRef, useState, type DragEvent } from 'react';
import { isTauri } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { usePageActive } from '../../components/PageActivity';
import { isAbsolutePath, normalizeFileUriPath } from '../../lib/paths';
import { x } from './labels';

const inside = (bounds: DOMRect | undefined, px: number, py: number) => Boolean(bounds
  && px >= bounds.left && px <= bounds.right && py >= bounds.top && py <= bounds.bottom);
const carriesPath = (transfer: DataTransfer) => ['Files', 'text/uri-list'].some(type => Array.from(transfer.types).includes(type));

/** Native drops provide absolute paths; browser drops must also expose a real path.
 *  `exclude` leaves a nested drop zone (the composer's image drop) to its own handler. */
export function useDirectoryDrop(onSelect: (path: string) => void, disabled: boolean,
  options: { exclude?: string; initialError?: string; onError?: (message: string) => void } = {}) {
  const active = usePageActive();
  const target = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState(options.initialError || '');
  const { exclude } = options;
  const select = (paths: string[]) => {
    setDragging(false);
    if (!active || disabled) return;
    if (paths.length !== 1 || !isAbsolutePath(paths[0])) {
      const message = x('directoryDropError');
      setError(message); options.onError?.(message);
      return;
    }
    setError('');
    onSelect(paths[0]);
  };
  const selectRef = useRef(select); selectRef.current = select;
  const excluded = (event: DragEvent<HTMLDivElement>) => Boolean(exclude && (event.target as Element).closest?.(exclude));

  useEffect(() => {
    setDragging(false);
    if (!active || disabled || !isTauri()) return;
    let disposed = false;
    const listener = getCurrentWebviewWindow().onDragDropEvent(({ payload }) => {
      if (disposed) return;
      if (payload.type === 'leave') { setDragging(false); return; }
      const ratio = window.devicePixelRatio || 1;
      const px = payload.position.x / ratio, py = payload.position.y / ratio;
      const within = inside(target.current?.getBoundingClientRect(), px, py)
        && !(exclude && inside(target.current?.querySelector(exclude)?.getBoundingClientRect(), px, py));
      setDragging(within && payload.type !== 'drop');
      if (within && payload.type === 'drop') selectRef.current(payload.paths);
    });
    void listener.catch(() => { /* The path picker remains available. */ });
    return () => { disposed = true; void listener.then(unlisten => unlisten()).catch(() => {}); };
  }, [active, disabled, exclude]);

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (excluded(event)) { setDragging(false); return; }
    event.preventDefault(); event.stopPropagation();
    setDragging(false);
    if (!active || disabled || !carriesPath(event.dataTransfer)) return;
    const items = Array.from(event.dataTransfer.items || []);
    // Chromium exposes the kind of a dropped entry even without its absolute path.
    if (items.some(item => item.webkitGetAsEntry?.()?.isFile)) { setError(x('directoryDropError')); return; }
    const direct = Array.from(event.dataTransfer.files)
      .map(file => (file as File & { path?: string }).path)
      .filter((path): path is string => Boolean(path));
    const paths = direct.length ? direct : (event.dataTransfer.getData('text/uri-list') || event.dataTransfer.getData('text/plain'))
      .split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#')).map(normalizeFileUriPath);
    selectRef.current(paths);
  };
  return { target, dragging, error, handlers: {
    onDrop,
    onDragOver: (event: DragEvent<HTMLDivElement>) => {
      // Text selections and other in-page drags carry no folder path.
      if (excluded(event) || !carriesPath(event.dataTransfer)) { setDragging(false); return; }
      event.preventDefault(); event.stopPropagation();
      event.dataTransfer.dropEffect = active && !disabled ? 'copy' : 'none';
      setDragging(active && !disabled);
    },
    onDragLeave: (event: DragEvent<HTMLDivElement>) => {
      if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
      setDragging(false);
    },
  } };
}
