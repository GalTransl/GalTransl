import { t as translate } from "../../i18n/core";
import type { IconName } from '../../components/Icon';
import type { ActivityItem } from './timeline';

/* ── Tool presentation ──
   Each backend tool maps to an action verb + icon + the most salient argument,
   so a row reads like "启动翻译 ForGal-json" rather than a raw function name. */

type ToolMeta = {
  action: string;
  running: string;
  /** 图标名（统一图标集）：渲染处 `<Icon name={meta.icon} />` */
  icon: IconName;
  summary: (args: Record<string, unknown> | undefined) => string;
  verb: string;
};

const TOOL_META: Record<string, ToolMeta> = {
  get_plugin_settings: {
    get action() { return translate("agent:tools.get_plugin_settings.action"); }, get running() { return translate("agent:tools.get_plugin_settings.running"); }, get verb() { return translate("agent:tools.get_plugin_settings.verb"); }, icon: 'plug',
    summary: (args) => String(args?.plugin_name || translate("agent:toolMeta.summary_string_allPlugin")),
  },
  get_project_overview: {
    get action() { return translate("agent:tools.get_project_overview.action"); },
    get running() { return translate("agent:tools.get_project_overview.running"); },
    verb: '',
    icon: 'folder-open',
    // 带了 include 就亮出来，界面上一眼看出这次只取了哪几段
    summary: (a) =>
      Array.isArray(a?.include) && a.include.length
        ? translate("agent:toolMeta.summary_message_text", { value: a.include.map((s) => str(s)).join('、') })
        : translate("agent:toolMeta.summary_message_readProject"),
  },
  update_project_config: { get action() { return translate("agent:tools.update_project_config.action"); }, get running() { return translate("agent:tools.update_project_config.running"); }, verb: '', icon: 'sliders', summary: () => translate("agent:toolMeta.summary_message_translationGuidelineSettings") },
  list_input_files: { get action() { return translate("agent:tools.list_input_files.action"); }, get running() { return translate("agent:tools.list_input_files.running"); }, verb: '', icon: 'archive', summary: () => translate("agent:toolMeta.summary_message_translationFileSentence") },
  read_input_file: { get action() { return translate("agent:tools.read_input_file.action"); }, get running() { return translate("agent:tools.read_input_file.running"); }, verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  read_guideline: {
    get action() { return translate("agent:tools.read_guideline.action"); },
    get running() { return translate("agent:tools.read_guideline.running"); },
    verb: '',
    icon: 'bookmark',
    summary: (a) => (str(a?.scope) === 'project' ? translate("agent:toolMeta.summary_message_projectGuideline") : str(a?.name)),
  },
  write_project_guideline: {
    get action() { return translate("agent:tools.write_project_guideline.action"); },
    get running() { return translate("agent:tools.write_project_guideline.running"); },
    verb: '',
    icon: 'pencil',
    summary: (a) => {
      const mode = str(a?.mode);
      if (mode === 'overwrite') return translate("agent:toolMeta.summary_message_textVariant2");
      if (mode === 'append') return translate("agent:toolMeta.summary_message_textVariant3");
      if (mode === 'replace') return translate("agent:toolMeta.summary_message_replace");
      return mode;
    },
  },
  list_dict_files: { get action() { return translate("agent:tools.list_dict_files.action"); }, get running() { return translate("agent:tools.list_dict_files.running"); }, verb: '', icon: 'books', summary: () => translate("agent:toolMeta.summary_message_projectDictionaryFile") },
  read_dict: { get action() { return translate("agent:tools.read_dict.action"); }, get running() { return translate("agent:tools.read_dict.running"); }, verb: '', icon: 'book', summary: (a) => str(a?.file_key) },
  save_dict: {
    get action() { return translate("agent:tools.save_dict.action"); },
    get running() { return translate("agent:tools.save_dict.running"); },
    verb: '',
    icon: 'save',
    // 带 category = 文件不存在时顺带新建（原 create_dict_file）
    summary: (a) =>
      [str(a?.file_key), a?.category ? translate("agent:toolMeta.summary_categoryFilter_newDictionary", { value: DICT_CATEGORY_LABELS[str(a.category)] || str(a.category) }) : '']
        .filter(Boolean)
        .join(' · '),
  },
  get_name_table: { get action() { return translate("agent:tools.get_name_table.action"); }, get running() { return translate("agent:tools.get_name_table.running"); }, verb: '', icon: 'user', summary: () => translate("agent:toolMeta.summary_message_nameReplace") },
  save_name_table: { get action() { return translate("agent:tools.save_name_table.action"); }, get running() { return translate("agent:tools.save_name_table.running"); }, verb: '', icon: 'users', summary: (a) => (Array.isArray(a?.names) ? translate("agent:toolMeta.summary_message_entry", { count: a.names.length }) : '') },
  start_translation: { get action() { return translate("agent:tools.start_translation.action"); }, get running() { return translate("agent:tools.start_translation.running"); }, verb: '', icon: 'play', summary: (a) => [str(a?.translator), ...(Array.isArray(a?.files) ? [translate("agent:toolMeta.summary_filter_countFile", { count: a.files.length })] : [])].filter(Boolean).join(' · ') },
  run_subagents: {
    // 子代理：一次调用带一批任务，界面上每个子代理一行（见 SubagentList）
    get action() { return translate("agent:tools.run_subagents.action"); },
    get running() { return translate("agent:tools.run_subagents.running"); },
    verb: '',
    icon: 'users',
    summary: (a) => {
      const tasks = Array.isArray(a?.tasks) ? a.tasks : [];
      if (!tasks.length) return '';
      // `file:"*" + count:N` 的展开发生在**后端**：入参里只有 1 个任务，实际会派 N 个。
      // 摘要要按展开后的数量说，否则"派子代理 1 个"和下面 16 行子代理对不上。
      const total = tasks.reduce((sum, task) => {
        const raw = (task as Record<string, unknown> | undefined)?.count;
        const n = typeof raw === 'number' && Number.isFinite(raw) ? Math.floor(raw) : 1;
        return sum + Math.max(1, n);
      }, 0);
      const files = tasks
        .map((task) => {
          const file = str((task as Record<string, unknown> | undefined)?.file);
          return file === '*' ? translate("agent:toolMeta.files_message_auto") : file; // "*" 是"全部均分"的写法，照抄出来没人看得懂
        })
        .filter(Boolean);
      const head = files.slice(0, 2).join('、');
      const rest = files.length > 2 ? translate("agent:toolMeta.rest_message_item", { count: files.length }) : '';
      return translate("agent:toolMeta.summary_message_count", { total: total, head: head, rest: rest });
    },
  },
  read_proofread_changes: { get action() { return translate("agent:tools.read_proofread_changes.action"); }, get running() { return translate("agent:tools.read_proofread_changes.running"); }, verb: '', icon: 'search', summary: (a) => str(a?.task_id) },
  revert_proofread_changes: { get action() { return translate("agent:tools.revert_proofread_changes.action"); }, get running() { return translate("agent:tools.revert_proofread_changes.running"); }, verb: '', icon: 'repeat', summary: (a) => [str(a?.change_id), str(a?.indexes)].filter(Boolean).join(' · ') },
  ask_user: {
    get action() { return translate("agent:tools.ask_user.action"); },
    get running() { return translate("agent:tools.ask_user.running"); },
    verb: '',
    icon: 'help',
    summary: (a) => {
      const questions = Array.isArray(a?.questions) ? a.questions : [];
      const first = questions[0] && typeof questions[0] === 'object'
        ? str((questions[0] as Record<string, unknown>).question)
        : '';
      return [first, questions.length > 1 ? translate("agent:toolMeta.summary_filter_text", { count: questions.length }) : ''].filter(Boolean).join(' · ');
    },
  },
  stop_translation: { get action() { return translate("agent:tools.stop_translation.action"); }, get running() { return translate("agent:tools.stop_translation.running"); }, verb: '', icon: 'stop', summary: () => '' },
  wait: { get action() { return translate("agent:tools.wait.action"); }, get running() { return translate("agent:tools.wait.running"); }, verb: '', icon: 'hourglass', summary: (a) => waitSummary(a) },
  get_runtime: { get action() { return translate("agent:tools.get_runtime.action"); }, get running() { return translate("agent:tools.get_runtime.running"); }, verb: '', icon: 'settings', summary: () => '' },
  list_problems: { get action() { return translate("agent:tools.list_problems.action"); }, get running() { return translate("agent:tools.list_problems.running"); }, verb: '', icon: 'search', summary: (a) => str(a?.problem_type) || translate("agent:toolMeta.summary_message_problemStats") },
  manage_problem_filter: { get action() { return translate("agent:tools.manage_problem_filter.action"); }, get running() { return translate("agent:tools.manage_problem_filter.running"); }, verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.keyword) ? a.keyword.map((k) => str(k)).join('、') : str(a?.keyword)].filter(Boolean).join(' · ') },
  manage_problem_white_list: { get action() { return translate("agent:tools.manage_problem_white_list.action"); }, get running() { return translate("agent:tools.manage_problem_white_list.running"); }, verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.entry) ? a.entry.map((k) => str(k)).join('、') : str(a?.entry)].filter(Boolean).join(' · ') },
  read_transl_cache: { get action() { return translate("agent:tools.read_transl_cache.action"); }, get running() { return translate("agent:tools.read_transl_cache.running"); }, verb: '', icon: 'file-text', summary: translCacheSummary },
  read_output: { get action() { return translate("agent:tools.read_output.action"); }, get running() { return translate("agent:tools.read_output.running"); }, verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  search_input: { get action() { return translate("agent:tools.search_input.action"); }, get running() { return translate("agent:tools.search_input.running"); }, verb: '', icon: 'search-plus', summary: (a) => [str(a?.query), str(a?.filename), a?.context ? translate("agent:toolMeta.summary_filter_sentenceContext", { displayContext: a.context }) : ''].filter(Boolean).join(' · ') },
  search_output_files: { get action() { return translate("agent:tools.search_output_files.action"); }, get running() { return translate("agent:tools.search_output_files.running"); }, verb: '', icon: 'search-plus', summary: (a) => [str(a?.query), str(a?.filename), a?.context ? translate("agent:toolMeta.summary_filter_sentenceContext", { displayContext: a.context }) : ''].filter(Boolean).join(' · ') },
  patch_transl_cache: {
    get action() { return translate("agent:tools.patch_transl_cache.action"); },
    get running() { return translate("agent:tools.patch_transl_cache.running"); },
    verb: '',
    icon: 'pencil',
    // 一次调用可以跨多个文件（patches 每条带 file）：跨了就报文件数，
    // 只改一个文件时只报条数（文件名在参数里，不必重复）。
    // clear_comment 是"顺带清批注"，列出来：一次改动里它是容易被忽略的那半个动作。
    summary: (a) => {
      if (a?.action === 'replace') {
        const files = new Set(Array.isArray(a.files) ? a.files.map(str).filter(Boolean) : [str(a.filename)].filter(Boolean));
        return [translate("agent:toolMeta.head_message_countFile", { count: files.size }), `${str(a.query)} -> ${str(a.replacement)}`, a.clear_comment ? translate("agent:toolMeta.summary_clearCommentFilter_clearComment") : ''].filter(Boolean).join(' · ');
      }
      const patches = Array.isArray(a?.patches) ? (a.patches as Record<string, unknown>[]) : [];
      const files = new Set(patches.map((p) => str(p?.file) || str(a?.filename)).filter(Boolean));
      const head = files.size > 1 ? translate("agent:toolMeta.head_message_countFile", { count: files.size }) : '';
      return [head, patches.length ? translate("agent:toolMeta.summary_clearCommentFilter_entry", { count: patches.length }) : '', a?.clear_comment ? translate("agent:toolMeta.summary_clearCommentFilter_clearComment") : '']
        .filter(Boolean)
        .join(' · ') || str(a?.filename);
    },
  },
  delete_transl_cache: { get action() { return translate("agent:tools.delete_transl_cache.action"); }, get running() { return translate("agent:tools.delete_transl_cache.running"); }, verb: '', icon: 'trash', summary: (a) => [str(a?.filename), str(a?.indexes)].filter(Boolean).join(' · ') },
  read_history_archive: { get action() { return translate("agent:tools.read_history_archive.action"); }, get running() { return translate("agent:tools.read_history_archive.running"); }, verb: '', icon: 'archive', summary: (a) => [str(a?.chunk), str(a?.query)].filter(Boolean).join(' · ') || translate("agent:toolMeta.summary_message_archive") },
};

