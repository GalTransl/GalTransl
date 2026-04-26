const KEY = 'galtransl-last-project-tab';
const VALID_TABS = ['translate', 'cache', 'config', 'dictionary', 'names'] as const;
type ProjectTab = typeof VALID_TABS[number];

export function loadLastProjectTab(projectDir: string): ProjectTab {
  try {
    const map = JSON.parse(localStorage.getItem(KEY) || '{}') as Record<string, string>;
    const tab = map[projectDir];
    if ((VALID_TABS as readonly string[]).includes(tab)) return tab as ProjectTab;
  } catch {}
  return 'translate';
}

export function saveLastProjectTab(projectDir: string, tab: string): void {
  try {
    const map = JSON.parse(localStorage.getItem(KEY) || '{}') as Record<string, string>;
    map[projectDir] = tab;
    localStorage.setItem(KEY, JSON.stringify(map));
  } catch {}
}
