import { t as translate } from "../../i18n/core";
import type { AgentEvent } from '../../lib/api';
import { asArgs, toolMeta } from './toolMeta';

/* ── Timeline model ──
   Raw SSE events are folded into render groups: runs of thinking/tool activity
   collapse into one summary row ("工作 6 秒 · 4 步"), while terminal moments
   (finish / error / stopped) and the initial user message stay as their own rows.
   This mirrors how modern agent clients avoid a wall of one-line cards. */

export type ActivityItem = {
  kind: 'content' | 'reasoning' | 'tool' | 'compact' | 'retry';
  step: number;
  content?: string;
  id?: string;
  name?: string;
  arguments?: unknown;
  ok?: boolean;
  result?: unknown;
  error?: string;
  durationMs?: number;
  // 权限审批：后端在这个工具调用执行前挂起等用户点（见 PermissionCard）。
  // 没有超时字段——后端不设超时，卡片会一直等着（老会话事件里可能还带着 timeout_s /
  // started_at，这里不读、也不用）。
  permission?: {
    id: string;
    name: string;
    label: string;
    risk: string;
    mode: string;
    arguments?: Record<string, unknown>;
    // 「将要变更」预览：后端在挂起前算好的 before→after（只读算出来的，不是执行结果）。
    // 结构与工具结果里那份一致，交给 extractChangeList 认（见 PermissionCard）。
    preview?: unknown;
  };
  // 流式 content/reasoning：正在收 delta、还没收到对应的 *_end
  streaming?: boolean;
  // 回合的收尾回复：渲染时提升为顶层普通消息（不折进活动组）
  final?: boolean;
  // wait tool: 倒计时快照
  waitTotalMs?: number;
  waitRemainingMs?: number;
  waitInterrupted?: boolean;
  // compact: 上下文压缩提示（前后都是估算 token；tokensAfter 含保留下来的尾部）
  removed?: number;
  summaryChars?: number;
  tokensBefore?: number;
  tokensAfter?: number;
  // retry: LLM 请求失败自动重试（倒计时 + 第 N/M 次）
  attempt?: number;
  maxAttempts?: number;
  retryDelayMs?: number;
  retryStartedAtMs?: number;
  retryCode?: string;
  retryReason?: string;
  retryDone?: boolean;
  // 子代理：发起它的那次 run_subagents 调用下面挂的子代理行（见 SubagentList）
  subagents?: SubagentRun[];
};

/** 子代理自己的一步（工具调用或一句说明），只在对它展开时显示。 */
export type SubagentStep =
  | { kind: 'text'; round: number; text: string }
  | {
      kind: 'tool';
      id: string;
      name: string;
      args?: unknown;
      ok?: boolean;
      result?: unknown;
      error?: string;
      durationMs?: number;
      at: number;
    };

/** 一个子代理的运行状态（由 subagent_* 事件累积出来）。 */
export type SubagentRun = {
  id: string;
  agent: string;
  /** 角色中文名（校对） */
  label: string;
  file: string;
  indexes: string;
  brief: string;
  parentId?: string;
  status: 'running' | 'done' | 'failed' | 'stopped' | 'max_rounds';
  steps: SubagentStep[];
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  /** 正在退避重试（subagent_retry，瞬态）：拿到下一次成功的响应就清掉 */
  retry?: { attempt: number; maxAttempts: number; code?: string; reason?: string };
  turns?: number;
  toolCalls?: number;
  /** 写了几条校对批注（proofread_comment） */
  proofreadComment?: number;
  report?: string;
  error?: string;
};

export type TimelineGroup =
  | { type: 'activity'; id: string; items: ActivityItem[]; finalContent?: ActivityItem }
  | { type: 'user'; id: string; step: number; message: string }
  | { type: 'error'; id: string; step: number; message: string; traceback?: string }
  | { type: 'stopped'; id: string; step: number; reason: string };

/** run_subagents 的结果一到，就按结果里的 tasks 把子代理状态对齐一遍。

    结果是最终名单：父回合被停止时，没跑完的子代理等不到自己的 subagent_done 事件
    （后端把它们的终态直接写进了结果的 tasks，status=stopped）。不对齐的话这些行会
    永远停在"进行中"，头部就一直挂着一个假的「N/16 个在跑」。 */
