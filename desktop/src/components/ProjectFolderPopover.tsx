import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { invoke } from '@tauri-apps/api/core';
import { Icon, type IconName } from './Icon';

const FOLDERS: Array<{ label: string; shortLabel: string; icon: IconName; directory: string }> = [
  { label: '项目文件夹', shortLabel: '项目', icon: 'folder-open', directory: '' },
  { label: '输入文件夹', shortLabel: '输入', icon: 'inbox', directory: 'gt_input' },
  { label: '输出文件夹', shortLabel: '输出', icon: 'upload', directory: 'gt_output' },
  { label: '缓存文件夹', shortLabel: '缓存', icon: 'database', directory: 'transl_cache' },
];

export function ProjectFolderPopover({ projectDir, className, expanded, onError }: {
  projectDir: string;
  className: string;
  expanded?: boolean;
  onError: (message: string) => void;
}) {
  const id = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout>>();
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const exitTimer = useRef<ReturnType<typeof setTimeout>>();
  const focusFrame = useRef<number>();
  const pinned = useRef(false);
  const [closing, setClosing] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const cancelClose = () => {
    clearTimeout(closeTimer.current);
    clearTimeout(exitTimer.current);
    setClosing(false);
  };
  const close = useCallback(() => {
    if (focusFrame.current !== undefined) cancelAnimationFrame(focusFrame.current);
    clearTimeout(openTimer.current);
    clearTimeout(closeTimer.current);
    clearTimeout(exitTimer.current);
    pinned.current = false;
    setClosing(true);
    exitTimer.current = setTimeout(() => setPosition(null), 130);
  }, []);
  const closeSoon = () => {
    clearTimeout(openTimer.current);
    if (pinned.current || panel.current?.contains(document.activeElement)) return;
    clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(close, 260);
  };
  const show = () => {
    clearTimeout(openTimer.current);
    cancelClose();
    const rect = anchor.current?.getBoundingClientRect();
    if (!rect) return;
    // Place actions immediately beside the icon: no long invisible hover corridor.
    const left = Math.max(8, Math.min(rect.right + 6, window.innerWidth - 264));
    const top = Math.max(8, Math.min(rect.top + rect.height / 2 - 34, window.innerHeight - 76));
    setPosition({ left, top });
  };
  const hover = () => {
    cancelClose();
    clearTimeout(openTimer.current);
    openTimer.current = setTimeout(show, 120);
  };
  const openFolder = async (directory: string) => {
    close();
    const base = projectDir.replace(/[\\/]+$/, '');
    const path = directory ? `${base}\\${directory}` : projectDir;
    try {
      await invoke('open_folder', { path });
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  };

  useEffect(() => () => {
    if (focusFrame.current !== undefined) cancelAnimationFrame(focusFrame.current);
    clearTimeout(openTimer.current);
    clearTimeout(closeTimer.current);
    clearTimeout(exitTimer.current);
  }, []);
  useEffect(() => {
    if (!position) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        anchor.current?.focus();
        close();
      }
    };
    const outside = (event: PointerEvent) => {
      if (!anchor.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) close();
    };
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('hashchange', close);
    document.addEventListener('keydown', escape);
    document.addEventListener('pointerdown', outside);
    return () => {
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('hashchange', close);
      document.removeEventListener('keydown', escape);
      document.removeEventListener('pointerdown', outside);
    };
  }, [position, close]);

  return <>
    <span
      ref={anchor}
      className={`${className} sidebar-folder-trigger${position && !closing ? ' sidebar-folder-trigger--active' : ''}`}
      role="button"
      tabIndex={0}
      aria-label="项目文件夹快捷操作"
      aria-expanded={Boolean(position) && !closing}
      aria-controls={position ? id : undefined}
      onMouseEnter={hover}
      onMouseLeave={closeSoon}
      onFocus={(event) => { if (event.currentTarget.matches(':focus-visible')) show(); }}
      onBlur={(event) => { if (!panel.current?.contains(event.relatedTarget)) closeSoon(); }}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        if (pinned.current) close();
        else { show(); pinned.current = true; }
      }}
      onKeyDown={(event) => {
        if (['Enter', ' ', 'ArrowRight', 'ArrowDown'].includes(event.key)) {
          event.preventDefault();
          event.stopPropagation();
          show();
          pinned.current = true;
          focusFrame.current = requestAnimationFrame(() => panel.current?.querySelector('button')?.focus());
        }
      }}
    ><Icon name={expanded ? 'folder-open' : 'folder'} /></span>
    {position && createPortal(
      <div
        ref={panel}
        id={id}
        role="group"
        aria-label="项目文件夹快捷操作"
        className={`sidebar-folder-popover${closing ? ' sidebar-folder-popover--closing' : ''}`}
        style={{ left: position.left, top: position.top } as CSSProperties}
        onMouseEnter={cancelClose}
        onMouseLeave={closeSoon}
        onFocus={cancelClose}
        onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget) && event.relatedTarget !== anchor.current) close(); }}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') return;
          event.stopPropagation();
          if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            const buttons = Array.from(event.currentTarget.querySelectorAll('button'));
            const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? 3 : (index + (event.key === 'ArrowRight' ? 1 : 3)) % 4;
            buttons[next]?.focus();
          }
        }}
      >
        <span className="sidebar-folder-popover__bridge" />
        {FOLDERS.map(({ label, shortLabel, icon, directory }) => (
          <button key={label} type="button" aria-label={label} title={label} onClick={() => void openFolder(directory)}>
            <span className="sidebar-folder-popover__icon"><Icon name={icon} /></span><span>{shortLabel}</span>
          </button>
        ))}
      </div>, document.body,
    )}
  </>;
}
