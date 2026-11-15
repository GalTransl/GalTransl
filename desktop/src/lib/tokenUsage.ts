/**
 * 工作台「Token 用量」一格的口径：把后端累计的计数换成界面要显示的数。
 *
 * 缓存命中和思考不是每个接口都报：后端对没报过的项给 null，这里也保持 null（界面显示「接口未提供」），
 * 不能当成 0——那会把「没数据」显示成「零命中」。比例只拿报了这一项的请求当分母。
 */
import type { ProjectRuntimeUsage } from './api';

export type TokenUsageSummary = {
  /** 有没有任何请求返回过用量；false 时那一格显示占位符 */
  hasData: boolean;
  requests: number;
  /** 成功返回但没带用量的请求数（流被中途掐断、接口不支持） */
  unreportedRequests: number;
  input: number;
  output: number;
  total: number;
  cached: number | null;
  /** 0–1；缓存命中 ÷ 报了缓存的那些请求的输入 */
  cacheRate: number | null;
  reasoning: number | null;
  /** 0–1；思考 ÷ 报了思考的那些请求的输出 */
  reasoningRate: number | null;
};

function count(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

export function summarizeTokenUsage(usage: ProjectRuntimeUsage | null | undefined): TokenUsageSummary {
  const requests = count(usage?.requests);
  const reported = Math.min(count(usage?.reported_requests), requests);
  const input = count(usage?.input_tokens);
  const output = count(usage?.output_tokens);
  const cacheBase = count(usage?.cache_reported_input_tokens);
  const reasoningBase = count(usage?.reasoning_reported_output_tokens);
  const cached = usage?.cached_tokens == null || cacheBase === 0 ? null : count(usage.cached_tokens);
  const reasoning = usage?.reasoning_tokens == null || reasoningBase === 0 ? null : count(usage.reasoning_tokens);
  return {
    hasData: reported > 0,
    requests,
    unreportedRequests: requests - reported,
    input,
    output,
    total: input + output,
    cached,
    cacheRate: cached === null ? null : Math.min(cached / cacheBase, 1),
    reasoning,
    reasoningRate: reasoning === null ? null : Math.min(reasoning / reasoningBase, 1),
  };
}

/** 一格和明细共用的写法：999 / 12.3K / 4.56M */
export function formatTokenCount(value: number): string {
  const n = count(value);
  if (n < 1000) return String(n);
  // 先按显示精度取整再选单位，避免 999950 显示成「1000.0K」
  if (n < 999_950) return `${(n / 1000).toFixed(1)}K`;
  if (n < 999_995_000) return `${(n / 1_000_000).toFixed(2)}M`;
  return `${(n / 1_000_000_000).toFixed(2)}B`;
}

/** 0–1 的比例写成一位小数的百分数（不带 % 号，符号在文案里） */
export function formatTokenPercent(rate: number): string {
  return (Math.max(0, Math.min(rate, 1)) * 100).toFixed(1);
}