function reconcileSubagentStatuses(target: ActivityItem) {
  if (target.kind !== 'tool' || target.name !== 'run_subagents') return;
  const runs = target.subagents;
  if (!runs?.length || typeof target.result !== 'object' || target.result === null) return;
  const tasks = (target.result as Record<string, unknown>).tasks;
  if (!Array.isArray(tasks)) return;
  for (const run of runs) {
    if (run.status !== 'running') continue; // 已有终态的（done 事件先到）听事件的
    const task = tasks.find(
      (task): task is Record<string, unknown> =>
        typeof task === 'object' && task !== null && (task as Record<string, unknown>).id === run.id,
    );
    if (!task) continue;
    const status = String(task.status || '');
    if (!status || status === 'running') continue;
    run.status = status as SubagentRun['status'];
    if (typeof task.error === 'string' && task.error) run.error = task.error;
    if (typeof task.report === 'string' && task.report) run.report = task.report;
    if (typeof task.duration_ms === 'number') run.durationMs = task.duration_ms;
  }
}

export function buildTimeline(events: AgentEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = [];
  let current: Extract<TimelineGroup, { type: 'activity' }> | null = null;
  // 助手段落（parts）的认领游标：parts 与 delta 拼出来的卡片按顺序一一对应
  // （两侧口径一致，见后端 _assistant_parts）。实时流里卡片已由 delta 建好，
  // 这里按顺序认领并写入权威文本；重建时没有 delta，就按 parts 顺序新建。
  // 游标只在同一个活动组内前进，组结束即归零。
  let partsCursor = 0;

  // final=true 表示组被真正终结（用户消息/finish/error/stopped）：此时兜底
  // 撤掉残留光标。buildTimeline 每次全量重算，结尾的尾组冲刷不能算终结——
  // 尾组正是正在流式的活动组，在那里清标志会让活卡片永远显示「已思考」。
  const closeActivity = (final = false) => {
    if (current && final) {
      // 兜底：end 事件丢失时（异常中断/旧会话回放），组终结强制撤掉残留光标
      for (const it of current.items) {
        if ((it.kind === 'content' || it.kind === 'reasoning') && it.streaming) it.streaming = false;
      }
    }
    // finalContent 也算有效内容：纯文字回复的回合里 items 会被清空
    // （finish 把同文的流式 content 移出折叠区），只剩 finalContent 也要入组。
    if (current && (current.items.length || current.finalContent)) groups.push(current);
    current = null;
    partsCursor = 0;
  };

  // 收掉指定流（content/reasoning）里所有还在流式的段：撤光标、记耗时。
  // 交替思考模型一路流里 想/说 来回切换，end 到达时它对应的段未必还是
  // items 的最后一条（中间可能隔着别的段），所以按 kind 扫，不能只看末尾。
  const endStreamKind = (kind: 'content' | 'reasoning', durationMs?: number) => {
    if (!current) return;
    let last: ActivityItem | null = null;
    for (const it of current.items) {
      if (it.kind === kind && it.streaming) {
        it.streaming = false;
        last = it;
      }
    }
    if (last && typeof durationMs === 'number') {
      // 同一张卡片在一次请求里可能收尾多次（想→说→想），耗时累加成总时长
      last.durationMs = (last.durationMs || 0) + durationMs;
    }
  };

  // 流式增量落点：从末尾往前找同 kind 的块拼回去。交替思考的 后段 要拼回
  // 前面的块（同一段思考/同一个回复），而不是新起一块掉到回复下面；扫描
  // 跨过说/想块，遇到工具/压缩行即止——那意味着上一轮请求已结束，不能跨轮拼。
  const findAppendTarget = (kind: 'content' | 'reasoning'): ActivityItem | null => {
    if (!current) return null;
    for (let i = current.items.length - 1; i >= 0; i -= 1) {
      const it = current.items[i];
      if (it.kind === 'tool' || it.kind === 'compact' || it.kind === 'retry') return null;
      if (it.kind === kind && it.streaming !== undefined) return it;
    }
    return null;
  };

  // 工具行 upsert：tool_call 事件与助手消息里的 tool_call 段落都走这里，按 id
  // 复用同一行——两处都会发，界面不能出现两行。
  const upsertToolCall = (
    items: ActivityItem[],
    step: number,
    part: { id?: string; name?: string; arguments?: unknown },
  ) => {
    const existing = part.id ? items.find((it) => it.kind === 'tool' && it.id === part.id) : undefined;
    if (existing) {
      existing.name = part.name ?? existing.name;
      existing.arguments = part.arguments;
      return;
    }
    items.push({ kind: 'tool', step, id: part.id, name: part.name, arguments: part.arguments });
  };

  for (const ev of events) {
    // 控制与指标事件不进对话转录，也不打断当前活动组。
    // compacting 的 start/done 只是压缩进度；压缩结果由 compacted 在组内展示。
    if (
      ev.type === 'status' ||
      ev.type === 'close' ||
      ev.type === 'context_usage' ||
      ev.type === 'compacting' ||
      ev.type === 'queue'
    )
      continue;

    // 用户消息独立成行（右对齐气泡），并打断当前活动组。
    if (ev.type === 'user_message') {
      closeActivity(true);
      groups.push({ type: 'user', id: `u-${ev.step}`, step: ev.step, message: ev.message || '' });
      continue;
    }

    if (ev.type === 'content') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({ kind: 'content', step: ev.step, content: ev.content });
      continue;
    }

    // 流式「说」增量：拼回本轮请求里已有的 content 块（打字机效果）；
    // 没有可拼接的（比如恢复会话时第一事件就是 delta）才新起一条。
    if (ev.type === 'content_delta') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = findAppendTarget('content');
      if (target) {
        target.content = (target.content || '') + (ev.delta || '');
        target.streaming = true;
      } else {
        current.items.push({
          kind: 'content',
          step: ev.step,
          content: ev.delta || '',
          streaming: true,
        });
      }
      continue;
    }

    // 一段「说」的流结束：撤掉打字机光标，并记下这段回复的耗时
    if (ev.type === 'content_end') {
      endStreamKind('content', ev.duration_ms);
      continue;
    }

    // 思考流（reasoning）：与「说」（content）分开成独立的可折叠卡片。
    // 增量拼回本轮请求里已有的思考卡片（交替思考的尾部也归位到上面那张），
    // 没有可拼接的才新起一条。
    if (ev.type === 'reasoning_delta') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = findAppendTarget('reasoning');
      if (target) {
        target.content = (target.content || '') + (ev.delta || '');
        target.streaming = true;
      } else {
        current.items.push({
          kind: 'reasoning',
          step: ev.step,
          content: ev.delta || '',
          streaming: true,
        });
      }
      continue;
    }

    // 思考流结束：卡片收尾（撤光标、记耗时）。只挂在已有卡片上、不新建——
    // 重建时的卡片由 assistant_message 的 parts 建（delta 是瞬态的，不回放）。
    if (ev.type === 'reasoning_end') {
      endStreamKind('reasoning', ev.duration_ms);
      continue;
    }

    // 助手消息：思考/正文/工具调用的有序段落，转录里文本的唯一来源。
    // delta 是瞬态的（刷新/切会话后根本不存在），重建完全靠这里；实时流里则
    // 按顺序"认领"delta 已经建好的卡片、写入权威文本（幂等，不会多出一份）。
    // streaming=true 的是"进行中"快照（status().streaming 合成），后续 delta
    // 会继续往这批卡片上追加。
    if (ev.type === 'assistant_message') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const partial = Boolean(ev.streaming);
      for (const part of ev.parts || []) {
        if (part.type === 'tool_call') {
          upsertToolCall(current.items, ev.step, part);
          continue;
        }
        const kind = part.type === 'reasoning' ? 'reasoning' : 'content';
        let claimed: ActivityItem | undefined;
        for (let i = partsCursor; i < current.items.length; i += 1) {
          if (current.items[i].kind === kind) {
            claimed = current.items[i];
            partsCursor = i + 1;
            break;
          }
        }
        if (claimed) {
          claimed.content = part.text;
          claimed.streaming = partial;
        } else {
          current.items.push({ kind, step: ev.step, content: part.text, streaming: partial });
          partsCursor = current.items.length;
        }
      }
      continue;
    }

    if (ev.type === 'tool_call') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      upsertToolCall(current.items, ev.step, { id: ev.id, name: ev.name, arguments: ev.arguments });
      continue;
    }

    if (ev.type === 'tool_result') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = ev.id ? current.items.find((it) => it.kind === 'tool' && it.id === ev.id) : undefined;
      if (target) {
        target.ok = ev.ok;
        target.result = ev.result;
        target.error = ev.error;
        target.durationMs = ev.duration_ms;
        reconcileSubagentStatuses(target);
      } else {
        current.items.push({
          kind: 'tool',
          step: ev.step,
          id: ev.id,
          name: ev.name,
          ok: ev.ok,
          result: ev.result,
          error: ev.error,
          durationMs: ev.duration_ms,
        });
      }
      continue;
    }

    // 权限审批：挂到**本次工具调用**那一行上（不单独成行），卡片就摆在那行下面。
    // 后端在真正执行写操作前发这条事件并挂起，等用户点允许/拒绝。
    if (ev.type === 'permission_request') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const target = ev.tool_call_id
        ? current.items.find((it) => it.kind === 'tool' && it.id === ev.tool_call_id)
        : undefined;
      const item: ActivityItem =
        target ?? { kind: 'tool', step: ev.step, id: ev.tool_call_id, name: ev.name };
      if (!target) current.items.push(item);
      item.permission = {
        id: ev.id || '',
        name: ev.name || item.name || '',
        label: ev.label || '',
        risk: ev.risk || '',
        mode: ev.mode || '',
        arguments:
          ev.arguments && typeof ev.arguments === 'object' && !Array.isArray(ev.arguments)
            ? (ev.arguments as Record<string, unknown>)
            : undefined,
        // 后端算好的「将要变更」（编辑类工具才有；算不出就没有这个键）
        preview: ev.preview,
      };
      continue;
    }

    // wait 工具的倒计时事件：挂到**本次调用**那一行上，不单独成行。
    // 按 id 匹配；老日志/异常情形没有 id 时，退回"最近一个还没收到 wait_end 的
    // wait 行"——绝不能笼统找第一个，同一活动组里等待过多次时，tick 会一直打到
    // 最早那行，后面几次等待就没有倒计时条了（只有个笼统的"进行中"）。
    if (ev.type === 'wait_start' || ev.type === 'wait_tick' || ev.type === 'wait_end') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      let target = ev.id
        ? current.items.find((it) => it.kind === 'tool' && it.id === ev.id)
        : undefined;
      if (!target) {
        for (let i = current.items.length - 1; i >= 0; i -= 1) {
          const it = current.items[i];
          if (it.kind === 'tool' && it.name === 'wait' && it.waitInterrupted === undefined) {
            target = it;
            break;
          }
        }
      }
      if (target) {
        if (typeof ev.total_ms === 'number') target.waitTotalMs = ev.total_ms;
        if (typeof ev.remaining_ms === 'number') target.waitRemainingMs = ev.remaining_ms;
        if (ev.type === 'wait_end') {
          target.waitInterrupted = Boolean(ev.interrupted);
          target.waitRemainingMs = 0;
        }
      }
      continue;
    }

    // 上下文压缩：折叠成活动组里的一条提示行，不打断当前活动组。
    if (ev.type === 'compacted') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      current.items.push({
        kind: 'compact',
        step: ev.step,
        removed: ev.removed,
        summaryChars: ev.summary_chars,
        tokensBefore: ev.tokens_before,
        tokensAfter: ev.tokens_after,
      });
      continue;
    }

    // LLM 请求失败自动重试：先在活动组里放一条带倒计时的重试提示行，
    // 并丢掉上一次尝试已经流出的半截内容（那次请求已作废，重试会整段重发）。
    // 同一次请求的多次重试共用一行：只有 attempt 从 1 开始才算新一轮请求、另起一行
    //（attempt 递增说明是同一请求的第 N 次退避，就地更新计数与原因）。
    if (ev.type === 'llm_retry_start') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      // 从末尾往前清掉本次失败尝试流出的 content/reasoning；遇到工具/压缩/重试行
      // 说明已到上一轮边界，停止清除，避免误删之前的内容。
      for (let i = current.items.length - 1; i >= 0; i -= 1) {
        const it = current.items[i];
        if (it.kind === 'content' || it.kind === 'reasoning') {
          current.items.splice(i, 1);
          continue;
        }
        break;
      }
      let retryItem: ActivityItem | undefined;
      if ((ev.attempt ?? 1) > 1) {
        for (let i = current.items.length - 1; i >= 0; i -= 1) {
          const it = current.items[i];
          if (it.kind === 'retry') {
            retryItem = it;
            break;
          }
        }
      }
      if (!retryItem) {
        retryItem = { kind: 'retry', step: ev.step };
        current.items.push(retryItem);
      }
      retryItem.step = ev.step;
      retryItem.attempt = ev.attempt;
      retryItem.maxAttempts = ev.max_attempts;
      retryItem.retryDelayMs = ev.delay_ms;
      retryItem.retryStartedAtMs = typeof ev.ts === 'number' ? ev.ts * 1000 : Date.now();
      retryItem.retryCode = ev.code;
      retryItem.retryReason = ev.reason;
      retryItem.retryDone = false;
      continue;
    }

    // 退避结束、下一次尝试已发出：把重试行定格为「已重试」，停掉倒计时。
    if (ev.type === 'llm_retry_end') {
      if (current) {
        for (let i = current.items.length - 1; i >= 0; i -= 1) {
          const it = current.items[i];
          if (it.kind === 'retry') {
            it.retryDone = true;
            break;
          }
        }
      }
      continue;
    }

    // 子代理（run_subagents）：挂到发起它的那次工具调用下面，一层就够——子代理没有子代理。
    // 事件带 id（本次派发）与 parent_id（那次工具调用），据此定位到行与具体哪个子代理。
    if (ev.type.startsWith('subagent_')) {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const host =
        current.items.find((it) => it.kind === 'tool' && it.id === ev.parent_id) ??
        // 老记录/事件乱序时的兜底：本组最后一次 run_subagents 调用
        [...current.items].reverse().find((it) => it.kind === 'tool' && it.name === 'run_subagents');
      if (!host) {
        continue;
      }
      if (!host.subagents) host.subagents = [];
      let run = host.subagents.find((item) => item.id === ev.id);
      if (!run) {
        run = {
          id: ev.id || '',
          agent: ev.agent || '',
          label: ev.label || translate("agent:timeline.label_message_proxy"),
          file: ev.file || '',
          indexes: ev.indexes || '',
          brief: ev.brief || '',
          status: 'running',
          steps: [],
          // 用后端给的时间戳：subagent_start 是持久事件，刷新/切页会重放它，
          // 拿"事件到达时间"会让进行中的计时每次重建都归零。
          startedAt: typeof ev.started_at === 'number' ? ev.started_at * 1000 : Date.now(),
        };
        host.subagents.push(run);
      }
      // 重试期间后端会插一条 subagent_retry（瞬态）：标出来让用户知道它在退避，
      // 不是卡住了。任何后续动静都说明这次重试成功了，标记就地清掉。
      if (ev.type === 'subagent_retry') {
        run.retry = {
          attempt: typeof ev.attempt === 'number' ? ev.attempt : 1,
          maxAttempts: typeof ev.max_attempts === 'number' ? ev.max_attempts : 0,
          code: ev.code || '',
          reason: ev.reason || '',
        };
        continue;
      }
      run.retry = undefined;
      if (ev.type === 'subagent_message') {
        if (ev.text) run.steps.push({ kind: 'text', round: ev.round || 0, text: ev.text });
      } else if (ev.type === 'subagent_tool_call') {
        run.steps.push({
          kind: 'tool',
          id: ev.tool_call_id || '',
          name: ev.name || '',
          args: ev.arguments,
          at: Date.now(),
        });
      } else if (ev.type === 'subagent_tool_result') {
        const step = [...run.steps]
          .reverse()
          .find((item): item is Extract<SubagentStep, { kind: 'tool' }> =>
            item.kind === 'tool' && item.id === ev.tool_call_id);
        if (step) {
          step.ok = ev.ok !== false;
          step.result = ev.result;
          step.error = ev.error || '';
          step.durationMs = ev.duration_ms;
        }
      } else if (ev.type === 'subagent_done') {
        run.status = (ev.status as SubagentRun['status']) || 'done';
        run.report = ev.report || '';
        run.turns = ev.turns;
        run.toolCalls = ev.tool_calls;
        // 新事件用 proofread_comment；旧会话落盘的是 doubts，切页重放时兜底认一下
        const comments = ev.proofread_comment ?? ev.doubts;
        run.proofreadComment = typeof comments === 'number' ? comments : 0;
        run.durationMs = ev.duration_ms;
        run.error = ev.error || '';
        run.finishedAt = typeof ev.finished_at === 'number' ? ev.finished_at * 1000 : Date.now();
      }
      continue;
    }

    // finish 是回合的收尾回复：作为活动组的 final 消息，渲染时提升为
    // 顶层普通文本（不折进折叠区），像对话里最后一条普通消息。
    if (ev.type === 'finish') {
      if (!current) current = { type: 'activity', id: `a-${ev.step}`, items: [] };
      const summary = ev.summary || '';
      // 收尾回复保留折叠区里的流式 content（显示「思考 · 耗时」），
      // summary 另行提升为顶层 final 消息，两者并存。
      if (summary) {
        current.finalContent = { kind: 'content', step: ev.step, content: summary, final: true };
      }
      closeActivity(true); // 回合结束：把组压进 groups，后续事件（新的 user_message 等）起新组
      continue;
    }

    // Terminal moments close the current activity run.
    closeActivity(true);
    if (ev.type === 'error') {
      groups.push({
        type: 'error',
        id: `e-${ev.step}`,
        step: ev.step,
        message: ev.message || translate("common:actions.unknownError"),
        traceback: ev.traceback,
      });
    } else if (ev.type === 'stopped') {
      groups.push({
        type: 'stopped',
        id: `s-${ev.step}`,
        step: ev.step,
        reason: ev.reason || translate("agent:timeline.reason_push_stop"),
      });
    }
  }

  closeActivity();
  return groups;
}

