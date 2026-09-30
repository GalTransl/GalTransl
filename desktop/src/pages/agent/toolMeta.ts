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
    action: '查看插件设置', running: '正在查看插件设置', verb: '查看', icon: 'plug',
    summary: (args) => String(args?.plugin_name || '全部插件'),
  },
  get_project_overview: {
    action: '了解项目',
    running: '了解项目',
    verb: '',
    icon: 'folder-open',
    // 带了 include 就亮出来，界面上一眼看出这次只取了哪几段
    summary: (a) =>
      Array.isArray(a?.include) && a.include.length
        ? `按需：${a.include.map((s) => str(s)).join('、')}`
        : '读取项目概况',
  },
  update_project_config: { action: '修改项目配置', running: '修改项目配置', verb: '', icon: 'sliders', summary: () => '调整翻译参数/规范等设置' },
  list_input_files: { action: '查看原文文件清单', running: '查看原文文件清单', verb: '', icon: 'archive', summary: () => '列出待翻译文件与句数' },
  read_input_file: { action: '读取原文', running: '读取原文', verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  read_guideline: {
    action: '读取翻译规范',
    running: '读取翻译规范',
    verb: '',
    icon: 'bookmark',
    summary: (a) => (str(a?.scope) === 'project' ? '项目规范' : str(a?.name)),
  },
  write_project_guideline: {
    action: '修改项目规范',
    running: '修改项目规范',
    verb: '',
    icon: 'pencil',
    summary: (a) => {
      const mode = str(a?.mode);
      if (mode === 'overwrite') return '整份覆写';
      if (mode === 'append') return '增写';
      if (mode === 'replace') return '替换一段';
      return mode;
    },
  },
  list_dict_files: { action: '查看字典清单', running: '查看字典清单', verb: '', icon: 'books', summary: () => '列出项目字典文件' },
  read_dict: { action: '读取字典', running: '读取字典', verb: '', icon: 'book', summary: (a) => str(a?.file_key) },
  save_dict: {
    action: '保存字典',
    running: '保存字典',
    verb: '',
    icon: 'save',
    // 带 category = 文件不存在时顺带新建（原 create_dict_file）
    summary: (a) =>
      [str(a?.file_key), a?.category ? `新建${DICT_CATEGORY_LABELS[str(a.category)] || str(a.category)}字典` : '']
        .filter(Boolean)
        .join(' · '),
  },
  get_name_table: { action: '读取人名表', running: '读取人名表', verb: '', icon: 'user', summary: () => 'name替换表' },
  save_name_table: { action: '保存人名表', running: '保存人名表', verb: '', icon: 'users', summary: (a) => (Array.isArray(a?.names) ? `${a.names.length} 条` : '') },
  start_translation: { action: '启动翻译', running: '启动翻译', verb: '', icon: 'play', summary: (a) => [str(a?.translator), ...(Array.isArray(a?.files) ? [`仅 ${a.files.length} 个文件`] : [])].filter(Boolean).join(' · ') },
  run_subagents: {
    // 子代理：一次调用带一批任务，界面上每个子代理一行（见 SubagentList）
    action: '派子代理',
    running: '子代理并行中',
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
          return file === '*' ? '自动均分' : file; // "*" 是"全部均分"的写法，照抄出来没人看得懂
        })
        .filter(Boolean);
      const head = files.slice(0, 2).join('、');
      const rest = files.length > 2 ? ` 等 ${files.length} 项` : '';
      return `${total} 个 · ${head}${rest}`;
    },
  },
  ask_user: {
    action: '询问用户',
    running: '等你回答',
    verb: '',
    icon: 'help',
    summary: (a) => {
      const questions = Array.isArray(a?.questions) ? a.questions : [];
      const first = questions[0] && typeof questions[0] === 'object'
        ? str((questions[0] as Record<string, unknown>).question)
        : '';
      return [first, questions.length > 1 ? `共 ${questions.length} 题` : ''].filter(Boolean).join(' · ');
    },
  },
  stop_translation: { action: '停止翻译', running: '停止翻译', verb: '', icon: 'stop', summary: () => '' },
  wait: { action: '等待', running: '等待中', verb: '', icon: 'hourglass', summary: (a) => waitSummary(a) },
  get_runtime: { action: '查询运行时', running: '查询运行时', verb: '', icon: 'settings', summary: () => '' },
  list_problems: { action: '检查问题清单', running: '检查问题清单', verb: '', icon: 'search', summary: (a) => str(a?.problem_type) || '问题类型统计' },
  manage_problem_filter: { action: '管理问题过滤', running: '管理问题过滤', verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.keyword) ? a.keyword.map((k) => str(k)).join('、') : str(a?.keyword)].filter(Boolean).join(' · ') },
  manage_problem_white_list: { action: '管理问题白名单', running: '管理问题白名单', verb: '', icon: 'filter', summary: (a) => [str(a?.action), Array.isArray(a?.entry) ? a.entry.map((k) => str(k)).join('、') : str(a?.entry)].filter(Boolean).join(' · ') },
  read_transl_cache: { action: '查阅缓存', running: '查阅缓存', verb: '', icon: 'file-text', summary: translCacheSummary },
  read_output: { action: '读取输出', running: '读取输出', verb: '', icon: 'file-text', summary: (a) => [str(a?.filename), str(a?.index)].filter(Boolean).join(' · ') },
  search_input: { action: '搜索原文', running: '搜索原文', verb: '', icon: 'search-plus', summary: (a) => [str(a?.query), str(a?.filename), a?.context ? `±${a.context} 句上下文` : ''].filter(Boolean).join(' · ') },
  patch_transl_cache: {
    action: '修改译文',
    running: '修改译文',
    verb: '',
    icon: 'pencil',
    // 一次调用可以跨多个文件（patches 每条带 file）：跨了就报文件数，
    // 只改一个文件时只报条数（文件名在参数里，不必重复）。
    // clear_comment 是"顺带清批注"，列出来：一次改动里它是容易被忽略的那半个动作。
    summary: (a) => {
      const patches = Array.isArray(a?.patches) ? (a.patches as Record<string, unknown>[]) : [];
      const files = new Set(patches.map((p) => str(p?.file) || str(a?.filename)).filter(Boolean));
      const head = files.size > 1 ? `${files.size} 个文件` : '';
      return [head, patches.length ? `${patches.length} 条` : '', a?.clear_comment ? '清空批注' : '']
        .filter(Boolean)
        .join(' · ') || str(a?.filename);
    },
  },
  delete_transl_cache: { action: '删除缓存', running: '删除缓存', verb: '', icon: 'trash', summary: (a) => [str(a?.filename), str(a?.indexes)].filter(Boolean).join(' · ') },
  read_history_archive: { action: '回查归档', running: '回查归档', verb: '', icon: 'archive', summary: (a) => [str(a?.chunk), str(a?.query)].filter(Boolean).join(' · ') || '列出归档' },
};

