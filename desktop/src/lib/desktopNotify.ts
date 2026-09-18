import { getCurrentWindow } from '@tauri-apps/api/window';
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from '@tauri-apps/plugin-notification';

/**
 * 系统通知（Windows 上就是右下角的 toast）。
 *
 * Agent 回合是后台跑的：它卡在 ask_user 或权限审批上时，用户很可能已经切到别的窗口，
 * 界面上那张卡片没人在看。这时弹一条系统通知，点一下就能回到应用处理。
 *
 * 三条规矩：
 * - 只在 Tauri 壳里发（没有 Cargo 时的浏览器回退没有原生通知可用，静默跳过）；
 * - 窗口在前台就不发（卡片就在眼前，再弹一次是噪音）；
 * - 发不出去一律静默——通知是锦上添花，绝不能反过来影响回合。
 */

/** key → 已提醒过的去重集合。同一次提问/审批只提醒一遍（React 严格模式的重复 effect 也不会弹两次）。 */
const notifiedKeys = new Set<string>();

function inTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

let permissionPromise: Promise<boolean> | null = null;

/** 通知权限：Windows 恒为已授权；macOS/Linux 首次会弹系统授权框。只请求一次并缓存结果。 */
function ensurePermission(): Promise<boolean> {
  if (!permissionPromise) {
    permissionPromise = (async () => {
      try {
        if (await isPermissionGranted()) return true;
        return (await requestPermission()) === 'granted';
      } catch {
        return false;
      }
    })();
  }
  return permissionPromise;
}

/** 窗口是否在前台。Tauri 用窗口真状态；浏览器回退用 document.hasFocus()；判不出来按"在前台"。 */
async function isWindowFocused(): Promise<boolean> {
  if (inTauri()) {
    try {
      return await getCurrentWindow().isFocused();
    } catch {
      return true;
    }
  }
  return typeof document === 'undefined' ? true : document.hasFocus();
}

/**
 * 弹一条系统通知。key 用于去重：同一个 key 只发一次（比如同一次 ask_user 的 id）。
 */
export async function notifyNeedsAttention(key: string, title: string, body: string): Promise<void> {
  if (!inTauri() || !key || notifiedKeys.has(key)) return;
  // 去重集合无界增长没意义：清一次旧的，代价是极少数长期挂着的项可能被再提醒一次
  if (notifiedKeys.size > 500) notifiedKeys.clear();
  notifiedKeys.add(key);
  if (await isWindowFocused()) return;
  if (!(await ensurePermission())) return;
  try {
    sendNotification({ title, body });
  } catch {
    /* 静默：通知发不出去不该影响任何东西 */
  }
}
