import { useEffect, useRef, useState, type DragEvent } from 'react';
import { isTauri } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { usePageActive } from '../../components/PageActivity';
import { isAbsolutePath, normalizeFileUriPath } from '../../lib/paths';
import { x } from './labels';

/** Native drops provide absolute paths; browser drops must also expose a real path. */
export function useDirectoryDrop(onSelect: (path: string) => void, disabled: boolean) {
  const active = usePageActive();
  const target = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState('');
  const select = (paths: string[]) => {
    setDragging(false);
    if (!active || disabled) return;
    if (paths.length !== 1 || !isAbsolutePath(paths[0])) { setError(x('directoryDropError')); return; }
    setError('');
    onSelect(paths[0]);
  };
  const selectRef = useRef(select); selectRef.current = select;

  useEffect(() => {
    setDragging(false);
    if (!active || disabled || !isTauri()) return;
    let disposed = false;
    const listener = getCurrentWebviewWindow().onDragDropEvent(({ payload }) => {
      if (disposed) return;
      if (payload.type === 'leave') { setDragging(false); return; }
      const bounds = target.current?.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const { x: px, y: py } = payload.position;
      const within = bounds && px / ratio >= bounds.left && px / ratio <= bounds.right
        && py / ratio >= bounds.top && py / ratio <= bounds.bottom;
      setDragging(Boolean(within) && payload.type !== 'drop');
      if (within && payload.type === 'drop') selectRef.current(payload.paths);
    });
    void listener.catch(() => { /* The path picker remains available. */ });
    return () => { disposed = true; void listener.then(unlisten => unlisten()).catch(() => {}); };
  }, [active, disabled]);

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault(); event.stopPropagation();
    setDragging(false);
    if (!active || disabled) return;
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