// 已并入别的工具的旧名字：旧会话的转录里还有这些调用，按合并后的工具显示（参数换成新工具的口径）。
// 不放进 TOOL_META：那张表要和后端现有工具一一对应（见 tests/test_agent_tool_meta_labels.py）。
const RETIRED_TOOL_ALIASES: Record<string, { name: string; args: (a: Record<string, unknown> | undefined) => Record<string, unknown> }> = {
  list_transl_cache: { name: 'read_transl_cache', args: (a) => ({ ...a, action: 'list' }) },
  search_transl_cache: { name: 'read_transl_cache', args: (a) => ({ ...a, action: 'search' }) },
  create_dict_file: { name: 'save_dict', args: (a) => ({ file_key: a?.filename, category: a?.category }) },
};

const DEFAULT_TOOL_META: ToolMeta = { get action() { return translate("agent:toolMeta.action_action_textVariant2"); }, get running() { return translate("agent:toolMeta.running_running_text"); }, verb: '', icon: 'tool', summary: () => '' };

/** 未收录进 TOOL_META 的工具：至少把原始工具名亮出来，不再只显示「调用工具」。 */
export function toolMeta(name: string | undefined): ToolMeta {
  if (!name) return DEFAULT_TOOL_META;
  const meta = TOOL_META[name];
  if (meta) return meta;
  const alias = RETIRED_TOOL_ALIASES[name];
  const base = alias && TOOL_META[alias.name];
  if (alias && base) return { ...base, summary: (a) => base.summary(alias.args(a)) };
  return { ...DEFAULT_TOOL_META, action: name, running: name };
}

