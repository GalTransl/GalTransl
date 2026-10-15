import { useEffect, useState } from 'react';
import { fetchProjectConfig } from './api';

/** Refresh on entry, independently of the once-per-second runtime snapshots. */
export function useProjectTranslationConfig(projectId: string, configFileName: string, active: boolean) {
  const key = `${projectId}:${configFileName}`;
  const [snapshot, setSnapshot] = useState<{ key: string; config: Record<string, unknown> } | null>(null);
  useEffect(() => {
    if (!active || !projectId) return;
    let cancelled = false;
    void fetchProjectConfig(projectId, configFileName).then(data => {
      if (!cancelled) setSnapshot({ key, config: data.config });
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId, configFileName, key, active]);
  return snapshot?.key === key ? snapshot.config : null;
}
