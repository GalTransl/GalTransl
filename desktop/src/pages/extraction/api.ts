import { getBackendBaseUrl } from '../../lib/api';

export function extractionUrl(action: string, params: Record<string, string | number> = {}) {
  return `${getBackendBaseUrl().replace(/\/$/, '')}/api/extraction/${action}?${new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))}`;
}
export async function extractionApi<T>(action: string, params: Record<string, string | number> = {}, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(extractionUrl(action, params), {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `${response.status} ${response.statusText}`);
  return result as T;
}
