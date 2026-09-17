export function normalizeKeywordList(value: unknown): string[] {
  const items = typeof value === 'string' ? value.split(/\r?\n/) : value;
  if (!Array.isArray(items)) return [];
  return [...new Set(items.filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim()).filter(Boolean))];
}

/** 按缓存问题使用的英文逗号拆分问题项。 */
export function splitProblemItems(problem: string | undefined): string[] {
  return String(problem || '')
    .split(/,\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** 将问题文本拆成可单独过滤的问题类型。 */
export function splitProblemTypes(problem: string | undefined): string[] {
  return [...new Set(splitProblemItems(problem)
    .map((part) => part.split('：')[0].trim())
    .filter(Boolean))];
}

/**
 * 按「问题项」精准匹配过滤：只丢掉与某个 key 逐字相同的那一项（与后端
 * ProblemFilter.filter_problem_text 同口径）。不做子串匹配，因此无法用一个词滤掉整个大类。
 */
export function filterProblemText(problem: string | undefined, keys: string[]): string {
  const text = problem || '';
  if (keys.length === 0) return text;
  const wanted = new Set(keys.map((key) => key.trim()).filter(Boolean));
  if (wanted.size === 0) return text;
  return text.split(/,\s*/).map((part) => part.trim())
    .filter((part) => part && !wanted.has(part)).join(', ');
}

/**
 * 校验问题白名单条目：必须是「缓存文件名:index」，index 为数字或闭区间。
 * 返回错误文案；合法返回 null。与后端 ProblemWhiteList.parse_problem_white_list_entry 同口径。
 */
export function validateProblemWhiteListEntry(value: string): string | null {
  const text = value.trim();
  const sep = text.lastIndexOf(':');
  if (sep <= 0) return '格式应为「缓存文件名:index」，如 01.json:12';
  if (!text.slice(0, sep).trim()) return '缺少缓存文件名';
  const token = text.slice(sep + 1).trim();
  if (/^\d+$/.test(token)) return null;
  if (/^(\d+)\s*-\s*(\d+)$/.test(token)) return null;
  return 'index 应为数字或区间（如 12 或 12-15）';
}
