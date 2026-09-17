/** Agent 权限模式：四档，与后端 runtime.py 的 PERMISSION_MODES 一一对应。
 *
 * 只存在前端 localStorage（与后端配置名同一套做法），随 start / message 一起送过去；
 * 后端在**每次工具调用前**按它决定放行还是先问用户——模型不知道当前是什么模式，
 * 被拒时只会收到一条"用户拒绝权限"的工具错误。
 *
 * - ask（每次询问，默认）：所有写操作都先弹确认卡；
 * - accept-edits（允许编辑）：只自动放行"改译文数据"（缓存 / 字典 / 人名表），
 *   改项目配置、改项目规范、启动翻译仍然要确认；
 * - auto（全自动）：不再确认；
 * - auto-quiet（全自动-减少问询）：放行规则与 auto 相同，差别只在后端会**把档位写进
 *   system prompt**，要求 Agent 更自主、少调用 ask_user（唯一一档告诉模型的）。
 */

export const PERMISSION_MODES = ['ask', 'accept-edits', 'auto', 'auto-quiet'] as const;

export type PermissionMode = (typeof PERMISSION_MODES)[number];

/** 后端答复卡片的三种动作（与后端 PERMISSION_DECISIONS 一致）。 */
export const PERMISSION_DECISIONS = ['allow-once', 'allow-session', 'deny'] as const;

export type PermissionDecision = (typeof PERMISSION_DECISIONS)[number];

export const PERMISSION_MODE_LABELS: Record<PermissionMode, string> = {
  ask: '每次询问',
  'accept-edits': '允许编辑',
  auto: '全自动',
  'auto-quiet': '全自动-减少问询',
};

/** 每档的一句话说明（菜单里跟在名字下面，选择时看得见代价）。 */
export const PERMISSION_MODE_HINTS: Record<PermissionMode, string> = {
  ask: '每个改动都先问你',
  'accept-edits': '缓存与字典直接改；改设置、规范、启动翻译、派子代理要先问',
  auto: '不再确认，Agent 自主改缓存、设置与启动翻译',
  'auto-quiet': '同「全自动」，并要求 Agent 更自主、尽量不问',
};

const STORAGE_KEY = 'galtransl.agent.permissionMode';

export function normalizePermissionMode(value: unknown): PermissionMode {
  return PERMISSION_MODES.includes(value as PermissionMode) ? (value as PermissionMode) : 'ask';
}

export function loadPermissionMode(): PermissionMode {
  try {
    return normalizePermissionMode(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return 'ask';
  }
}

export function savePermissionMode(mode: PermissionMode): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // localStorage 不可用（隐私模式等）：本次会话仍然生效，只是记不住
  }
}
