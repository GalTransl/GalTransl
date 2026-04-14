import { useCallback, useEffect, useState } from 'react';
import { fetchNameDict } from './api';

/**
 * Hook to load the name replacement dictionary (JP→CN) for a project.
 * Returns a Map for O(1) lookup. Automatically reloads when projectId changes.
 */
export function useNameDict(projectId: string) {
  const [nameDict, setNameDict] = useState<Map<string, string>>(new Map());
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    if (!projectId) {
      setNameDict(new Map());
      setLoaded(true);
      return;
    }
    try {
      const res = await fetchNameDict(projectId);
      setNameDict(new Map(Object.entries(res.name_dict)));
    } catch {
      // Non-critical: if name dict fails to load, just use empty map
      setNameDict(new Map());
    } finally {
      setLoaded(true);
    }
  }, [projectId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { nameDict, loaded, reload };
}

/**
 * Resolve a speaker name through the name replacement dictionary.
 * If the speaker has a translation, returns "原文→译文" format for pills.
 * Otherwise returns the original name.
 */
export function resolveSpeakerName(speaker: string, nameDict: Map<string, string>): string {
  const translated = nameDict.get(speaker);
  if (translated) return translated;
  return speaker;
}

/**
 * Check if a speaker has a translation in the name dict.
 */
export function hasNameTranslation(speaker: string, nameDict: Map<string, string>): boolean {
  return nameDict.has(speaker);
}
