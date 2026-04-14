const DEFAULT_BACKEND_URL = 'http://127.0.0.1:18000';

export type ConnectionPhase = 'connecting' | 'online' | 'offline';

export type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

export type TranslatorOption = {
  description: string;
  name: string;
};

export type Job = {
  config_file_name: string;
  created_at: string;
  error: string;
  finished_at: string;
  job_id: string;
  project_dir: string;
  started_at: string;
  status: JobStatus;
  success: boolean;
  translator: string;
};

export type SubmitJobPayload = {
  config_file_name: string;
  project_dir: string;
  translator: string;
  backend_profile?: string;
};

type TranslatorsResponse = {
  translators: TranslatorOption[];
};

type JobsResponse = {
  jobs: Job[];
};

type ErrorResponse = {
  error?: string;
};

// ---- Project API types ----

export type ProjectConfigResponse = {
  config: Record<string, unknown>;
  project_dir: string;
  config_file_name: string;
};

export type ProjectConfigUpdatePayload = {
  config: Record<string, unknown>;
  config_file_name: string;
};

export type FileEntry = {
  name: string;
  is_file: boolean;
  size: number;
  modified: string;
  entry_count?: number;
};

export type ProjectFilesResponse = {
  project_dir: string;
  input_dir: string;
  output_dir: string;
  cache_dir: string;
  input_files: FileEntry[];
  output_files: FileEntry[];
  cache_files: FileEntry[];
};

export type CacheFileResponse = {
  project_dir: string;
  filename: string;
  entries: CacheEntry[];
};

export type CacheEntry = {
  index: number;
  name: string | string[];
  pre_src: string;
  post_src: string;
  pre_dst: string;
  proofread_dst?: string;
  trans_by?: string;
  proofread_by?: string;
  problem?: string;
  trans_conf?: number;
  doub_content?: string;
  unknown_proper_noun?: string;
  // 旧key名兼容字段（读取旧缓存时可能存在）
  pre_jp?: string;
  post_jp?: string;
  pre_zh?: string;
  proofread_zh?: string;
  post_zh_preview?: string;
  post_dst_preview?: string;
};

export type CacheSearchField = 'all' | 'src' | 'dst';

export type CacheSearchResult = {
  filename: string;
  index: number;
  speaker: string | string[];
  post_src: string;
  pre_dst: string;
  match_src: boolean;
  match_dst: boolean;
  problem: string;
  trans_by: string;
};

export type CacheSearchResponse = {
  results: CacheSearchResult[];
  total: number;
};

export type CacheReplaceField = 'src' | 'dst' | 'all';

export type CacheReplaceFileDetail = {
  filename: string;
  matches: number;
  entries?: CacheEntry[];
};

export type CacheReplaceResponse = {
  success: boolean;
  total_matches: number;
  total_files: number;
  dry_run: boolean;
  file_details: CacheReplaceFileDetail[];
};

export type FileProgress = {
  filename: string;
  total: number;
  translated: number;
  problems: number;
  failed: number;
};

export type ProjectProgressResponse = {
  project_dir: string;
  total: number;
  translated: number;
  problems: number;
  failed: number;
  files: FileProgress[];
};

export type RuntimeJob = {
  job_id: string;
  status: JobStatus;
  translator: string;
  created_at: string;
  started_at: string;
  finished_at: string;
  error?: string;
};

export type ProjectRuntimeSummary = {
  total: number;
  translated: number;
  problems: number;
  failed: number;
  percent: number;
  workers_active: number;
  workers_configured: number;
  translation_speed_lpm: number;
  eta_seconds: number | null;
  updated_at: string;
};

export type ProjectRuntimeErrorEntry = {
  id: string;
  ts: string;
  kind: string;
  level: string;
  message: string;
  filename: string;
  index_range: string;
  retry_count: number | null;
  model: string;
  sleep_seconds: number | null;
};

export type ProjectRuntimeSuccessEntry = {
  id: string;
  ts: string;
  filename: string;
  index: number;
  speaker: string | string[] | null;
  source_preview: string;
  translation_preview: string;
  trans_by: string;
};

export type ProjectRuntimeResponse = {
  project_dir: string;
  job: RuntimeJob | null;
  summary: ProjectRuntimeSummary;
  current_file: string;
  recent_errors: ProjectRuntimeErrorEntry[];
  recent_successes: ProjectRuntimeSuccessEntry[];
  files: FileProgress[];
};

export type StopProjectResponse = {
  success: boolean;
  project_dir: string;
  job_id: string;
  status: JobStatus;
  message: string;
};

