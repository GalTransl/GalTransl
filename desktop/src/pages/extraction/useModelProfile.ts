import { useEffect, useState } from 'react';
import { AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, BACKEND_PROFILES_CHANGE_EVENT,
  getAgentDefaultBackendProfile, getBackendProfile, getBackendProfileNames } from '../../lib/api';
import { readProjectStorage, writeProjectStorage } from '../../lib/projectStorage';
import type { Session } from './types';

const STORAGE_KEY = 'galtransl.extraction.modelBindings';
const profiles = () => getBackendProfileNames().filter(name => getBackendProfile(name)?.['OpenAI-Compatible']);
const loadBindings = (): Record<string, string> => {
  try {
    const saved = JSON.parse(readProjectStorage(STORAGE_KEY) || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved)
      ? Object.fromEntries(Object.entries(saved).filter(([, value]) => typeof value === 'string')) as Record<string, string> : {};
  } catch { return {}; }
};

/** Empty bindings follow the main Agent default; concrete names stay session-local. */
export function useModelProfile(session: Session | undefined, selected: string) {
  const [models, setModels] = useState(profiles);
  const [agentDefault, setAgentDefault] = useState(getAgentDefaultBackendProfile);
  const [bindings, setBindings] = useState(loadBindings);
  useEffect(() => {
    const sync = () => { setModels(profiles()); setAgentDefault(getAgentDefaultBackendProfile()); };
    sync();
    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
    window.addEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
      window.removeEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    };
  }, []);
  const key = session?.id || selected;
  const saved = bindings[key] ?? session?.profile_name ?? '';
  const boundProfile = models.includes(saved) ? saved : '';
  const defaultProfile = models.includes(agentDefault) ? agentDefault : models[0] || '';
  const update = (target: string, value: string) => setBindings(previous => {
    const next = { ...previous, [target]: value };
    writeProjectStorage(STORAGE_KEY, JSON.stringify(next));
    return next;
  });
  return { models, boundProfile, defaultProfile, profile: boundProfile || defaultProfile,
    setProfile: (value: string) => { if (!value || models.includes(value)) update(key, value); },
    moveProfile: (target: string) => update(target, boundProfile),
  };
}
