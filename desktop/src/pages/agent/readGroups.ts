import type { ActivityItem } from './timeline';
import { asArgs, toolOperation } from './toolMeta';

// 明确列举已知只读工具，未知工具、写操作和审批都维持独立呈现。
const readTools = new Set([
  'list_cache_history', 'diff_cache_history',
  'read', 'get_project',
  'get_plugin_settings', 'get_project_overview', 'list_input_files', 'read_input_file',
  'read_guideline', 'list_dict_files', 'read_dict', 'get_name_table', 'get_runtime',
  'list_problems', 'read_proofread_changes', 'read_transl_cache', 'read_output',
  'search_input', 'search_output_files', 'search_cache', 'read_history_archive',
  'list_transl_cache', 'search_transl_cache',
]);
// 提取 Agent 的命令执行：连续多条收成一组，和查阅分开计数。
const commandTools = new Set(['bash', 'run_python']);

export interface ActivityEntry {
  key: string;
  indices: number[];
  /** 可与相邻同类调用合并的类别；写入、审批、失败和未知工具没有类别。 */
  group?: 'read' | 'command';
  reading: boolean;
  live: boolean;
}

export function activityEntries(items: ActivityItem[], isLive: boolean): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  items.forEach((item, index) => {
    const args = asArgs(item.arguments);
    const operation = toolOperation(item.name, args);
    const quiet = item.kind === 'tool' && !item.permission && item.ok !== false && item.error === undefined && !item.final;
    const reading = quiet && (readTools.has(operation)
      || (operation === 'replace_cache' && args?.dry_run === true)
      || (operation === 'revert_cache_history' && args?.dry_run === true)
      || (['manage_problem_filter', 'manage_problem_white_list'].includes(operation) && args?.action === 'list'));
    const group = reading ? 'read' : quiet && commandTools.has(operation) ? 'command' : undefined;
    const previous = entries[entries.length - 1];
    if (group && previous?.group === group) previous.indices.push(index);
    else entries.push({ key: `${item.kind}-${item.id || item.step}`, indices: [index], group, reading, live: false });
  });
  const tail = entries[entries.length - 1];
  // 子调用之间的空档仍属于正在查阅/执行；叙述、思考、写入和终态才结束这一阶段。
  if (tail?.group) tail.live = isLive;
  return entries;
}
