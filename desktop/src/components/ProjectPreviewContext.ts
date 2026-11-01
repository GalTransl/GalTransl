import { createContext } from 'react';

export type DictionaryPreviewTarget = { page: 'dictionary'; fileKey: string; line?: string };
export type CachePreviewTarget = { page: 'cache'; filename?: string; index?: number; query?: string };
export type ProjectPreviewTarget = { page: 'translate' } | DictionaryPreviewTarget | CachePreviewTarget;
export type ProjectPreviewRequest = ProjectPreviewTarget & { projectDir: string; configFileName?: string };

export const ProjectPreviewContext = createContext<((request: ProjectPreviewRequest) => void) | null>(null);
export const ProjectPreviewScopeContext = createContext<{ projectDir: string; configFileName?: string } | null>(null);
