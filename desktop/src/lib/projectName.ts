/** 项目名会直接拼进路径：路径分隔符和 Windows 非法字符都不能出现，否则能带出父目录。 */
const INVALID_PROJECT_NAME_CHARS = /[\\/:*?"<>|\u0000-\u001F]/;
/** Windows 的设备名不能当目录名。 */
const RESERVED_PROJECT_NAMES = new Set([
  'con', 'prn', 'aux', 'nul',
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);
export const PROJECT_NAME_MAX_LENGTH = 80;

export type ProjectNameIssue = 'empty' | 'invalid' | null;

/**
 * 校验项目名。返回 null 表示可用。
 *
 * 这个函数是写盘前的最后一道门：`..`、`a/b`、`a\b` 这类名字经 joinPath
 * 拼出来会跑到父目录之外，必须在点「创建项目」之前挡掉。
 */
export function validateProjectName(value: string): ProjectNameIssue {
  const name = value.trim();
  if (!name) return 'empty';
  if (name === '.' || name === '..') return 'invalid';
  if (name.length > PROJECT_NAME_MAX_LENGTH) return 'invalid';
  if (INVALID_PROJECT_NAME_CHARS.test(name)) return 'invalid';
  // Windows 会静默去掉结尾的点和空格，`foo.` 与 `foo` 会落到同一个目录
  if (/[.\s]$/.test(name)) return 'invalid';
  if (RESERVED_PROJECT_NAMES.has(name.toLowerCase())) return 'invalid';
  return null;
}
