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
 * 把一段文本转义成「只匹配它自己」的正则（按条过滤用，与后端 re.escape 同口径）。
 * 两端的转义写法都要能被 Python re 与 JS RegExp 同时接受。
 */
export function escapeProblemFilterPattern(text: string): string {
  return String(text || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 编译一条过滤项；写坏的正则退回按字面匹配（与后端同口径，不让一条坏模式毁掉整个列表）。 */
function compileProblemFilterPattern(key: string): RegExp | null {
  const text = key.trim();
  if (!text) return null;
  try {
    return new RegExp(text);
  } catch {
    return new RegExp(escapeProblemFilterPattern(text));
  }
}

/**
 * 按「问题项」逐个套正则过滤：命中（search）的那一项丢掉（与后端
 * ProblemFilter.filter_problem_text 同口径）。每个 key 都是一条正则——
 * 如 `缺失.*标点`、`^残留日文：♪`（原则上只过滤小类，不要整类过滤）；
 * 按字面过滤用 escapeProblemFilterPattern 转义。
 */
export function filterProblemText(problem: string | undefined, keys: string[]): string {
  const text = problem || '';
  if (keys.length === 0) return text;
  const patterns = keys.map(compileProblemFilterPattern).filter((re): re is RegExp => re !== null);
  if (patterns.length === 0) return text;
  return text.split(/,\s*/).map((part) => part.trim())
    .filter((part) => part && !patterns.some((re) => re.test(part))).join(', ');
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
