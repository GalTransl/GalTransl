export interface ReadingPosition {
  scrollTop: number;
  following: boolean;
}

export function readingKey(project: string, session: string): string {
  return JSON.stringify([project, session]);
}

/** 只保留当前窗口最近访问的阅读位置，不重复缓存会话正文。 */
export function createReadingMemory(limit = 200) {
  const entries = new Map<string, ReadingPosition>();
  return {
    read(key: string) {
      const value = entries.get(key);
      if (value) {
        entries.delete(key);
        entries.set(key, value);
      }
      return value && { ...value };
    },
    save(key: string, value: ReadingPosition) {
      entries.delete(key);
      entries.set(key, { ...value });
      while (entries.size > limit) entries.delete(entries.keys().next().value!);
    },
    delete(key: string) { entries.delete(key); },
  };
}

export const readingMemory = createReadingMemory();