export type DictFileContent = {
  path: string;
  lines: string[];
  count: number;
  error?: string;
};

export type ProjectDictionaryResponse = {
  project_dir: string;
  default_dict_folder: string;
  pre_dict_files: string[];
  gpt_dict_files: string[];
  post_dict_files: string[];
  dict_contents: Record<string, DictFileContent>;
};

export type DictionaryCategory = 'pre' | 'gpt' | 'post';

export type ProjectDictionaryManagerResponse = {
  project_dir: string;
  config_file_name: string;
  pre_dict_files: string[];
  gpt_dict_files: string[];
  post_dict_files: string[];
  dict_contents: Record<string, DictFileContent>;
};

export type CommonDictionaryManagerResponse = {
  dict_dir: string;
  pre_dict_files: string[];
  gpt_dict_files: string[];
  post_dict_files: string[];
  dict_contents: Record<string, DictFileContent>;
};

export type ProblemEntry = {
  filename: string;
  index: number;
  speaker: string | string[];
  post_src: string;
  pre_dst: string;
  problem: string;
  trans_by: string;
  // 旧key名兼容
  post_jp?: string;
  pre_zh?: string;
};

export type ProjectProblemsResponse = {
  project_dir: string;
  problems: ProblemEntry[];
  total: number;
};

export type ProjectLogsResponse = {
  project_dir: string;
  exists: boolean;
  total_lines?: number;
  lines: string[];
};

export type PluginInfo = {
  name: string;
  display_name: string;
  version: string;
  author: string;
  description: string;
  type: string;
  module: string;
  settings: Record<string, unknown>;
};

export type AppSettings = {
  printTranslationLogInTerminal: boolean;
};

export type PluginsResponse = {
  plugins: PluginInfo[];
};

// ---- Project ID helpers ----

export function encodeProjectDir(projectDir: string): string {
  // Use base64url encoding for safe URL paths
  const bytes = new TextEncoder().encode(projectDir);
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function decodeProjectDir(token: string): string {
  // Restore base64 padding and characters
  let base64 = token.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4 !== 0) {
    base64 += '=';
  }
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new TextDecoder().decode(bytes);
}

// ---- API Error ----

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

// ---- Existing API functions ----

export async function fetchTranslators() {
  const response = await apiRequest<TranslatorsResponse>('/api/translators');
  return response.translators;
}

export async function fetchJobs() {
  const response = await apiRequest<JobsResponse>('/api/jobs');
  return response.jobs;
}

export async function submitJob(payload: SubmitJobPayload) {
  return apiRequest<Job>('/api/jobs', {
    body: JSON.stringify(payload),
    headers: {
      'Content-Type': 'application/json',
    },
    method: 'POST',
  });
}

// ---- Project API functions ----

export async function fetchProjectConfig(projectId: string, configFileName = 'config.yaml') {
  return apiRequest<ProjectConfigResponse>(
    `/api/projects/${projectId}/config?config=${encodeURIComponent(configFileName)}`,
  );
}

export async function updateProjectConfig(projectId: string, payload: ProjectConfigUpdatePayload) {
  return apiRequest<{ success: boolean; project_dir: string; config_file_name: string }>(
    `/api/projects/${projectId}/config`,
    {
      body: JSON.stringify(payload),
      headers: {
        'Content-Type': 'application/json',
      },
      method: 'PUT',
    },
  );
}

export async function fetchProjectFiles(projectId: string) {
  return apiRequest<ProjectFilesResponse>(`/api/projects/${projectId}/files`);
}

export async function fetchProjectCache(projectId: string) {
  return apiRequest<{ project_dir: string; cache_dir: string; files: FileEntry[] }>(
    `/api/projects/${projectId}/cache`,
  );
}

export async function fetchCacheFile(projectId: string, filename: string) {
  return apiRequest<CacheFileResponse>(
    `/api/projects/${projectId}/cache/${encodeURIComponent(filename)}`,
  );
}

export async function saveCacheFile(projectId: string, filename: string, entries: CacheEntry[], configFileName?: string) {
  return apiRequest<{ success: boolean; filename: string; entries?: CacheEntry[] }>(
    `/api/projects/${projectId}/cache/save`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, entries, config_file_name: configFileName || 'config.yaml' }),
    },
  );
}

export async function deleteCacheEntry(projectId: string, filename: string, index: number) {
  return apiRequest<{ success: boolean; filename: string; deleted_index: number }>(
    `/api/projects/${projectId}/cache/delete-entry`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, index }),
    },
  );
}

