/** 侧边栏每个项目分组默认只列最近这么多条会话，其余点「显示更多」展开。 */
export const SESSION_PREVIEW_LIMIT = 5;

/** 决定一个分组里实际列出哪些会话。
 *  未展开时只留前 limit 条，但 keep 命中的会话（正在看的、正在跑的）无论多旧都要留：
 *  否则侧边栏上看不出"你在哪"，运行中的蓝灯也没地方挂。保留原列表顺序。 */
export function previewSessions<T>(sessions: readonly T[], expanded: boolean, keep: (session: T) => boolean,
  limit = SESSION_PREVIEW_LIMIT): { visible: T[]; hidden: number; collapsible: boolean } {
  const visible = expanded ? [...sessions] : sessions.filter((session, index) => index < limit || keep(session));
  return { visible, hidden: sessions.length - visible.length, collapsible: expanded && sessions.length > limit };
}
