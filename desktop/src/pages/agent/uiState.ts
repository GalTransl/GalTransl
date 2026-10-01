/* ── Activity group (thinking + tool calls collapsed into one row) ── */

/** 用户手动开合过的折叠状态（模块级）：切页面/切会话会把组件卸载重建，
 *  useState 里的展开状态会丢。这里按 persistKey（项目::会话::组/条目）记住，
 *  重挂时恢复。只记「用户点过」的，自动跟随逻辑不受影响。 */
export const manualOpenState = new Map<string, boolean>();

/** 运行中回合的墙钟起点（模块级，key 同 stateKey）：切页面/切会话会把组件卸载重建，
 *  useRef 里的起点随之丢失；重挂时若直接拿 Date.now() 当起点，"处理中 · Ns"的计时
 *  就从 0 重走。起点按组记住，重挂后接着上次的时刻继续走。回合结束即删，表不会积大。
 *  （整个应用重启后表是空的，退回"从当下重计"；结束的回合本就走事件耗时之和的兜底。） */
export const liveStartedState = new Map<string, number>();
