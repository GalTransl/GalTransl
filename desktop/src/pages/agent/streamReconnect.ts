// Retry after 1/2/4/8/10 seconds; both Agent streams use the same budget.
export const STREAM_MAX_RECONNECTS = 5;
export const streamReconnectDelay = (attempt: number) => Math.min(1000 * 2 ** attempt, 10000);
