/** 翻译后端配置的展示信息。
 *
 *  配置文件结构可能是 OpenAI-Compatible（tokens[0].endpoint / modelName）或
 *  SakuraLLM（endpoints[0] / rewriteModelName），这里统一取出来给 UI 展示。
 *  与「翻译后端配置」页的卡片口径保持一致。
 */

const MISSING_PROFILE_META = '—';

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

  const modelName =
    getNonEmptyString(firstOpenAiToken?.modelName) ??
    getNonEmptyString(sakuraLlm?.rewriteModelName) ??
    MISSING_PROFILE_META;

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