// 已并入别的工具的旧名字：旧会话的转录里还有这些调用，按合并后的工具显示（参数换成新工具的口径）。
// 不放进 TOOL_META：那张表要和后端现有工具一一对应（见 tests/test_agent_tool_meta_labels.py）。
const RETIRED_TOOL_ALIASES: Record<string, { name: string; args: (a: Record<string, unknown> | undefined) => Record<string, unknown> }> = {
  list_transl_cache: { name: 'read_transl_cache', args: (a) => ({ ...a, action: 'list' }) },
  search_transl_cache: { name: 'read_transl_cache', args: (a) => ({ ...a, action: 'search' }) },
  create_dict_file: { name: 'save_dict', args: (a) => ({ file_key: a?.filename, category: a?.category }) },
};

const DEFAULT_TOOL_META: ToolMeta = { action: '调用工具', running: '调用工具', verb: '', icon: 'tool', summary: () => '' };

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

const DICT_CATEGORY_LABELS: Record<string, string> = { pre: '译前', gpt: 'GPT', post: '译后' };

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
  if (action === 'list') return ['缓存清单', str(a?.grep)].filter(Boolean).join(' · ');
  if (action === 'search') {
    return [
      `搜索「${str(a?.query)}」`,
      str(a?.filename),
      a?.context ? `±${a.context} 句上下文` : '',
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
    ? `${totalSeconds / 60} 分钟`
    : `${totalSeconds} 秒`;
  const head = jobId
    ? `等任务 ${jobId.length > 8 ? `${jobId.slice(0, 6)}…` : jobId} 结束或 ${duration}`
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
  const suffix = list.length > 1 ? `（共 ${list.length} 题）` : '';
  return (first || 'Agent 提了一个问题，需要你选择') + suffix;
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