const DICT_CATEGORY_LABELS: Record<string, string> = { get pre() { return translate("agent:toolMeta.pre_pre_text"); }, gpt: 'GPT', get post() { return translate("agent:toolMeta.post_post_text"); } };

/** read_transl_cache 的 action（与后端 _cache_read_action 同一推断：有 query 是 search，有 filename 是 read）。 */
function translCacheAction(a: Record<string, unknown> | undefined): string {
  const action = str(a?.action);
  if (action) return action;
  if (str(a?.query)) return 'search';
  if (str(a?.filename)) return 'read';
  return 'list';
}

function translCacheSummary(a: Record<string, unknown> | undefined): string {
  const action = translCacheAction(a);
  if (action === 'list') return [translate("agent:toolMeta.translCacheSummary_filter_cacheList"), str(a?.grep)].filter(Boolean).join(' · ');
  if (action === 'search') {
    return [
      translate("agent:toolMeta.translCacheSummary_strAQueryStrAFilenameAContextAContextFilter_search", { value: str(a?.query) }),
      str(a?.filename),
      a?.context ? translate("agent:toolMeta.translCacheSummary_strAQueryStrAFilenameAContextAContextFilter_sentenceContext", { displayContext: a.context }) : '',
    ].filter(Boolean).join(' · ');
  }
  return [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ');
}

export function str(v: unknown): string {
  if (v === undefined || v === null) return '';
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/** wait 工具的参数摘要：把 seconds/minutes 归一成"等待 2 分钟"。
 *  带了 job_id（等某个任务，任务先结束就提前返回）时把这一点说清楚。 */
function waitSummary(args: Record<string, unknown> | undefined): string {
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const totalSeconds = num(args?.seconds) + num(args?.minutes) * 60;
  const reason = typeof args?.reason === 'string' ? args.reason.trim() : '';
  const jobId = typeof args?.job_id === 'string' ? args.job_id.trim() : '';
  if (totalSeconds <= 0) return reason;
  const duration = totalSeconds % 60 === 0 && totalSeconds >= 60
    ? translate("agent:toolMeta.duration_message_minutes", { value: totalSeconds / 60 })
    : translate("agent:toolMeta.duration_message_seconds", { totalSeconds: totalSeconds });
  const head = jobId
    ? translate("agent:toolMeta.head_message_job", { value: jobId.length > 8 ? `${jobId.slice(0, 6)}…` : jobId, duration: duration })
    : duration;
  return reason ? `${head} · ${reason}` : head;
}

/** 倒计时显示：mm:ss，超过一小时用 h:mm:ss。 */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** token 数显示：1000 起用 K（1 位小数），如 128.4K / 1000.0K。 */
export function formatTokenCount(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n < 1000) return String(Math.round(n));
  return `${(n / 1000).toFixed(1)}K`;
}

export function formatDuration(ms: number | undefined): string {
  if (!ms || ms < 1000) return `${ms || 0}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

export function asArgs(args: unknown): Record<string, unknown> | undefined {
  if (args && typeof args === 'object' && !Array.isArray(args)) return args as Record<string, unknown>;
  return undefined;
}

/** 系统通知里显示的提问摘要：第一题的内容（多题时带上题数）；拿不到就兜一句。 */
export function askNotifyBody(item: { arguments?: unknown } | null): string {
  const questions = asArgs(item?.arguments)?.questions;
  const list = Array.isArray(questions) ? questions : [];
  const first = list[0] && typeof list[0] === 'object' && !Array.isArray(list[0])
    ? str((list[0] as Record<string, unknown>).question).trim()
    : '';
  const suffix = list.length > 1 ? translate("agent:toolMeta.suffix_message_text", { count: list.length }) : '';
  return (first || translate("agent:toolMeta.askNotifyBody_message_agentCountProblemSelect")) + suffix;
}

/** 这张工具行是不是"启动翻译"且拿到了 job_id（拿不到就退回原来的工具行）。 */
export function translationJobId(item: ActivityItem): string {
  const r = item.result;
  if (!r || typeof r !== 'object') return '';
  return str((r as Record<string, unknown>).job_id);
}

/** 截断到 max 个字符，超出补省略号。 */
export function clipText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function formatPayload(payload: unknown): string {
  if (payload === undefined || payload === null) return '';
  if (typeof payload === 'string') return payload;
  try {
    return JSON.stringify(payload, null, 2);
  } catch {
    return String(payload);
  }
}
