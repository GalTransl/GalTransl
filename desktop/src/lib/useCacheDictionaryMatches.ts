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
  /** 只查这些句子（entry.index）。浏览页只传屏幕附近渲染出来的卡片：几千句的文件一次全查
   *  要连发几十批请求，占住共用的后端，切到别的页面也跟着卡。不传则查全部。 */
  wanted?: ReadonlySet<number> | null,
) {
  const cache = useRef(new Map<string, CacheDictionaryCounts>());
  const [counts, setCounts] = useState(new Map<string, CacheDictionaryCounts>());
  const [error, setError] = useState('');
  // 同一内容复用同一个对象：卡片按引用比较 props，换新对象就会整卡重渲染。
  const sentenceObjects = useRef(new Map<string, CacheDictionarySentence>());
  const sentences = useMemo(() => {
    const previous = sentenceObjects.current;
    const next = new Map<string, CacheDictionarySentence>();
    const result: { key: string; simpleKey: string; entry: CacheDictionarySentence }[] = [];
    entries.forEach((entry, index) => {
      if (wanted && !wanted.has(entry.index)) return;
      const built: CacheDictionarySentence = {
        ...dictionarySentence(entry),
        context_before: entries.slice(Math.max(0, index - 2), index).map(dictionarySentence),
        context_after: entries.slice(index + 1, index + 3).map(dictionarySentence),
      };
      const key = JSON.stringify(built);
      const sentence = next.get(key) ?? previous.get(key) ?? built;
      next.set(key, sentence);
      result.push({ key, simpleKey: dictionarySentenceKey(entry), entry: sentence });
    });
    sentenceObjects.current = next;
    return result;
  }, [entries, wanted]);
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
    // A partial view keeps rows scrolled away (scrolling back must not refetch them)
    // and only drops the oldest results once the cache outgrows the file.
    if (wanted) {
      const limit = Math.max(entries.length * 2, 1000);
      for (const key of currentCache.keys()) {
        if (currentCache.size <= limit) break;
        currentCache.delete(key);
      }
    } else {
      const keys = new Set(sentences.map(sentence => sentence.key));
      for (const key of currentCache.keys()) if (!keys.has(key)) currentCache.delete(key);
    }
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
