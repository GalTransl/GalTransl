/**
 * 「这次任务会用哪个后端」的统一口径。
 *
 * 翻译工作台顶部显示当前后端用它，字典页「AI 生成 GPT 字典」的二次确认也用它——
 * 两处必须给出同一句话，否则用户会以为换了个地方启动就换了后端。
 */
import { getSelectedBackendProfileDisplay, resolveSelectedBackendProfile } from './api';

export type BackendUsageSummary = {
  backend: string;
  model: string;
  profile: string;
};

function stringifyConfigValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function toModelDisplayName(modelName: string): string {
  const trimmed = modelName.trim();
  return trimmed.split('/').filter(Boolean).pop() ?? trimmed;
}

function uniqueNonEmpty(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function collectBackendModels(config: Record<string, unknown> | null): { backend: string; model: string } {
  if (!config) {
    return { backend: '未配置后端类型', model: '未填写模型' };
  }

  const enabledBackends: string[] = [];
  const models: string[] = [];

  const openAiConfig = config['OpenAI-Compatible'];
  if (openAiConfig && typeof openAiConfig === 'object') {
    enabledBackends.push('OpenAI-Compatible');
    const tokens = Array.isArray((openAiConfig as Record<string, unknown>).tokens)
      ? (openAiConfig as Record<string, unknown>).tokens as Array<Record<string, unknown>>
      : [];
    models.push(...tokens.map((token) => toModelDisplayName(stringifyConfigValue(token.modelName))));
  }

  const sakuraConfig = config.SakuraLLM;
  if (sakuraConfig && typeof sakuraConfig === 'object') {
    enabledBackends.push('SakuraLLM');
    const rewriteModelName = stringifyConfigValue((sakuraConfig as Record<string, unknown>).rewriteModelName);
    if (rewriteModelName) models.push(toModelDisplayName(rewriteModelName));
  }

  return {
    backend: uniqueNonEmpty(enabledBackends).join(' / ') || '未配置后端类型',
    model: uniqueNonEmpty(models).join(' / ') || '未填写模型',
  };
}

export function summarizeBackendUsage(projectDir: string, projectBackendConfig: Record<string, unknown> | null): BackendUsageSummary {
  const { name, profile } = resolveSelectedBackendProfile(projectDir);
  const selectedProfileDisplay = getSelectedBackendProfileDisplay(projectDir);

  // Following an empty global default means no backend is configured. The
  // project config is only used when the project explicitly opts out of the
  // global profile with "不使用（使用项目自身配置）".
  if (!profile && selectedProfileDisplay === '__default__') {
    return {
      backend: '未配置后端',
      model: '',
      profile: '',
    };
  }

  const activeConfig = profile ?? projectBackendConfig;
  const { model } = collectBackendModels(activeConfig);
  return {
    backend: profile ? name : '自定义后端',
    model,
    profile: name,
  };
}

/** 「配置名 · 模型名」这种一行文案，两处确认/展示都用它 */
export function formatBackendUsage(summary: BackendUsageSummary): string {
  if (!summary.backend) return '未配置后端';
  return summary.model ? `${summary.backend} · ${summary.model}` : summary.backend;
}
