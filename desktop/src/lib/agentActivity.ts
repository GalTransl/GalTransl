import { useCallback, useSyncExternalStore } from 'react';

/* 「提取 Agent / 翻译 Agent」的会话状态灯：主侧栏的两个入口和两页各自的会话列表共用这一份。
   - 蓝：有会话正在运行；
   - 绿 / 橙：会话这一轮跑完了（橙 = 出错结束），但还没点开看过；点开该会话即熄灭。

   主侧栏常驻，在后台轮询两边的会话列表；Agent 页在前台时也把自己拉到的列表报上来，
   让灯和页面里的状态同一拍变化。「刚跑完」只在这里按会话判定一次——谁先报到算谁的；
   每次上报带着请求发出的时刻，比已记录的更旧的结果直接丢掉，免得晚到的旧列表把已经
   跑完的会话又标回运行中、再触发一次"刚跑完"。 */

export type AgentKind = 'translation' | 'extraction';
export type AgentSessionPhase = 'running' | 'done' | 'failed';
export type AgentFinishedLight = Exclude<AgentSessionPhase, 'running'>;
/** 主侧栏入口上的点：有会话在跑 > 有没看过的出错 > 有没看过的完成 */
export type AgentIndicator = AgentSessionPhase | null;
export type AgentSessionReport = { id: string; phase: AgentSessionPhase };

type KindState = {
  /** 各会话最近一次观测到的运行态，at = 那次请求发出的时刻 */
  observed: Map<string, { running: boolean; at: number }>;
  /** 跑完但还没点开看过的会话（不可变：变了就换新对象，供 useSyncExternalStore 比较） */
  unseen: Record<string, AgentFinishedLight>;
  /** 正显示在前台的会话；页面不在前台时为空 */
  viewing: string;
  indicator: AgentIndicator;
};

/** 翻译 Agent 会话列表里的 status（running / awaiting_input / stopped / failed / 空串）。 */
export const translationSessionPhase = (status?: string): AgentSessionPhase =>
  status === 'running' ? 'running' : status === 'failed' ? 'failed' : 'done';

const createState = (): KindState => ({ observed: new Map(), unseen: {}, viewing: '', indicator: null });
const states: Record<AgentKind, KindState> = { translation: createState(), extraction: createState() };
const listeners = new Set<() => void>();

function without(unseen: Record<string, AgentFinishedLight>, ids: Iterable<string>) {
  let next = unseen;
  for (const id of ids) {
    if (!(id in next)) continue;
    if (next === unseen) next = { ...unseen };
    delete next[id];
  }
  return next;
}

function commit(kind: AgentKind, unseen: Record<string, AgentFinishedLight>) {
  const state = states[kind];
  let running = false;
  for (const entry of state.observed.values()) {
    if (entry.running) { running = true; break; }
  }
  const lights = Object.values(unseen);
  const indicator: AgentIndicator = running ? 'running' : lights.includes('failed') ? 'failed' : lights.length ? 'done' : null;
  if (unseen === state.unseen && indicator === state.indicator) return;
  state.unseen = unseen;
  state.indicator = indicator;
  listeners.forEach((listener) => listener());
}

/** 报一批会话的当前状态。complete = 这是该类 Agent 当前可见会话的完整列表：
 *  不在里面的会话（删掉了、所在项目/文件夹关掉了）连同它的灯一起清掉。 */
export function reportAgentSessions(
  kind: AgentKind,
  sessions: Iterable<AgentSessionReport>,
  observedAt: number,
  { complete = false }: { complete?: boolean } = {},
) {
  const state = states[kind];
  let unseen = state.unseen;
  const listed = new Set<string>();
  for (const { id, phase } of sessions) {
    listed.add(id);
    const previous = state.observed.get(id);
    if (previous && previous.at > observedAt) continue;
    const running = phase === 'running';
    state.observed.set(id, { running, at: observedAt });
    if (running) {
      // 新一轮开始：上一轮留下的绿/橙灯作废，改亮蓝灯
      unseen = without(unseen, [id]);
    } else if (previous?.running && id !== state.viewing && unseen[id] !== phase) {
      unseen = { ...unseen, [id]: phase };
    }
  }
  if (complete) {
    for (const [id, entry] of state.observed) {
      if (!listed.has(id) && entry.at <= observedAt) state.observed.delete(id);
    }
    unseen = without(unseen, Object.keys(unseen).filter((id) => !state.observed.has(id)));
  }
  commit(kind, unseen);
}

/** 正看着的会话在页面上停下来了（实时流推来的终态）：立刻记为已停、不亮灯。
 *  不等下一次列表轮询——否则看完立刻切走，晚到一步的轮询会把它当成"没看就跑完了"。 */
export function settleViewedAgentSession(kind: AgentKind, id: string) {
  const state = states[kind];
  if (!id || state.viewing !== id || !state.observed.get(id)?.running) return;
  state.observed.set(id, { running: false, at: Date.now() });
  commit(kind, without(state.unseen, [id]));
}

/** 页面在前台时报告正在看的会话（不在前台传空串）；看到即熄灭它的灯。 */
export function setViewingAgentSession(kind: AgentKind, id: string) {
  const state = states[kind];
  state.viewing = id;
  if (id) commit(kind, without(state.unseen, [id]));
}

/** 会话被删除或被收起时立刻撤掉它的记录，不等下一次完整列表。 */
export function forgetAgentSessions(kind: AgentKind, ids: Iterable<string>) {
  const state = states[kind];
  const list = [...ids];
  for (const id of list) state.observed.delete(id);
  commit(kind, without(state.unseen, list));
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useAgentIndicator(kind: AgentKind): AgentIndicator {
  const getSnapshot = useCallback(() => states[kind].indicator, [kind]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export function useUnseenAgentLights(kind: AgentKind): Record<string, AgentFinishedLight> {
  const getSnapshot = useCallback(() => states[kind].unseen, [kind]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
