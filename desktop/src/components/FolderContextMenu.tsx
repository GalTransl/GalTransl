import { invoke } from '@tauri-apps/api/core';
import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { t as translate } from '../i18n';
import { Icon } from './Icon';

/* 文件夹分组的右键菜单（提取 / 翻译 Agent 会话侧栏共用）：目前只有「打开文件夹」。
   样式复用主侧栏的 .sidebar__context-menu；挂到 body 上，免得被侧栏的
   overflow / transform 裁掉或错位。 */

const MENU_WIDTH = 180;
const MENU_HEIGHT = 48;

export function useFolderContextMenu(): {
  onContextMenu: (event: ReactMouseEvent, path: string) => void;
  menu: ReactNode;
} {
  const [state, setState] = useState<{ x: number; y: number; path: string } | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!state) return;
    const close = () => setState(null);
    const onPointerDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') close(); };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('blur', close);
    window.addEventListener('resize', close);
    // 捕获阶段：侧栏列表自己的滚动也算，菜单位置是 fixed 的，滚了就对不上了
    window.addEventListener('scroll', close, true);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('blur', close);
      window.removeEventListener('resize', close);
      window.removeEventListener('scroll', close, true);
    };
  }, [state]);

  const onContextMenu = useCallback((event: ReactMouseEvent, path: string) => {
    if (!path) return;
    event.preventDefault();
    event.stopPropagation();
    setState({
      x: Math.max(8, Math.min(event.clientX, window.innerWidth - MENU_WIDTH - 8)),
      y: Math.max(8, Math.min(event.clientY, window.innerHeight - MENU_HEIGHT - 8)),
      path,
    });
  }, []);

  const menu = state ? createPortal(
    <div
      ref={ref}
      className="sidebar__context-menu"
      role="menu"
      style={{ left: state.x, top: state.y }}
      onContextMenu={(event) => event.preventDefault()}
    >
      <button
        type="button"
        role="menuitem"
        className="sidebar__context-menu-item"
        title={state.path}
        autoFocus
        onClick={() => {
          const { path } = state;
          setState(null);
          void invoke('open_folder', { path }).catch((error) => console.error('open_folder failed', path, error));
        }}
      >
        <span className="sidebar__context-menu-icon" aria-hidden><Icon name="folder-open" /></span>
        {translate('common:actions.openFolder')}
      </button>
    </div>,
    document.body,
  ) : null;

  return { onContextMenu, menu };
}