/** 把一批流事件接到转录末尾；相邻的同类增量（content/reasoning delta）合并成一条，
 *  拼接顺序不变，所以 buildTimeline 的结果与逐条追加一致，但数组不再按 token 增长。 */
export function appendStreamEvents(prev: AgentEvent[], batch: AgentEvent[]): AgentEvent[] {
  const next = prev.slice();
  for (const ev of batch) {
    const last = next[next.length - 1];
    if (
      (ev.type === 'content_delta' || ev.type === 'reasoning_delta') &&
      last &&
      last.type === ev.type
    ) {
      next[next.length - 1] = { ...last, delta: (last.delta || '') + (ev.delta || '') };
    } else {
      next.push(ev);
    }
  }
  return next;
}

export function isTerminal(s: string): boolean {
  return s === 'awaiting_input' || s === 'done' || s === 'failed' || s === 'idle';
}

/** 当前活动组最后一条被渲染的条目（决定运行指示器的文案与配色）。 */
export function lastActivityItem(timeline: TimelineGroup[]): ActivityItem | null {
  const last = timeline[timeline.length - 1];
  if (last && last.type === 'activity' && last.items.length) {
    return last.items[last.items.length - 1];
  }
  return null;
}

export function workingLabel(timeline: TimelineGroup[]): string {
  const item = lastActivityItem(timeline);
  if (item) {
    if (item.kind === 'content' || item.kind === 'reasoning') return translate("agent:timeline.workingLabel_message_text");
    // 重试/压缩行不是工具调用、没有 name，走 toolMeta 会误显示成「调用工具」
    if (item.kind === 'retry') {
      const attempt = item.attempt ?? 1;
      const max = item.maxAttempts ?? 0;
      return max > 0 ? translate("agent:timeline.workingLabel_message_retry", { attempt: attempt, max: max }) : translate("agent:timeline.workingLabel_message_retryVariant2");
    }
    if (item.kind === 'compact') return translate("agent:timeline.workingLabel_message_context");
    // 工具行只有在**还没结果**时才代表"正在做这件事"（判定同 toolRowPhases）。
    // 结果一到这行就完成了——继续挂它的 running 文案会让"等你回答…"一直亮着，
    // 明明已经答完、后端都开始请求下一轮了。
    if (item.ok === undefined && item.result === undefined && item.error === undefined) {
      return `${toolMeta(item.name).running}…`;
    }
    return translate("agent:timeline.workingLabel_message_processing");
  }
  return translate("agent:timeline.workingLabel_message_pendingStart");
}

