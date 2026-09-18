import { open as openInShell } from '@tauri-apps/plugin-shell';

/** 项目主页（首页与「关于」页共用一份，避免多处硬编码）。 */
export const PROJECT_HOMEPAGE = 'https://github.com/GalTransl/GalTransl';

/** 最新发布页：更新提示与「关于」页的下载入口都指向这里。 */
export const RELEASE_LATEST_URL = `${PROJECT_HOMEPAGE}/releases/latest`;

/**
 * 用系统默认浏览器打开外部链接。
 *
 * Tauri 里走 shell 插件（capabilities 已按默认 scope 放行 http(s)），
 * 拿不到 Tauri 环境（浏览器里跑开发页）或调用失败时退回 window.open。
 */
export async function openExternalUrl(url: string): Promise<void> {
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    try {
      await openInShell(url);
      return;
    } catch {
      // 落到下面的 window.open
    }
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}