export async function searchCache(
  projectId: string,
  query: string,
  field: CacheSearchField = 'all',
  maxResults = 500,
) {
  return apiRequest<CacheSearchResponse>(
    `/api/projects/${projectId}/cache/search`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, field, max_results: maxResults }),
    },
  );
}

export async function replaceCache(
  projectId: string,
  query: string,
  replacement: string,
  field: CacheReplaceField = 'dst',
  dryRun = false,
) {
  return apiRequest<CacheReplaceResponse>(
    `/api/projects/${projectId}/cache/replace`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, replacement, field, dry_run: dryRun }),
    },
  );
}

export async function fetchProjectProgress(projectId: string) {
  return apiRequest<ProjectProgressResponse>(`/api/projects/${projectId}/progress`);
}

export async function fetchProjectRuntime(projectId: string) {
  return apiRequest<ProjectRuntimeResponse>(`/api/projects/${projectId}/runtime`);
}

export async function stopProjectTranslation(projectId: string) {
  return apiRequest<StopProjectResponse>(`/api/projects/${projectId}/stop`, {
    method: 'POST',
  });
}

export async function fetchProjectDictionary(projectId: string, configFileName = 'config.yaml') {
  return apiRequest<ProjectDictionaryResponse>(
    `/api/projects/${projectId}/dictionary?config=${encodeURIComponent(configFileName)}`,
  );
}

export async function fetchProjectDictionaryManager(projectId: string, configFileName = 'config.yaml') {
  return apiRequest<ProjectDictionaryManagerResponse>(
    `/api/projects/${projectId}/dictionary/project?config=${encodeURIComponent(configFileName)}`,
  );
}

