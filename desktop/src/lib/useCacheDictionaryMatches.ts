import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchCacheDictionaryMatches, type CacheEntry, type CacheDictionaryCounts, type CacheDictionarySentence } from './api';

/** Send text only. Inspection metadata never enters editable/saved cache entries. */
export function dictionarySentence(entry: CacheEntry): CacheEntry {
  return {
    index: entry.index,
    name: entry.name,
    pre_src: entry.pre_src || entry.pre_jp || '',
    post_src: entry.post_src || entry.post_jp || '',
    pre_dst: entry.pre_dst || entry.pre_zh || '',
    proofread_dst: entry.proofread_dst || entry.proofread_zh || '',
  };
}

export function dictionarySentenceKey(entry: CacheEntry): string {
  return JSON.stringify(dictionarySentence(entry));
}

export function useCacheDictionaryMatches(
  projectId: string, configFileName: string, filename: string | null,
  entries: CacheEntry[], active: boolean, revision = 0,
) {
  const cache = useRef(new Map<string, CacheDictionaryCounts>());
  const [counts, setCounts] = useState(new Map<string, CacheDictionaryCounts>());
  const [error, setError] = useState('');
  const sentences = useMemo(() => entries.map((entry, index) => {
    const sentence: CacheDictionarySentence = {
      ...dictionarySentence(entry),
      context_before: entries.slice(Math.max(0, index - 2), index).map(dictionarySentence),
      context_after: entries.slice(index + 1, index + 3).map(dictionarySentence),
    };
    return { key: JSON.stringify(sentence), simpleKey: dictionarySentenceKey(entry), entry: sentence };
  }), [entries]);
  const sentenceMap = useMemo(() => new Map(sentences.map(sentence => [sentence.entry.index, sentence.entry])), [sentences]);
  const visibleCounts = useMemo(() => new Map(sentences.flatMap(sentence => {
    const value = counts.get(sentence.key);
    return value ? [[sentence.simpleKey, value] as const] : [];
  })), [sentences, counts]);

  useEffect(() => {
    cache.current = new Map();
    setCounts(new Map());
    setError('');
  }, [projectId, configFileName, filename, active, revision]);

  useEffect(() => {
    if (!active || !projectId || !filename) return;
    const controller = new AbortController();
    const currentCache = cache.current;
    // Bound retention to the selected file's current text, including unsaved edits.
    const keys = new Set(sentences.map(sentence => sentence.key));
    for (const key of currentCache.keys()) if (!keys.has(key)) currentCache.delete(key);
    const missing = sentences.filter(sentence => !currentCache.has(sentence.key));
    if (!missing.length) return;
    const timer = setTimeout(async () => {
      setError('');
      try {
        for (let offset = 0; offset < missing.length; offset += 100) {
          const batch = missing.slice(offset, offset + 100);
          const response = await fetchCacheDictionaryMatches(projectId, configFileName, batch.map(sentence => sentence.entry), { signal: controller.signal });
          if (controller.signal.aborted) return;
          batch.forEach((sentence, i) => currentCache.set(sentence.key, response.results[i].counts));
          setCounts(new Map(currentCache));
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(String(reason instanceof Error ? reason.message : reason));
      }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [projectId, configFileName, filename, sentences, active, revision]);
  return { counts: visibleCounts, sentences: sentenceMap, error };
}
