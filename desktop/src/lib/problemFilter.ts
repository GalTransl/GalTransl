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

export function filterProblemText(problem: string | undefined, keys: string[]): string {
  const text = problem || '';
  if (keys.length === 0) return text;
  return text.split(/,\s*/).map((part) => part.trim())
    .filter((part) => part && !keys.some((key) => part.includes(key))).join(', ');
}