export async function createProjectDictionaryFile(
  projectId: string,
  payload: { config_file_name: string; category: DictionaryCategory; filename: string },
) {
  return apiRequest<{ success: boolean; file_key: string; path: string }>(
    `/api/projects/${projectId}/dictionary/project/create`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function saveProjectDictionaryFile(
  projectId: string,
  payload: { config_file_name: string; file_key: string; content: string },
) {
  return apiRequest<{ success: boolean; file_key: string }>(
    `/api/projects/${projectId}/dictionary/project/save`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function deleteProjectDictionaryFile(
  projectId: string,
  payload: { config_file_name: string; file_key: string; delete_file?: boolean },
) {
  return apiRequest<{ success: boolean; file_key: string; deleted_file: boolean }>(
    `/api/projects/${projectId}/dictionary/project/delete`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function fetchCommonDictionaryManager() {
  return apiRequest<CommonDictionaryManagerResponse>('/api/dictionaries/common');
}

export async function createCommonDictionaryFile(payload: { category: DictionaryCategory; filename: string }) {
  return apiRequest<{ success: boolean; filename: string; path: string }>(
    '/api/dictionaries/common/create',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function saveCommonDictionaryFile(payload: { filename: string; content: string }) {
  return apiRequest<{ success: boolean; filename: string }>(
    '/api/dictionaries/common/save',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function deleteCommonDictionaryFile(payload: { filename: string }) {
  return apiRequest<{ success: boolean; filename: string }>(
    '/api/dictionaries/common/delete',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  );
}

export async function fetchProjectProblems(projectId: string) {
  return apiRequest<ProjectProblemsResponse>(`/api/projects/${projectId}/problems`);
}

export async function fetchProjectLogs(projectId: string, tail = 2000) {
  return apiRequest<ProjectLogsResponse>(
    `/api/projects/${projectId}/logs?tail=${tail}`,
  );
}

export async function fetchPlugins() {
  const response = await apiRequest<PluginsResponse>('/api/plugins');
  return response.plugins;
}

export async function fetchAppSettings() {
  return apiRequest<AppSettings>('/api/app-settings');
}

export async function updateAppSettings(settings: AppSettings) {
  return apiRequest<AppSettings>('/api/app-settings', {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(settings),
  });
}

// ---- Backend Profiles API types ----

export type BackendProfilesResponse = {
  profiles: Record<string, Record<string, unknown>>;
};

export type BackendProfileResponse = {
  name: string;
  profile: Record<string, unknown>;
};

// ---- Backend Profiles API functions ----

export async function fetchBackendProfiles() {
  return apiRequest<BackendProfilesResponse>('/api/backend-profiles');
}

export async function fetchBackendProfile(name: string) {
  return apiRequest<BackendProfileResponse>(
    `/api/backend-profiles/${encodeURIComponent(name)}`,
  );
}

export async function createBackendProfile(name: string, profile: Record<string, unknown>) {
  return apiRequest<{ success: boolean; name: string }>(
    `/api/backend-profiles/${encodeURIComponent(name)}`,
    {
      body: JSON.stringify({ profile }),
      headers: {
        'Content-Type': 'application/json',
      },
      method: 'PUT',
    },
  );
}

export async function updateBackendProfile(name: string, profile: Record<string, unknown>) {
  return apiRequest<{ success: boolean; name: string }>(
    `/api/backend-profiles/${encodeURIComponent(name)}`,
    {
      body: JSON.stringify({ profile }),
      headers: {
        'Content-Type': 'application/json',
      },
      method: 'PUT',
    },
  );
}

export async function deleteBackendProfile(name: string) {
  return apiRequest<{ success: boolean; name: string }>(
    `/api/backend-profiles/${encodeURIComponent(name)}`,
    {
      method: 'DELETE',
    },
  );
}

// ---- Backend Profile Selection (localStorage) ----

const BACKEND_PROFILE_KEY = 'galtransl-backend-profile';
const DEFAULT_BACKEND_PROFILE_KEY = 'galtransl-default-backend-profile';
const TRANSLATOR_TEMPLATE_KEY = 'galtransl-project-translator-template';

/** Get the global default backend profile name. */
export function getDefaultBackendProfile(): string {
  try {
    return localStorage.getItem(DEFAULT_BACKEND_PROFILE_KEY) || '';
  } catch {
    return '';
  }
}

/** Set the global default backend profile name. Pass empty to clear. */
export function setDefaultBackendProfile(name: string) {
  try {
    if (name) {
      localStorage.setItem(DEFAULT_BACKEND_PROFILE_KEY, name);
    } else {
      localStorage.removeItem(DEFAULT_BACKEND_PROFILE_KEY);
    }
  } catch {
    // ignore storage errors
  }
}

/**
 * Get the backend profile selected for a specific project.
 * Falls back to the global default if no project-specific selection exists.
 */
export function getSelectedBackendProfile(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(BACKEND_PROFILE_KEY) || '{}');
    if (map[projectDir] !== undefined) {
      return map[projectDir]; // may be empty string (explicitly chose "不使用")
    }
    // No project-specific selection → fall back to global default
    return getDefaultBackendProfile();
  } catch {
    return getDefaultBackendProfile();
  }
}

export function setSelectedBackendProfile(projectDir: string, profileName: string) {
  try {
    const map = JSON.parse(localStorage.getItem(BACKEND_PROFILE_KEY) || '{}');
    // Store even empty string — it means "explicitly don't use any global config".
    // A missing key means "fall back to default".
    map[projectDir] = profileName;
    localStorage.setItem(BACKEND_PROFILE_KEY, JSON.stringify(map));
  } catch {
    // ignore storage errors
  }
}

/**
 * Check whether a project has an explicit backend profile selection
 * (as opposed to falling back to the global default).
 */
export function hasExplicitBackendProfile(projectDir: string): boolean {
  try {
    const map = JSON.parse(localStorage.getItem(BACKEND_PROFILE_KEY) || '{}');
    return projectDir in map;
  } catch {
    return false;
  }
}

// ---- Translator Template Selection (localStorage) ----

/**
 * Get the translator template selected for a specific project.
 */
export function getSelectedTranslatorTemplate(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(TRANSLATOR_TEMPLATE_KEY) || '{}');
    return typeof map[projectDir] === 'string' ? map[projectDir] : '';
  } catch {
    return '';
  }
}

/**
 * Persist translator template selection for a specific project.
 */
export function setSelectedTranslatorTemplate(projectDir: string, translatorName: string) {
  try {
    const map = JSON.parse(localStorage.getItem(TRANSLATOR_TEMPLATE_KEY) || '{}');
    map[projectDir] = translatorName;
    localStorage.setItem(TRANSLATOR_TEMPLATE_KEY, JSON.stringify(map));
  } catch {
    // ignore storage errors
  }
}

// ---- Internal ----

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const baseUrl = getBackendBaseUrl();

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, init);
  } catch {
    throw new ApiError(`无法连接到后端：${baseUrl}`, 0);
  }

  const data = (await response.json().catch(() => ({}))) as T & ErrorResponse;
  if (!response.ok) {
    throw new ApiError(data.error || `请求失败：${response.status}`, response.status);
  }

  return data;
}

function getBackendBaseUrl() {
  const configured = import.meta.env.VITE_BACKEND_URL?.trim();
  return configured ? configured.replace(/\/$/, '') : DEFAULT_BACKEND_URL;
}
