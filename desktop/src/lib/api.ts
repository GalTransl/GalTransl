import { invoke } from '@tauri-apps/api/core';

import type { PermissionDecision, PermissionMode } from './permissionMode';

const DEFAULT_BACKEND_URL = 'http://127.0.0.1:12333';
let runtimeBackendBaseUrl: string | null = null;

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
  gendic_added_entries?: number;
  gendic_duplicated_entries?: number;
};

export type PromptTemplateOverride = {
  system_prompt?: string;
  user_prompt?: string;
};

export type SubmitJobPayload = {
  config_file_name: string;
  project_dir: string;
  translator: string;
  backend_profile?: string;
  backend_profile_data?: Record<string, unknown>;
  prompt_template_overrides?: Record<string, PromptTemplateOverride>;
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

type ProjectConfigTemplateResponse = {
  content: string;
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
  // 用于标记条目是否被删除（前端状态，不会发送到后端）
  deleted?: boolean;
};

export type CacheSearchField = 'all' | 'src' | 'dst' | 'problem';

export type CacheSearchResult = {
  filename: string;
  index: number;
  speaker: string | string[];
  post_src: string;
  pre_dst: string;
  match_src: boolean;
  match_dst: boolean;
  match_problem: boolean;
  problem: string;
  trans_by: string;
};

export type CacheSearchResponse = {
  results: CacheSearchResult[];
  total: number;
};

export type CacheReplaceField = 'src' | 'dst' | 'all';

export type CacheSearchOptions = {
  re: boolean;
};

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
  gendic_added_entries?: number;
  gendic_duplicated_entries?: number;
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

export type ProjectRetranslStatEntry = {
  key: string;
  count: number;
};

export type ProjectRuntimeResponse = {
  project_dir: string;
  job: RuntimeJob | null;
  summary: ProjectRuntimeSummary;
  stage: string;
  current_file: string;
  recent_errors: ProjectRuntimeErrorEntry[];
  recent_successes: ProjectRuntimeSuccessEntry[];
  retransl_stats: ProjectRetranslStatEntry[];
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
  mtime?: number | null;
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
  filter_keys?: string[];
};

// ---- Name Table API types ----

export type NameEntry = {
  src_name: string;
  dst_name: string;
  count: number;
};

export type NameTableResponse = {
  project_dir: string;
  source_file: string | null;
  names: NameEntry[];
};

export type NameTableGenerateResponse = {
  success: boolean;
  source_file: string;
  names: NameEntry[];
  total: number;
};

export type NameTableSaveResponse = {
  success: boolean;
  source_file: string;
  total: number;
};

export type NameDictResponse = {
  project_dir: string;
  name_dict: Record<string, string>;
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

export type ThemeMode = 'light' | 'dark' | 'system';

export type CustomBackgroundPreference = {
  imageDataUrl: string;
  imageName: string;
  opacity: number;
  surfaceOpacity: number;
};

export type PluginsResponse = {
  plugins: PluginInfo[];
};

export type ProblemTypeInfo = {
  name: string;
  description: string;
};

export type ProblemTypesResponse = {
  problem_types: ProblemTypeInfo[];
};

export type PromptTemplateInfo = {
  name: string;
  description: string;
  default_system_prompt: string;
  system_prompt: string;
  system_overridden: boolean;
  default_user_prompt: string;
  user_prompt: string;
  user_overridden: boolean;
  overridden: boolean;
};

export type PromptTemplatesResponse = {
  templates: PromptTemplateInfo[];
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

export type VersionCheckResponse = {
  version: string;
  latest_version: string | null;
  update_available: boolean;
};

export async function fetchVersion() {
  const response = await apiRequest<{ version: string }>('/api/version');
  return response.version;
}

export async function fetchVersionCheck() {
  return apiRequest<VersionCheckResponse>('/api/version/check');
}

export async function ensureDesktopBackendReady(options?: { hideConsole?: boolean; timeoutMs?: number }) {
  if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window) || !shouldUseManagedDesktopBackend()) {
    return null;
  }

  return invoke<string>('ensure_backend_ready', {
    hideConsole: options?.hideConsole ?? getHideBackendConsolePreference(),
    timeoutMs: options?.timeoutMs,
  });
}

export async function fetchTranslators() {
  const response = await apiRequest<TranslatorsResponse>('/api/translators');
  return response.translators;
}

export async function fetchJobs() {
  const response = await apiRequest<JobsResponse>('/api/jobs');
  return response.jobs;
}

export async function fetchJob(jobId: string) {
  return apiRequest<Job>(`/api/jobs/${jobId}`);
}

export async function submitJob(payload: SubmitJobPayload) {
  const overrides = getPromptTemplateOverridesForJob(payload.translator);
  const payloadWithOverrides = Object.keys(overrides).length > 0
    ? { ...payload, prompt_template_overrides: overrides }
    : payload;
  return apiRequest<Job>('/api/jobs', {
    body: JSON.stringify(payloadWithOverrides),
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
  // no-store：缓存文件会被 Agent 的 patch/delete 工具改写，读它必须拿到磁盘上的当前内容。
  // 后端没给缓存头，浏览器/Electron 的 HTTP 缓存没有可用的过期与校验信息，不让它插手最稳。
  return apiRequest<CacheFileResponse>(
    `/api/projects/${projectId}/cache/${encodeURIComponent(filename)}`,
    { cache: 'no-store' },
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

export async function deleteCacheFiles(projectId: string, filenames: string[]) {
  return apiRequest<{ success: boolean; deleted_files: string[]; not_found_files: string[] }>(
    `/api/projects/${projectId}/cache/delete-file`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filenames }),
    },
  );
}

export async function searchCache(
  projectId: string,
  query: string,
  field: CacheSearchField = 'all',
  options: CacheSearchOptions = { re: false },
  maxResults = 500,
  configFileName = 'config.yaml',
) {
  return apiRequest<CacheSearchResponse>(
    `/api/projects/${projectId}/cache/search`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, field, options, max_results: maxResults, config_file_name: configFileName }),
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

export async function fetchProjectProblems(projectId: string, configFileName = 'config.yaml') {
  return apiRequest<ProjectProblemsResponse>(`/api/projects/${projectId}/problems?config=${encodeURIComponent(configFileName)}`);
}

// ---- Name Table API functions ----

export async function fetchNameTable(projectId: string) {
  return apiRequest<NameTableResponse>(`/api/projects/${projectId}/name-table`);
}

export async function generateNameTable(projectId: string) {
  return apiRequest<NameTableGenerateResponse>(`/api/projects/${projectId}/name-table/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function saveNameTable(projectId: string, names: NameEntry[]) {
  return apiRequest<NameTableSaveResponse>(`/api/projects/${projectId}/name-table/save`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ names }),
  });
}

export function getAiTranslateUrl(projectId: string) {
  const baseUrl = getBackendBaseUrl();
  return `${baseUrl}/api/projects/${projectId}/name-table/ai-translate`;
}

export async function fetchNameDict(projectId: string) {
  return apiRequest<NameDictResponse>(`/api/projects/${projectId}/name-dict`);
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

export async function fetchProblemTypes() {
  const response = await apiRequest<ProblemTypesResponse>('/api/problem-types');
  return response.problem_types;
}

export async function fetchTranslationGuidelines() {
  const response = await apiRequest<{ guidelines: string[] }>('/api/translation-guidelines');
  return response.guidelines;
}

/** 项目翻译规范：项目目录里的一个文件（不是配置项），翻译时拼在全局规范之后。 */
export type ProjectGuidelineResponse = {
  filename: string;
  path: string;
  exists: boolean;
  content: string;
};

export async function fetchProjectGuideline(projectId: string) {
  return apiRequest<ProjectGuidelineResponse>(`/api/projects/${projectId}/guideline`);
}

/** 写项目规范。mode：overwrite 覆写 / append 增写 / replace 替换（old_text 需唯一命中）。 */
export async function saveProjectGuideline(
  projectId: string,
  payload: {
    mode: 'overwrite' | 'append' | 'replace';
    content?: string;
    old_text?: string;
    new_text?: string;
  },
) {
  return apiRequest<{
    success: boolean;
    filename: string;
    path: string;
    mode: string;
    created: boolean;
    length: number;
  }>(`/api/projects/${projectId}/guideline`, {
    body: JSON.stringify(payload),
    headers: {
      'Content-Type': 'application/json',
    },
    method: 'PUT',
  });
}

export async function fetchAppSettings() {
  return apiRequest<AppSettings>('/api/app-settings');
}

export async function fetchDefaultProjectConfigTemplate() {
  const response = await apiRequest<ProjectConfigTemplateResponse>('/api/project-config-template');
  return response.content;
}

export async function fetchPromptTemplates() {
  return apiRequest<PromptTemplatesResponse>('/api/prompt-templates');
}

// ---- Prompt Template localStorage helpers ----

const PROMPT_TEMPLATES_OVERRIDES_KEY = 'galtransl_prompt_templates_overrides';

export function loadPromptTemplateOverrides(): Record<string, PromptTemplateOverride> {
  try {
    const raw = localStorage.getItem(PROMPT_TEMPLATES_OVERRIDES_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (typeof parsed === 'object' && parsed !== null) {
      return parsed as Record<string, PromptTemplateOverride>;
    }
    return {};
  } catch {
    return {};
  }
}

export function savePromptTemplateOverrides(overrides: Record<string, PromptTemplateOverride>): void {
  try {
    localStorage.setItem(PROMPT_TEMPLATES_OVERRIDES_KEY, JSON.stringify(overrides));
  } catch {
    // ignore storage errors
  }
}

export function getPromptTemplateOverride(name: string): PromptTemplateOverride | null {
  const overrides = loadPromptTemplateOverrides();
  const override = overrides[name];
  if (override && typeof override === 'object') {
    return override;
  }
  return null;
}

export function setPromptTemplateOverride(name: string, override: PromptTemplateOverride): void {
  const overrides = loadPromptTemplateOverrides();
  overrides[name] = override;
  savePromptTemplateOverrides(overrides);
}

export function deletePromptTemplateOverride(name: string): void {
  const overrides = loadPromptTemplateOverrides();
  delete overrides[name];
  savePromptTemplateOverrides(overrides);
}

export function getPromptTemplateOverridesForJob(translator: string): Record<string, PromptTemplateOverride> {
  const overrides = loadPromptTemplateOverrides();
  const override = overrides[translator];
  if (!override) return {};
  return { [translator]: override };
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

type BackendProfilesMap = Record<string, Record<string, unknown>>;

// ---- Backend Profiles API functions ----

export async function fetchBackendProfiles() {
  return {
    profiles: readBackendProfilesStorage(),
  } satisfies BackendProfilesResponse;
}

export async function fetchBackendProfile(name: string) {
  const profile = getBackendProfile(name);
  if (!profile) {
    throw new Error(`profile not found: ${name}`);
  }
  return { name, profile } satisfies BackendProfileResponse;
}

export async function createBackendProfile(name: string, profile: Record<string, unknown>) {
  const trimmedName = name.trim();
  if (!trimmedName) {
    throw new Error('profile name is required');
  }
  const profiles = readBackendProfilesStorage();
  const isFirstProfile = Object.keys(profiles).length === 0;
  profiles[trimmedName] = cloneBackendProfile(profile);
  writeBackendProfilesStorage(profiles);
  if (isFirstProfile) {
    // 首个配置：同时设为翻译器默认 + Agent 默认（两者独立，新装好都给它最省心）
    setDefaultBackendProfile(trimmedName);
    setAgentDefaultBackendProfile(trimmedName);
  }
  return { success: true, name: trimmedName };
}

export async function updateBackendProfile(name: string, profile: Record<string, unknown>) {
  return createBackendProfile(name, profile);
}

export async function deleteBackendProfile(name: string) {
  const trimmedName = name.trim();
  if (!trimmedName) {
    throw new Error('profile name is required');
  }
  const profiles = readBackendProfilesStorage();
  if (!(trimmedName in profiles)) {
    throw new Error(`profile not found: ${trimmedName}`);
  }
  delete profiles[trimmedName];
  writeBackendProfilesStorage(profiles);
  // 删配置时：若它是翻译器默认就清翻译器默认、若是 Agent 默认就清 Agent 默认（互不影响）
  if (getDefaultBackendProfile() === trimmedName) {
    setDefaultBackendProfile('');
  }
  if (getAgentDefaultBackendProfile() === trimmedName) {
    setAgentDefaultBackendProfile('');
  }
  return { success: true, name: trimmedName };
}

// ---- OpenAI-Compatible model list query ----

export interface FetchOpenAIModelsPayload {
  endpoint: string;
  token: string;
  proxy?: { http?: string; https?: string } | string | null;
  timeout?: number;
}

export interface FetchOpenAIModelsResponse {
  models: string[];
  url: string;
}

export async function fetchOpenAIModels(payload: FetchOpenAIModelsPayload) {
  return apiRequest<FetchOpenAIModelsResponse>('/api/openai-models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// ---- Backend Profile Selection (localStorage) ----

const BACKEND_PROFILE_KEY = 'galtransl-backend-profile';
const BACKEND_PROFILES_STORAGE_KEY = 'galtransl-backend-profiles';
const DEFAULT_BACKEND_PROFILE_KEY = 'galtransl-default-backend-profile';
const TRANSLATOR_TEMPLATE_KEY = 'galtransl-project-translator-template';
const HOME_HISTORY_LIMIT_KEY = 'galtransl-home-history-limit';
const HOME_JOB_LIMIT_KEY = 'galtransl-home-job-limit';
const THEME_MODE_KEY = 'galtransl-theme-mode';
const CUSTOM_BACKGROUND_KEY = 'galtransl-custom-background';
const HIDE_BACKEND_CONSOLE_KEY = 'galtransl-hide-backend-console';
const CACHE_BROWSER_FONT_SIZE_KEY = 'galtransl-cache-browser-font-size';

export const HOME_HISTORY_LIMIT_DEFAULT = 20;
export const HOME_JOB_LIMIT_DEFAULT = 20;
export const HOME_LIST_LIMIT_MIN = 1;
export const HOME_LIST_LIMIT_MAX = 200;
export const CUSTOM_BACKGROUND_OPACITY_MIN = 0;
export const CUSTOM_BACKGROUND_OPACITY_MAX = 80;
export const CUSTOM_BACKGROUND_OPACITY_DEFAULT = 35;
export const CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN = 18;
export const CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX = 92;
export const CUSTOM_BACKGROUND_SURFACE_OPACITY_DEFAULT = 33;
export const HIDE_BACKEND_CONSOLE_DEFAULT = true;
export const CACHE_BROWSER_FONT_SIZE_MIN = 11;
export const CACHE_BROWSER_FONT_SIZE_MAX = 20;
export const CACHE_BROWSER_FONT_SIZE_DEFAULT = 14;

/** Custom event dispatched when the global default backend profile changes. */
export const BACKEND_PROFILES_CHANGE_EVENT = 'galtransl:backend-profiles-change';
export const DEFAULT_BACKEND_PROFILE_CHANGE_EVENT = 'galtransl:default-backend-profile-change';
export const PROJECT_CONFIG_DIRTY_CHANGE_EVENT = 'galtransl:project-config-dirty-change';
export const HOME_HISTORY_LIMIT_CHANGE_EVENT = 'galtransl:home-history-limit-change';
export const HOME_JOB_LIMIT_CHANGE_EVENT = 'galtransl:home-job-limit-change';
export const THEME_MODE_CHANGE_EVENT = 'galtransl:theme-mode-change';
export const CUSTOM_BACKGROUND_CHANGE_EVENT = 'galtransl:custom-background-change';
export const HIDE_BACKEND_CONSOLE_CHANGE_EVENT = 'galtransl:hide-backend-console-change';
export const CACHE_BROWSER_FONT_SIZE_CHANGE_EVENT = 'galtransl:cache-browser-font-size-change';

/* ── 已打开项目（open projects）的共享读写 + 广播 ──
 * 翻译器（App.tsx）和 Agent 页面都用这一套，保证两边的"已打开项目"列表
 * 始终同步：任一处 addOpenProject 都会写 localStorage 并广播，另一处监听
 * 后更新自己的 state。单一数据源 = localStorage，单一写入路径 = addOpenProject。 */
export const OPEN_PROJECTS_KEY = 'galtransl-open-projects';
export const OPEN_PROJECTS_CHANGE_EVENT = 'galtransl:open-projects-change';
const CONFIG_FILE_KEY = 'galtransl-config-file';

export function loadOpenProjects(): string[] {
  try {
    const raw = localStorage.getItem(OPEN_PROJECTS_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function saveOpenProjects(projects: string[]): void {
  try {
    localStorage.setItem(OPEN_PROJECTS_KEY, JSON.stringify(projects));
    window.dispatchEvent(new CustomEvent(OPEN_PROJECTS_CHANGE_EVENT, { detail: projects }));
  } catch {
    // ignore storage errors
  }
}

export function persistOpenProjects(projects: string[]): void {
  // 静默写盘（不广播）：App 在自己 state 变更后调它做持久化，
  // 避免与 OPEN_PROJECTS_CHANGE 监听器形成自回环。外部写入路径
  // （addOpenProject）用 saveOpenProjects，会广播通知监听方。
  try {
    localStorage.setItem(OPEN_PROJECTS_KEY, JSON.stringify(projects));
  } catch {
    // ignore storage errors
  }
}

export function readConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
  }
}

export function saveConfigFileName(projectDir: string, configFileName: string): void {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    map[projectDir] = configFileName;
    localStorage.setItem(CONFIG_FILE_KEY, JSON.stringify(map));
  } catch {
    // ignore storage errors
  }
}

/** 注册一个已打开项目（幂等）。已存在则只刷新其 config 文件名、保持原顺序；
 *  不存在则前插。写后广播 OPEN_PROJECTS_CHANGE_EVENT，监听方据此同步。 */
export function addOpenProject(projectDir: string, configFileName = 'config.yaml'): void {
  if (!projectDir) return;
  saveConfigFileName(projectDir, configFileName || 'config.yaml');
  const prev = loadOpenProjects();
  if (prev.includes(projectDir)) return;
  saveOpenProjects([projectDir, ...prev]);
}

const dirtyProjectConfigDirs = new Set<string>();

/** Return whether a project's config page has unsaved changes in this session. */
export function isProjectConfigDirty(projectDir: string): boolean {
  return Boolean(projectDir) && dirtyProjectConfigDirs.has(projectDir);
}

/** Update a project's unsaved state and notify persistent UI such as the sidebar. */
export function setProjectConfigDirty(projectDir: string, dirty: boolean) {
  if (!projectDir) return;
  if (dirty) {
    dirtyProjectConfigDirs.add(projectDir);
  } else {
    dirtyProjectConfigDirs.delete(projectDir);
  }
  window.dispatchEvent(new CustomEvent(PROJECT_CONFIG_DIRTY_CHANGE_EVENT, {
    detail: { projectDir, dirty },
  }));
}

function cloneBackendProfile(profile: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(profile ?? {})) as Record<string, unknown>;
}

function readBackendProfilesStorage(): BackendProfilesMap {
  try {
    const raw = localStorage.getItem(BACKEND_PROFILES_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {};
    }
    const profiles: BackendProfilesMap = {};
    for (const [name, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!name.trim()) {
        continue;
      }
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        profiles[name] = cloneBackendProfile(value as Record<string, unknown>);
      }
    }
    return profiles;
  } catch {
    return {};
  }
}

function writeBackendProfilesStorage(profiles: BackendProfilesMap) {
  try {
    localStorage.setItem(BACKEND_PROFILES_STORAGE_KEY, JSON.stringify(profiles));
    window.dispatchEvent(new CustomEvent(BACKEND_PROFILES_CHANGE_EVENT, { detail: Object.keys(profiles) }));
  } catch {
    // ignore storage errors
  }
}

export function getBackendProfile(name: string): Record<string, unknown> | null {
  const trimmedName = name.trim();
  if (!trimmedName) {
    return null;
  }
  const profiles = readBackendProfilesStorage();
  return profiles[trimmedName] ? cloneBackendProfile(profiles[trimmedName]) : null;
}

export function getBackendProfileNames(): string[] {
  return Object.keys(readBackendProfilesStorage());
}

export function resolveSelectedBackendProfile(projectDir: string): { name: string; profile: Record<string, unknown> | null } {
  const name = getSelectedBackendProfile(projectDir);
  if (!name) {
    return { name: '', profile: null };
  }
  return {
    name,
    profile: getBackendProfile(name),
  };
}

export function getSelectedBackendProfileJobPayload(projectDir: string): Pick<SubmitJobPayload, 'backend_profile' | 'backend_profile_data'> {
  const { name, profile } = resolveSelectedBackendProfile(projectDir);
  if (!profile) {
    return {};
  }
  return {
    ...(name ? { backend_profile: name } : {}),
    ...(profile ? { backend_profile_data: profile } : {}),
  };
}

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
    window.dispatchEvent(new CustomEvent(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, { detail: name }));
  } catch {
    // ignore storage errors
  }
}

/* ── Agent 默认后端配置（与翻译器默认各自独立） ──
 * 同一配置可同时是翻译器默认 + Agent 默认，也可只占其一；两者互不影响。
 * Agent 页用这套；翻译器页继续用上面的 getDefaultBackendProfile（翻译器默认）。 */
const AGENT_DEFAULT_BACKEND_PROFILE_KEY = 'galtransl-agent-default-backend-profile';

/** Custom event dispatched when the Agent default backend profile changes. */
export const AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT = 'galtransl:agent-default-backend-profile-change';

/** Get the Agent default backend profile name (independent of translator default). */
export function getAgentDefaultBackendProfile(): string {
  try {
    return localStorage.getItem(AGENT_DEFAULT_BACKEND_PROFILE_KEY) || '';
  } catch {
    return '';
  }
}

/** Set the Agent default backend profile name. Pass empty to clear. */
export function setAgentDefaultBackendProfile(name: string) {
  try {
    if (name) {
      localStorage.setItem(AGENT_DEFAULT_BACKEND_PROFILE_KEY, name);
    } else {
      localStorage.removeItem(AGENT_DEFAULT_BACKEND_PROFILE_KEY);
    }
    window.dispatchEvent(new CustomEvent(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, { detail: name }));
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

/**
 * Get the backend profile display value for a project's dropdown.
 * Returns '__default__' when no project-specific selection exists (following global default),
 * empty string for "don't use any", or a specific profile name.
 */
export function getSelectedBackendProfileDisplay(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(BACKEND_PROFILE_KEY) || '{}');
    if (map[projectDir] !== undefined) {
      return map[projectDir]; // '' or a specific name
    }
    // No project-specific selection → show as "following default"
    return '__default__';
  } catch {
    return '__default__';
  }
}

export function setSelectedBackendProfile(projectDir: string, profileName: string) {
  try {
    const map = JSON.parse(localStorage.getItem(BACKEND_PROFILE_KEY) || '{}');
    if (profileName === '__default__') {
      // "Follow global default" → remove the project-specific key entirely
      delete map[projectDir];
    } else {
      // Store even empty string — it means "explicitly don't use any global config".
      map[projectDir] = profileName;
    }
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

function normalizeHomeListLimit(value: unknown, fallback: number): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }

  const integer = Math.trunc(numeric);
  if (integer < HOME_LIST_LIMIT_MIN) {
    return HOME_LIST_LIMIT_MIN;
  }
  if (integer > HOME_LIST_LIMIT_MAX) {
    return HOME_LIST_LIMIT_MAX;
  }
  return integer;
}

function normalizeCustomBackgroundSurfaceOpacity(value: unknown): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return CUSTOM_BACKGROUND_SURFACE_OPACITY_DEFAULT;
  }
  const integer = Math.trunc(numeric);
  if (integer < CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN) {
    return CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN;
  }
  if (integer > CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX) {
    return CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX;
  }
  return integer;
}

export function getHomeHistoryRetentionLimit(): number {
  try {
    const raw = localStorage.getItem(HOME_HISTORY_LIMIT_KEY);
    return normalizeHomeListLimit(raw, HOME_HISTORY_LIMIT_DEFAULT);
  } catch {
    return HOME_HISTORY_LIMIT_DEFAULT;
  }
}

export function setHomeHistoryRetentionLimit(limit: number): number {
  const normalized = normalizeHomeListLimit(limit, HOME_HISTORY_LIMIT_DEFAULT);
  try {
    localStorage.setItem(HOME_HISTORY_LIMIT_KEY, String(normalized));
    window.dispatchEvent(new CustomEvent(HOME_HISTORY_LIMIT_CHANGE_EVENT, { detail: normalized }));
  } catch {
    // ignore storage errors
  }
  return normalized;
}

export function getCacheBrowserFontSizePreference(): number {
  try {
    const raw = localStorage.getItem(CACHE_BROWSER_FONT_SIZE_KEY);
    return normalizeCacheBrowserFontSize(raw);
  } catch {
    return CACHE_BROWSER_FONT_SIZE_DEFAULT;
  }
}

export function setCacheBrowserFontSizePreference(size: number): number {
  const normalized = normalizeCacheBrowserFontSize(size);
  try {
    localStorage.setItem(CACHE_BROWSER_FONT_SIZE_KEY, String(normalized));
    window.dispatchEvent(new CustomEvent(CACHE_BROWSER_FONT_SIZE_CHANGE_EVENT, { detail: normalized }));
  } catch {
    // ignore storage errors
  }
  return normalized;
}

export function getHideBackendConsolePreference(): boolean {
  try {
    const raw = localStorage.getItem(HIDE_BACKEND_CONSOLE_KEY);
    return normalizeHideBackendConsole(raw);
  } catch {
    return HIDE_BACKEND_CONSOLE_DEFAULT;
  }
}

export function setHideBackendConsolePreference(enabled: boolean): boolean {
  const normalized = normalizeHideBackendConsole(enabled);
  try {
    localStorage.setItem(HIDE_BACKEND_CONSOLE_KEY, String(normalized));
    window.dispatchEvent(new CustomEvent(HIDE_BACKEND_CONSOLE_CHANGE_EVENT, { detail: normalized }));
  } catch {
    // ignore storage errors
  }
  return normalized;
}

export function getHomeJobRetentionLimit(): number {
  try {
    const raw = localStorage.getItem(HOME_JOB_LIMIT_KEY);
    return normalizeHomeListLimit(raw, HOME_JOB_LIMIT_DEFAULT);
  } catch {
    return HOME_JOB_LIMIT_DEFAULT;
  }
}

export function setHomeJobRetentionLimit(limit: number): number {
  const normalized = normalizeHomeListLimit(limit, HOME_JOB_LIMIT_DEFAULT);
  try {
    localStorage.setItem(HOME_JOB_LIMIT_KEY, String(normalized));
    window.dispatchEvent(new CustomEvent(HOME_JOB_LIMIT_CHANGE_EVENT, { detail: normalized }));
  } catch {
    // ignore storage errors
  }
  return normalized;
}

function normalizeThemeMode(value: unknown): ThemeMode {
  if (value === 'light' || value === 'dark' || value === 'system') {
    return value;
  }
  return 'system';
}

function normalizeCacheBrowserFontSize(value: unknown): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return CACHE_BROWSER_FONT_SIZE_DEFAULT;
  }
  const integer = Math.trunc(numeric);
  if (integer < CACHE_BROWSER_FONT_SIZE_MIN) {
    return CACHE_BROWSER_FONT_SIZE_MIN;
  }
  if (integer > CACHE_BROWSER_FONT_SIZE_MAX) {
    return CACHE_BROWSER_FONT_SIZE_MAX;
  }
  return integer;
}

function normalizeHideBackendConsole(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value;
  }
  if (value === 'true') {
    return true;
  }
  if (value === 'false') {
    return false;
  }
  return HIDE_BACKEND_CONSOLE_DEFAULT;
}

export function getThemeModePreference(): ThemeMode {
  try {
    const raw = localStorage.getItem(THEME_MODE_KEY);
    return normalizeThemeMode(raw);
  } catch {
    return 'system';
  }
}

export function setThemeModePreference(mode: ThemeMode): ThemeMode {
  const normalized = normalizeThemeMode(mode);
  try {
    localStorage.setItem(THEME_MODE_KEY, normalized);
    window.dispatchEvent(new CustomEvent(THEME_MODE_CHANGE_EVENT, { detail: normalized }));
  } catch {
    // ignore storage errors
  }
  return normalized;
}

function normalizeCustomBackgroundOpacity(value: unknown): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return CUSTOM_BACKGROUND_OPACITY_DEFAULT;
  }
  const integer = Math.trunc(numeric);
  if (integer < CUSTOM_BACKGROUND_OPACITY_MIN) {
    return CUSTOM_BACKGROUND_OPACITY_MIN;
  }
  if (integer > CUSTOM_BACKGROUND_OPACITY_MAX) {
    return CUSTOM_BACKGROUND_OPACITY_MAX;
  }
  return integer;
}

function defaultCustomBackgroundPreference(): CustomBackgroundPreference {
  return {
    imageDataUrl: '',
    imageName: '',
    opacity: CUSTOM_BACKGROUND_OPACITY_DEFAULT,
    surfaceOpacity: CUSTOM_BACKGROUND_SURFACE_OPACITY_DEFAULT,
  };
}

function normalizeCustomBackgroundPreference(value: unknown): CustomBackgroundPreference {
  if (!value || typeof value !== 'object') {
    return defaultCustomBackgroundPreference();
  }

  const preference = value as Partial<CustomBackgroundPreference>;
  const imageDataUrl = typeof preference.imageDataUrl === 'string' ? preference.imageDataUrl : '';
  const imageName = typeof preference.imageName === 'string' ? preference.imageName : '';
  return {
    imageDataUrl,
    imageName,
    opacity: normalizeCustomBackgroundOpacity(preference.opacity),
    surfaceOpacity: normalizeCustomBackgroundSurfaceOpacity(preference.surfaceOpacity),
  };
}

export function getCustomBackgroundPreference(): CustomBackgroundPreference {
  try {
    const raw = localStorage.getItem(CUSTOM_BACKGROUND_KEY);
    if (!raw) {
      return defaultCustomBackgroundPreference();
    }
    const parsed = JSON.parse(raw) as unknown;
    return normalizeCustomBackgroundPreference(parsed);
  } catch {
    return defaultCustomBackgroundPreference();
  }
}

/**
 * Persist the custom-background preference.
 *
 * Throws if `localStorage.setItem` fails (e.g. quota exceeded). Callers are
 * responsible for surfacing the error to the user — silently swallowing it
 * previously caused the "restart reverts to an older wallpaper" bug, because
 * the in-memory state diverged from what was actually persisted.
 */
export function setCustomBackgroundPreference(preference: CustomBackgroundPreference): CustomBackgroundPreference {
  const normalized = normalizeCustomBackgroundPreference(preference);
  localStorage.setItem(CUSTOM_BACKGROUND_KEY, JSON.stringify(normalized));
  window.dispatchEvent(new CustomEvent(CUSTOM_BACKGROUND_CHANGE_EVENT, { detail: normalized }));
  return normalized;
}

export function clearCustomBackgroundPreference(): CustomBackgroundPreference {
  const cleared = defaultCustomBackgroundPreference();
  try {
    localStorage.removeItem(CUSTOM_BACKGROUND_KEY);
    window.dispatchEvent(new CustomEvent(CUSTOM_BACKGROUND_CHANGE_EVENT, { detail: cleared }));
  } catch {
    // ignore storage errors
  }
  return cleared;
}

// ---- Agent API ----

export type AgentEventType =
  | 'content'
  | 'content_delta'
  | 'content_end'
  | 'reasoning_delta'
  | 'reasoning_end'
  | 'user_message'
  | 'tool_call'
  | 'tool_result'
  | 'permission_request'
  | 'wait_start'
  | 'wait_tick'
  | 'wait_end'
  | 'llm_retry_start'
  | 'llm_retry_end'
  | 'compacted'
  | 'compacting'
  | 'context_usage'
  | 'queue'
  | 'assistant_message'
  // 子代理（run_subagents）：start/done 持久（重建界面用），中间几条是瞬态的逐步活动/重试
  | 'subagent_start'
  | 'subagent_message'
  | 'subagent_tool_call'
  | 'subagent_tool_result'
  | 'subagent_retry'
  | 'subagent_done'
  | 'finish'
  | 'error'
  | 'stopped'
  | 'status'
  | 'close';

/** 已用上下文 / 上下文窗口（token）。used_tokens 为估算值（含系统提示与对话历史）。 */
export type AgentContextUsage = {
  used_tokens: number;
  window_tokens: number;
};

/** 排队中的用户消息（模型还没看到）：显示在 composer 上方的队列面板里。 */
export type QueuedMessage = {
  id: string;
  text: string;
};

/**
 * 助手消息的有序段落（助手消息的 content 模型）。
 * 思考与正文随消息一起持久化，所以刷新/切会话/重连后仍能重建出卡片；
 * 流式增量只是实时打字机效果，不是转录的来源。
 */
export type AgentMessagePart =
  | { type: 'reasoning'; text: string }
  | { type: 'text'; text: string }
  | { type: 'tool_call'; id?: string; name?: string; arguments?: string };

/** 正在生成的助手消息（进行中）：重连时据此把"那半条消息"照原样补出来。 */
export type AgentStreamingMessage = {
  /** 快照覆盖到的事件序号，客户端据此续订 SSE（避免重复补增量） */
  step: number;
  parts: AgentMessagePart[];
};

export type AgentEvent = {
  type: AgentEventType;
  step: number;
  // content
  content?: string;
  // content_delta（流式增量）/ content_end（一段流式文本结束）/ reasoning_*（思考流同构）
  delta?: string;
  index?: number;
  length?: number;
  // user_message
  message?: string;
  // tool_call
  id?: string;
  name?: string;
  arguments?: unknown;
  // tool_result
  ok?: boolean;
  result?: unknown;
  error?: string;
  duration_ms?: number;
  // permission_request（写操作执行前请用户批准）
  tool_call_id?: string;
  label?: string;
  risk?: string;
  mode?: string;
  timeout_s?: number;
  // wait_start / wait_tick / wait_end
  seconds?: number;
  total_ms?: number;
  remaining_ms?: number;
  elapsed_ms?: number;
  interrupted?: boolean;
  // compacted（上下文压缩）
  removed?: number;
  summary_chars?: number;
  tokens_before?: number;
  // llm_retry_start / llm_retry_end（LLM 请求失败自动重试）
  attempt?: number;
  max_attempts?: number;
  delay_ms?: number;
  code?: string;
  ts?: number;
  aborted?: boolean;
  // finish
  summary?: string;
  total_steps?: number;
  /** 收尾事件专用：后端已安排好 followup 回合，马上又会跑起来（别把运行态打回停止） */
  followup?: boolean;
  // context_usage 事件 / status 快照里的上下文用量
  context?: AgentContextUsage;
  /** queue 事件 / status 快照：排队中的消息（整份，顺序即发送顺序） */
  queued?: QueuedMessage[];
  // assistant_message：助手消息的有序段落（思考/正文/工具调用）
  parts?: AgentMessagePart[];
  /** true 表示这是"进行中"的段落快照（由 status().streaming 合成，非持久化事件） */
  streaming?: boolean;
  // status
  status?: string;
  traceback?: string;
  reason?: string;
  started_at?: number;
  finished_at?: number;
  goal?: string;
  // 子代理事件（subagent_*）：id 是本次派发的 id，parent_id 指向发起它的那次工具调用
  parent_id?: string;
  agent?: string;
  file?: string;
  indexes?: string;
  brief?: string;
  model?: string;
  /** subagent_message：这一轮子代理说了什么 */
  round?: number;
  text?: string;
  /** subagent_done：子代理的收尾报告与统计 */
  report?: string;
  turns?: number;
  tool_calls?: number;
  /** subagent_done：写了几条校对意见（数字）；工具结果里的 tasks[].doubts 是 index 数组 */
  doubts?: number | number[];
};

export type AgentStatus = {
  status: string;
  project_dir: string;
  session_id?: string;
  title?: string;
  goal?: string;
  step: number;
  started_at?: number;
  finished_at?: number;
  error?: string;
  /** 已用上下文/窗口（界面指示器用；会话为空时 used_tokens 为 0） */
  context?: AgentContextUsage;
  /** 排队中的消息（队列面板的数据源；内存态，重启即空） */
  queued?: QueuedMessage[];
  /** 正在生成的助手消息（没有进行中的响应时为 null/缺省） */
  streaming?: AgentStreamingMessage | null;
  events?: AgentEvent[];
};

/** A conversation under a project. One project can hold many. */
export type AgentSession = {
  session_id: string;
  title: string;
  created_at: number;
  updated_at: number;
  /** 后端内存里的实时状态：running / awaiting_input / stopped / failed；无状态时为空串。
   *  侧边栏的状态灯据此显示（running 蓝灯亮着、跑完亮绿灯或橙灯）。 */
  status?: string;
};

export type AgentStartPayload = {
  project_dir: string;
  config_file_name?: string;
  backend_profile_data: Record<string, unknown>;
  goal?: string;
  /** Omit to let the backend create a fresh session. */
  session_id?: string;
} & AgentRequestContext;

/**
 * Agent 会话要带上的「后端上下文」：配置名＋翻译器那份的配置内容。
 *
 * 后端拿不到配置名（配置存在前端 localStorage），而「了解项目」要如实报出
 * 实际生效的两份后端（本会话在用的 + 翻译任务会用的），所以随 start/message
 * 一起送过去。地址与密钥不在返回里出现，这里送的是原名与内容。
 */
export type AgentBackendContext = {
  /** 本会话在用的后端配置名（Agent 页选中的那份）。 */
  backend_profile_name?: string;
  /** 本会话实际使用的配置内容。token 不落盘，重启后继续历史会话时必须重新随请求提供。 */
  backend_profile_data?: Record<string, unknown>;
  /** 翻译任务会用的后端配置名（项目选择 → 否则全局「翻译器默认」）。 */
  translator_profile_name?: string;
  translator_profile_data?: Record<string, unknown>;
};

/** start / message 的请求上下文：后端上下文 + 权限模式（见 lib/permissionMode）。 */
export type AgentRequestContext = AgentBackendContext & {
  /** 权限模式：后端据此决定每次工具调用是直接放行还是先请用户批准。 */
  permission_mode?: PermissionMode;
};

/** 取「翻译任务会用的后端」：优先项目自己的选择，没有则回落到全局「翻译器默认」。 */
export function getAgentTranslatorBackendContext(
  projectDir: string,
): Pick<AgentBackendContext, 'translator_profile_name' | 'translator_profile_data'> {
  const { name, profile } = resolveSelectedBackendProfile(projectDir);
  return {
    ...(name ? { translator_profile_name: name } : {}),
    ...(profile ? { translator_profile_data: profile } : {}),
  };
}

export async function startAgent(payload: AgentStartPayload) {
  return apiRequest<AgentStatus>('/api/agent/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/**
 * 回答 Agent 的 ask_user 提问（后端那个工具正阻塞着等这一下）。
 * answers 与提问一一对应：选项数组；null / 空数组 = 跳过该题。
 */
export async function answerAgentAsk(
  projectDir: string,
  answers: Array<string[] | null>,
  sessionId?: string,
) {
  return apiRequest<{ ok: boolean; answers: Array<string[] | null> }>('/api/agent/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, session_id: sessionId, answers }),
  });
}

/**
 * 回答权限确认卡（后端那个工具调用正阻塞着等这一下）。
 * decision：allow-once 只批这一次 / allow-session 本会话都批这个工具 / deny 拒绝。
 * reason：拒绝时可选的一句话（"为什么不要"），后端会把它拼进那条工具结果给模型看；
 * 批准时传了也会被忽略。
 */
export async function answerAgentPermission(
  projectDir: string,
  decision: PermissionDecision,
  sessionId?: string,
  reason?: string,
) {
  return apiRequest<{ ok: boolean; decision: string; name: string; reason?: string }>(
    '/api/agent/permission',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_dir: projectDir,
        session_id: sessionId,
        decision,
        reason: reason || undefined,
      }),
    },
  );
}

/**
 * 改权限模式。回合跑着也能改：后端下一次工具调用就按新档判；如果正好有一张权限卡在等，
 * 新档本来就会放行它的话会自动放行（相当于替你点了「允许一次」）。
 * 空闲会话也允许（下次 start/message 照样会带，两边一致）。
 */
export async function setAgentPermissionMode(
  projectDir: string,
  permissionMode: PermissionMode,
  sessionId?: string,
) {
  return apiRequest<{ ok: boolean; permission_mode: string; session_id: string }>(
    '/api/agent/permission-mode',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_dir: projectDir,
        session_id: sessionId,
        permission_mode: permissionMode,
      }),
    },
  );
}

/**
 * Send a user message to the project's agent session. While the agent is
 * running the message is queued as an interjection; otherwise it starts a
 * new turn continuing the same conversation.
 */
export async function sendAgentMessage(
  projectDir: string,
  message: string,
  sessionId?: string,
  backendContext?: AgentRequestContext,
) {
  return apiRequest<AgentStatus>('/api/agent/message', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project_dir: projectDir,
      message,
      session_id: sessionId,
      ...backendContext,
    }),
  });
}

/** Stop any running turn and drop the session history. */
export async function resetAgent(projectDir: string, sessionId?: string) {
  return apiRequest<AgentStatus>('/api/agent/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, session_id: sessionId }),
  });
}

export async function stopAgent(projectDir: string, sessionId?: string) {
  return apiRequest<AgentStatus>('/api/agent/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, session_id: sessionId }),
  });
}

/** 删掉一条排队消息（模型还没看到的那条）。 */
export async function deleteAgentQueued(projectDir: string, id: string, sessionId?: string) {
  return apiRequest<AgentStatus>('/api/agent/queue/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, id, session_id: sessionId }),
  });
}

/** 就地改一条排队消息的文本（位置不变）。 */
export async function updateAgentQueued(
  projectDir: string,
  id: string,
  message: string,
  sessionId?: string,
) {
  return apiRequest<AgentStatus>('/api/agent/queue/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, id, message, session_id: sessionId }),
  });
}

/** 「立即」：打断当前回合，把这条排队消息马上发出去。 */
export async function sendAgentQueuedNow(projectDir: string, id: string, sessionId?: string) {
  return apiRequest<AgentStatus>('/api/agent/queue/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, id, session_id: sessionId }),
  });
}

export async function fetchAgentStatus(projectDir: string, sessionId?: string) {
  const sid = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';
  return apiRequest<AgentStatus>(
    `/api/agent/status?project_dir=${encodeURIComponent(projectDir)}${sid}`,
  );
}

/** List all conversation sessions belonging to a project. */
export async function listAgentSessions(projectDir: string) {
  const res = await apiRequest<{ sessions: AgentSession[] }>(
    `/api/agent/sessions?project_dir=${encodeURIComponent(projectDir)}`,
  );
  return res.sessions || [];
}

/**
 * 回放某会话的已提交转录（后端从会话日志读，不受内存事件窗口限制）。
 * 这是界面重建转录的权威历史来源；status().events 只当补充。
 */
export async function fetchAgentTranscript(projectDir: string, sessionId?: string, limit?: number) {
  const sid = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';
  const lim = limit ? `&limit=${limit}` : '';
  const res = await apiRequest<{ events: AgentEvent[] }>(
    `/api/agent/transcript?project_dir=${encodeURIComponent(projectDir)}${sid}${lim}`,
  );
  return res.events || [];
}

/** Create an empty session (no turn started). Title defaults to 占位「新会话」，
 * 首条消息发出后由后端改成这条消息的内容（见 startAgent 的 goal）。 */
export async function createAgentSession(projectDir: string, title?: string) {
  return apiRequest<AgentSession>('/api/agent/sessions/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, title }),
  });
}

/** Delete a session and its persisted history. */
export async function deleteAgentSession(projectDir: string, sessionId: string) {
  return apiRequest<{ status: string; session_id: string }>('/api/agent/sessions/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_dir: projectDir, session_id: sessionId }),
  });
}

/**
 * Subscribe to an agent's SSE event stream. Calls `onEvent` for every agent
 * event (content / tool_call / tool_result / finish / error / stopped / status / close).
 * `afterStep`: skip replayed events with step <= afterStep (resume without duplicates).
 * Returns an abort function that closes the stream.
 */
export function subscribeAgentStream(
  projectDir: string,
  onEvent: (event: AgentEvent) => void,
  onError?: (err: Error) => void,
  afterStep?: number,
  sessionId?: string,
): () => void {
  const baseUrl = getBackendBaseUrl();
  const url =
    `${baseUrl}/api/agent/stream?project_dir=${encodeURIComponent(projectDir)}` +
    (typeof afterStep === 'number' ? `&after_step=${afterStep}` : '') +
    (sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '');
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(url, {
        method: 'GET',
        signal: controller.signal,
        headers: { Accept: 'text/event-stream' },
      });
      if (!response.ok || !response.body) {
        throw new Error(`agent stream 请求失败：${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames separated by "\n\n"
        let sep: number;
        while ((sep = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          parseAgentFrame(frame, onEvent);
        }
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  return () => controller.abort();
}

function parseAgentFrame(frame: string, onEvent: (event: AgentEvent) => void) {
  // frame format: "event: agent\ndata: {...}"
  const lines = frame.split('\n');
  let dataLine = '';
  for (const line of lines) {
    if (line.startsWith('data:')) {
      dataLine = line.slice(5).trim();
    }
  }
  if (!dataLine) return;
  try {
    const payload = JSON.parse(dataLine) as AgentEvent;
    onEvent(payload);
  } catch {
    // ignore malformed frame
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
  if (runtimeBackendBaseUrl) {
    return runtimeBackendBaseUrl;
  }
  const configured = import.meta.env.VITE_BACKEND_URL?.trim();
  return configured ? configured.replace(/\/$/, '') : DEFAULT_BACKEND_URL;
}

export function setRuntimeBackendBaseUrl(url: string | null) {
  runtimeBackendBaseUrl = url ? url.trim().replace(/\/$/, '') : null;
}

function shouldUseManagedDesktopBackend() {
  const baseUrl = getBackendBaseUrl();

  if (baseUrl === DEFAULT_BACKEND_URL) {
    return true;
  }

  try {
    const parsed = new URL(baseUrl);
    return parsed.port === '12333' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
  } catch {
    return false;
  }
}
