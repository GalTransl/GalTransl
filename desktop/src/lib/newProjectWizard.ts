import { basenamePath, dirnamePath, isAbsolutePath } from './paths';
import { validateProjectName } from './projectName';

export function projectDirectoryPrefill(directory: unknown): { parentDir: string; projectName: string } | null {
  if (typeof directory !== 'string' || /[\u0000-\u001f]/.test(directory)) return null;
  const path = directory.trim().replace(/[\\/]+$/, '');
  const parentDir = dirnamePath(path);
  const projectName = basenamePath(path);
  if (!isAbsolutePath(path) || !isAbsolutePath(parentDir) || validateProjectName(projectName)) return null;
  return { parentDir, projectName };
}

export function wizardDirectoryFromState(state: unknown): string | undefined {
  if (!state || typeof state !== 'object') return undefined;
  const directory = (state as Record<string, unknown>).newProjectWizardDirectory;
  return projectDirectoryPrefill(directory) ? directory as string : undefined;
}

export type WizardReferenceSegment = { kind: 'text'; text: string } | { kind: 'wizard'; directory: string };

/** Accept JSON-escaped paths as well as literal Windows paths. Incomplete streamed references stay text. */
export function splitWizardReferences(text: string): WizardReferenceSegment[] {
  const pattern = /@new_project_wizard\(\s*"((?:\\.|[^"\\])*)"\s*\)/g;
  const segments: WizardReferenceSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    let directory = match[1];
    try {
      const decoded: string = JSON.parse(`"${directory}"`);
      if (!/[\u0000-\u001f]/.test(decoded)) directory = decoded;
    } catch { /* A literal Windows path need not be JSON-escaped. */ }
    if (!projectDirectoryPrefill(directory)) continue;
    if (match.index! > last) segments.push({ kind: 'text', text: text.slice(last, match.index) });
    segments.push({ kind: 'wizard', directory });
    last = match.index! + match[0].length;
  }
  if (last < text.length) segments.push({ kind: 'text', text: text.slice(last) });
  return segments.length ? segments : [{ kind: 'text', text }];
}
