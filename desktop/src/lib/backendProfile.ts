/** 翻译后端配置的展示信息。
 *
 *  配置文件结构可能是 OpenAI-Compatible（tokens[0].endpoint / modelName）或
 *  SakuraLLM（endpoints[0] / rewriteModelName），这里统一取出来给 UI 展示。
 *  与「模型设置」页的卡片口径保持一致。
 */

export const MISSING_PROFILE_META = '—';

function getRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function getFirstArrayRecord(value: unknown): Record<string, unknown> | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  return getRecord(value[0]);
}

function getFirstArrayString(value: unknown): string | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  return getNonEmptyString(value[0]);
}

function getNonEmptyString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

/** 一个配置里能用到的全部模型名（OpenAI-Compatible 按 tokens 顺序去重；
 *  没有可用 token 时回退到 SakuraLLM 的 rewriteModelName。取不到返回空数组。 */
export function getProfileModelNames(config: Record<string, unknown> | null | undefined): string[] {
  const safeConfig = config ?? {};
  const openAiCompatible = getRecord(safeConfig['OpenAI-Compatible']);
  const tokens = Array.isArray(openAiCompatible?.tokens) ? openAiCompatible.tokens : [];
  const names = tokens
    .map((token) => getNonEmptyString(getRecord(token)?.modelName))
    .filter((name): name is string => Boolean(name));
  if (names.length > 0) {
    // 同一模型可能配了多个 key（不同端点/令牌），去重免得显示成「等 3 个模型」
    return Array.from(new Set(names));
  }
  const sakuraLlm = getRecord(safeConfig.SakuraLLM);
  const rewriteModelName = getNonEmptyString(sakuraLlm?.rewriteModelName);
  return rewriteModelName ? [rewriteModelName] : [];
}

export function getProfileMeta(config: Record<string, unknown> | null | undefined): {
  baseUrl: string;
  modelName: string;
} {
  const safeConfig = config ?? {};
  const openAiCompatible = getRecord(safeConfig['OpenAI-Compatible']);
  const firstOpenAiToken = getFirstArrayRecord(openAiCompatible?.tokens);
  const sakuraLlm = getRecord(safeConfig.SakuraLLM);
  const firstSakuraEndpoint = getFirstArrayString(sakuraLlm?.endpoints);

  const baseUrl =
    getNonEmptyString(firstOpenAiToken?.endpoint) ??
    firstSakuraEndpoint ??
    MISSING_PROFILE_META;

  // 与卡片、切换菜单同口径：取第一个可用模型名（不再只看 tokens[0]，
  // 免得第一个令牌没填模型名就显示「—」而后面几个填了）
  const modelName = getProfileModelNames(safeConfig)[0] ?? MISSING_PROFILE_META;

  return { baseUrl, modelName };
}

/** 「后端配置文件名/模型名」的展示串；模型名取不到时只返回配置文件名。 */
export function formatProfileLabel(
  name: string,
  config: Record<string, unknown> | null | undefined,
): string {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return '';
  const { modelName } = getProfileMeta(config);
  return modelName && modelName !== MISSING_PROFILE_META ? `${trimmed}/${modelName}` : trimmed;
}
