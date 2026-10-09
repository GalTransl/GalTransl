import { useCallback, useSyncExternalStore, type SetStateAction } from 'react';

const memory = new Map<string, string>();
const listeners = new Set<() => void>();
const keyFor = (projectDir: string, sessionId: string) =>
  `galtransl-agent-draft:${JSON.stringify([projectDir, sessionId])}`;

export function loadMessageDraft(projectDir: string, sessionId: string): string {
  const key = keyFor(projectDir, sessionId);
  if (!memory.has(key)) {
    let text = '';
    try { text = localStorage.getItem(key) || ''; } catch { /* Memory still works without storage. */ }
    memory.set(key, text);
  }
  return memory.get(key)!;
}

export function saveMessageDraft(projectDir: string, sessionId: string, text: string) {
  const key = keyFor(projectDir, sessionId);
  memory.set(key, text);
  try {
    if (text) localStorage.setItem(key, text);
    else localStorage.removeItem(key);
  } catch { /* Keep the in-memory draft if storage is unavailable/full. */ }
  listeners.forEach((listener) => listener());
}

export function moveMessageDraft(projectDir: string, from: string, to: string) {
  if (from === to) return;
  const text = loadMessageDraft(projectDir, from);
  if (text) saveMessageDraft(projectDir, to, text);
  saveMessageDraft(projectDir, from, '');
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useMessageDraft(projectDir: string, sessionId: string) {
  const getSnapshot = useCallback(() => loadMessageDraft(projectDir, sessionId), [projectDir, sessionId]);
  const text = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const setText = useCallback((value: SetStateAction<string>) => {
    saveMessageDraft(projectDir, sessionId, typeof value === 'function' ? value(loadMessageDraft(projectDir, sessionId)) : value);
  }, [projectDir, sessionId]);
  return [text, setText] as const;
}

const PROJECT_KEY = 'galtransl-agent-active-project';
export function loadAgentProject(): string | null {
  try { return localStorage.getItem(PROJECT_KEY); } catch { return null; }
}
export function saveAgentProject(projectDir: string) {
  try { localStorage.setItem(PROJECT_KEY, projectDir); } catch { /* Optional persistence. */ }
}