export function liveTail(items: ActivityItem[]): string {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const it = items[i];
    if ((it.kind === 'content' || it.kind === 'reasoning') && it.content) {
      // 取思考文本最后一行：折叠头部读起来像实时滚动的一行预览
      return lastReasoningLine(it.content);
    }
    if (it.kind === 'tool') {
      const s = toolMeta(it.name).summary(asArgs(it.arguments));
      return [toolMeta(it.name).action, s].filter(Boolean).join(' ');
    }
  }
  return '';
}

/* ── Reasoning row（模型「想」的思考过程）──
   与「说」分开：想用可折叠卡片，**默认折叠**（思考过程不自动铺开，要看细节点一下）。
   折叠且正在思考时，标题右侧用跑马灯滚动最近一行，保留"能感知在思考"的实时感；
   展开后内容就在眼前，跑马灯随即消失。 */

/** 折叠时跑马灯最多滚多少字符（取最近的一段，太长会滚得让人看不清）。 */
export const REASONING_MARQUEE_CHARS = 160;

/** 思考文本压成一行：去掉标题/加粗标记，取最后一行（活动组头部的静态预览用）。 */
function lastReasoningLine(text: string): string {
  const lines = text
    .split('\n')
    .map((line) => line.replace(/^#+\s*|\*\*/g, '').trim())
    .filter(Boolean);
  return lines[lines.length - 1] || '';
}
