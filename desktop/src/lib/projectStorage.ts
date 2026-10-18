// Project navigation must keep working when optional browser persistence fails
// (for example, when cached Agent transcripts exhaust the localStorage quota).
// null is a tombstone: a failed deletion must not expose the old disk value.
const pendingValues = new Map<string, string | null>();

export const PROJECT_HISTORY_KEY = 'galtransl-project-history';
export const PROJECT_HISTORY_CHANGE_EVENT = 'galtransl:project-history-change';

/** Prefix of the regenerable Agent transcript cache. The backend session log is
 *  authoritative, so these entries are the first candidates for eviction. */
export const AGENT_SESSION_CACHE_PREFIX = 'galtransl-agent-session:';

/** Character budget for that cache (localStorage counts UTF-16 code units). */
export const AGENT_SESSION_CACHE_BUDGET = 1_500_000;

const LEGACY_CUSTOM_BACKGROUND_KEY = 'galtransl-custom-background';

export type ProjectHistoryEntry = {
  projectDir: string;
  configFileName: string;
  lastOpened: string;
};

export function readProjectStorage(key: string): string | null {
  if (pendingValues.has(key)) return pendingValues.get(key)!;
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function tryPersist(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value);
    pendingValues.delete(key);
    return true;
  } catch {
    return false;
  }
}

function removeStorageKey(key: string): boolean {
  try {
    localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

function listStorageKeys(): string[] {
  try {
    const storage = localStorage;
    if (typeof storage?.key !== 'function' || typeof storage.length !== 'number') return [];
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key) keys.push(key);
    }
    return keys;
  } catch {
    return [];
  }
}

type CachedSessionEntry = { key: string; size: number; timestamp: number };

function readAgentSessionEntries(sortByTime = true): CachedSessionEntry[] {
  const entries: CachedSessionEntry[] = [];
  // Include failed writes as well as durable entries; both consume resources.
  for (const key of new Set([...listStorageKeys(), ...pendingValues.keys()])) {
    if (!key.startsWith(AGENT_SESSION_CACHE_PREFIX)) continue;
    const raw = readProjectStorage(key);
    if (raw === null) continue;
    let timestamp = 0;
    if (sortByTime) {
      try {
        const parsed = JSON.parse(raw) as { startedAt?: unknown; finishedAt?: unknown };
        const finishedAt = Number(parsed?.finishedAt);
        const startedAt = Number(parsed?.startedAt);
        if (Number.isFinite(finishedAt) && finishedAt > 0) timestamp = finishedAt;
        else if (Number.isFinite(startedAt) && startedAt > 0) timestamp = startedAt;
      } catch {
        // Unreadable cache is the safest thing to evict first.
      }
    }
    entries.push({ key, size: raw.length, timestamp });
  }
  return sortByTime ? entries.sort((a, b) => a.timestamp - b.timestamp) : entries;
}

/** Keep the regenerable transcript cache under budget, never dropping the entry
 *  that was just written unless it is the only one left. */
function enforceAgentSessionBudget(keepKey = ''): void {
  let entries = readAgentSessionEntries(false);
  let total = entries.reduce((sum, entry) => sum + entry.size, 0);
  if (total <= AGENT_SESSION_CACHE_BUDGET) return;
  // Normal saves only count characters; avoid parsing every cached tool result.
  entries = readAgentSessionEntries();
  for (const entry of entries) {
    if (total <= AGENT_SESSION_CACHE_BUDGET) break;
    if (entry.key === keepKey) continue;
    removeProjectStorage(entry.key);
    total -= entry.size;
  }
}

/** Clean up caches from older versions before settings or project setup writes. */
export function initializeProjectStorage(): void {
  enforceAgentSessionBudget();
}

export function writeProjectStorage(key: string, value: string): boolean {
  const transcript = key.startsWith(AGENT_SESSION_CACHE_PREFIX);
  // Event count does not bound serialized size: one tool result can be huge.
  // Skip this optional cache before evicting other sessions trying to fit it.
  if (transcript && value.length > AGENT_SESSION_CACHE_BUDGET) {
    removeProjectStorage(key);
    return false;
  }
  if (tryPersist(key, value)) {
    if (transcript) enforceAgentSessionBudget(key);
    return true;
  }

  // Browser storage is full. Cached Agent transcripts are regenerable from the
  // backend session log, so evict them oldest-first and retry after each removal.
  for (const entry of readAgentSessionEntries()) {
    if (entry.key === key) continue;
    removeProjectStorage(entry.key);
    if (tryPersist(key, value)) {
      if (transcript) enforceAgentSessionBudget(key);
      return true;
    }
  }

  // Freeing the target transcript's own previous value usually succeeds: a new
  // (bounded) transcript is smaller than the stale one it replaces.
  if (transcript && removeStorageKey(key) && tryPersist(key, value)) {
    enforceAgentSessionBudget(key);
    return true;
  }

  // Last resort: the pre-8.4 background image is a multi-megabyte base64 blob
  // that can be re-created from disk, so project metadata must win over it.
  if (removeStorageKey(LEGACY_CUSTOM_BACKGROUND_KEY) && tryPersist(key, value)) {
    if (transcript) enforceAgentSessionBudget(key);
    return true;
  }

  // Preserve the latest value for this app session, including deletions.
  pendingValues.set(key, value);
  if (transcript) enforceAgentSessionBudget(key);
  return false;
}

export function removeProjectStorage(key: string): void {
  if (removeStorageKey(key)) pendingValues.delete(key);
  else pendingValues.set(key, null);
}

export function readProjectHistory(): ProjectHistoryEntry[] {
  try {
    const parsed: unknown = JSON.parse(readProjectStorage(PROJECT_HISTORY_KEY) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is ProjectHistoryEntry =>
      entry !== null && typeof entry === 'object'
      && typeof entry.projectDir === 'string' && Boolean(entry.projectDir.trim())
      && typeof entry.configFileName === 'string' && typeof entry.lastOpened === 'string',
    );
  } catch {
    return [];
  }
}
