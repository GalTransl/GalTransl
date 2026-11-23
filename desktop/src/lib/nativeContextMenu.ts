/* 屏蔽 WebView 自带的右键菜单。

   桌面应用里它只会露出浏览器的东西：链接上是「在新窗口中打开链接 / 将链接另存为」，
   空白处是「刷新 / 检查」。只在它真有用的地方放行——能编辑的文本（剪切/粘贴/撤销）
   和已经选中的文字（复制）。应用自己的右键菜单走 React 的 onContextMenu，不受影响。 */

type ContextMenuTarget = { closest?: (selector: string) => unknown } | null;

/** 这次右键要不要保留浏览器自带的菜单。selectedText 是页面上当前选中的文字。 */
export function keepsNativeContextMenu(target: ContextMenuTarget, selectedText: string): boolean {
  if (target?.closest?.('input, textarea, [contenteditable]:not([contenteditable="false"])')) return true;
  return selectedText.trim() !== '';
}

/** 安装全局拦截，返回卸载函数。启动时调用一次即可。 */
export function installNativeContextMenuBlock(): () => void {
  if (typeof document === 'undefined') return () => {};
  const onContextMenu = (event: MouseEvent) => {
    const selectedText = window.getSelection()?.toString() ?? '';
    if (!keepsNativeContextMenu(event.target as ContextMenuTarget, selectedText)) event.preventDefault();
  };
  document.addEventListener('contextmenu', onContextMenu);
  return () => document.removeEventListener('contextmenu', onContextMenu);
}
