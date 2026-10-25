import type { ActivityItem } from './timeline';

// 明确列举已知只读工具，未知工具、写操作和审批都维持独立呈现。
const readTools = new Set([
  'read',
  'get_plugin_settings', 'get_project_overview', 'list_input_files', 'read_input_file',
  'read_guideline', 'list_dict_files', 'read_dict', 'get_name_table', 'get_runtime',
  'list_problems', 'read_proofread_changes', 'read_transl_cache', 'read_output',
  'search_input', 'search_output_files', 'read_history_archive',
  'list_transl_cache', 'search_transl_cache',
]);

export interface ActivityEntry {
  key: string;
  indices: number[];
  reading: boolean;
  live: boolean;
}

export function activityEntries(items: ActivityItem[], isLive: boolean): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  items.forEach((item, index) => {
    const reading = item.kind === 'tool' && readTools.has(item.name || '')
      && !item.permission && item.ok !== false && item.error === undefined && !item.final;
    const previous = entries[entries.length - 1];
    if (reading && previous?.reading) previous.indices.push(index);
    else entries.push({ key: `${item.kind}-${item.id || item.step}`, indices: [index], reading, live: false });
  });
  const tail = entries[entries.length - 1];
  // 子调用之间的空档仍属于正在查阅；叙述、思考、写入和终态才结束这一阶段。
  if (tail?.reading) tail.live = isLive;
  return entries;
}
