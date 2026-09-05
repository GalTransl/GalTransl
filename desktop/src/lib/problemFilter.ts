export function normalizeKeywordList(value: unknown): string[] {
  const items = typeof value === 'string' ? value.split(/\r?\n/) : value;
  if (!Array.isArray(items)) return [];
  return [...new Set(items.filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim()).filter(Boolean))];
}

export function filterProblemText(problem: string | undefined, keys: string[]): string {
  const text = problem || '';
  if (keys.length === 0) return text;
  return text.split(/,\s*/).map((part) => part.trim())
    .filter((part) => part && !keys.some((key) => part.includes(key))).join(', ');
}
