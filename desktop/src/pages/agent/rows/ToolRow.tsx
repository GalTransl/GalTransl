import { type ReactNode, useEffect, useRef, useState } from 'react';
import { Icon } from '../../../components/Icon';
import { SubagentList } from './SubagentList';
import type { ActivityItem } from '../timeline';
import { asArgs, formatCountdown, formatDuration, formatPayload, str, toolMeta } from '../toolMeta';
import { manualOpenState } from '../uiState';

/** 工具行的"轮到哪一步"。一批工具调用在后端是**挨个执行**的（runtime 里
    `for tc in tool_calls`），但助手消息的 parts 会先把整批一次性画成行——所以"哪一行在跑"
    不能看是不是最后一行，得看谁还没有结果：

    - running：第一个还没有结果的行，正在执行；
    - awaiting：它卡在权限门禁上等用户批准（那张审批卡就画在这行下面）；
    - queued：排在它后面、还没轮到的（后端还没轮到它们，界面上不该显示成"进行中"）；
    - stale：不在运行中的组里却也没有结果——历史里断在半路的那次调用（进程被杀等）。

    返回数组与 items 下标对齐；非工具行、已有结果的行都是 undefined。 */
type ToolPhase = 'running' | 'awaiting' | 'queued' | 'stale';

export function toolRowPhases(items: ActivityItem[], isLive: boolean): (ToolPhase | undefined)[] {
  const phases: (ToolPhase | undefined)[] = items.map(() => undefined);
  let isFirstPending = true;
  items.forEach((it, i) => {
    if (it.kind !== 'tool') return;
    if (it.ok !== undefined || it.result !== undefined || it.error !== undefined) return;
    if (!isLive) phases[i] = 'stale';
    else if (isFirstPending) phases[i] = it.permission ? 'awaiting' : 'running';
    else phases[i] = 'queued';
    isFirstPending = false;
  });
  return phases;
}

export function ToolRow({
  item,
  phase,
  persistKey,
}: {
  item: ActivityItem;
  /** 这一行在本次执行里轮到哪一步（见 toolRowPhases） */
  phase?: ToolPhase;
  persistKey: string;
}) {
  const stateKey = `${persistKey}::tool-${item.id || item.step}`;

  // 写入类工具的变更卡片数据（后端在结果里带回）：
  // - changes: [{path, before, after, kind}] 键值级 before→after
  // - line_diff: {rows: [{op: add|del, line}], truncated} 行级 diff（save_dict）
  // - deleted_preview: [{index, text}] 被删条目（delete_transl_cache）
  const changeList = extractChangeList(item.result);
  // 写类工具的可选入参 reason（模型说明"为什么改"）：有变更卡就画在卡里，没有
  // （如 save_dict 只新建空文件时的返回不含 changes）就在正文里单独给一行。
  const reason = extractReason(item.result);
  const ok = item.ok !== false;
  const pending = item.ok === undefined && item.result === undefined && !item.error;
  const awaiting = phase === 'awaiting';
  const isRunning = phase === 'running';
  const subagents = item.subagents ?? [];
  const hasSubagents = subagents.length > 0;
  // 正等批准时后端已经算好的「将要变更」（审批卡上摆的那份，见 PermissionCard）：
  // 有它就不必为了"它准备改什么"把这一行撑开去铺原始 JSON。
  const permPreview = awaiting ? extractChangeList(item.permission?.preview) : null;

  // 行**默认展开**的三种情形：
  // 1) 已有变更卡 —— 改了什么是这次调用的重点，diff 不该藏在一次点击后面；
  // 2) 有 reason 却没有变更卡（save_dict 只新建空文件之类不产生 changes）——理由也该直接可见；
  // 3) **正等着批准、但算不出 diff 的调用** —— 整文件删缓存、启动翻译、派子代理这类没有
  //    可比对的 before→after，用户要判断就只能看原始参数，那还是替它铺开（能算 diff 的
  //    都摆在审批卡上了，这一行不必再展开一次）。
  // manualOpenState 里只记"用户手动点过"的选择——记过就听用户的，没记过才用这个默认值
  // （重挂/刷新后同一规则）。
  const autoOpen = Boolean(changeList || reason) || hasSubagents || (awaiting && !permPreview);
  const [open, setOpenRaw] = useState(() => manualOpenState.get(stateKey) ?? autoOpen);
  const setOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };
  // 状态比行晚到（结果比行晚到、批准请求也可能晚一拍）：该展开了就展开。用户手动点过就不抢，
  // 否则会跟"刚点开又自己收起/展开"打架。
  const autoOpenedRef = useRef(autoOpen);
  useEffect(() => {
    if (!autoOpen || autoOpenedRef.current) return;
    autoOpenedRef.current = true;
    if (!manualOpenState.has(stateKey)) setOpenRaw(true);
  }, [autoOpen, stateKey]);

  const meta = toolMeta(item.name);
  const summary = meta.summary(asArgs(item.arguments));

  // 把原始参数/结果收进折叠菜单：写入调用要看的通常是 diff，JSON 参数与整份结果只是证据；
  // 派子代理的调用同理想——任务清单和各份报告已经在子代理行里逐条显示了，那串 JSON 是重复的。
  // 失败时例外：错误全文要直接可见（那时子代理行也未必建得起来）。
  const foldRaw = ok && (hasSubagents || (Boolean(changeList) && !pending));

  // wait 行：等待期间显示倒计时（只出秒数，不画进度条）。
  const isWait = item.name === 'wait';
  const waiting = isWait && pending && typeof item.waitTotalMs === 'number';
  const waitTotal = item.waitTotalMs || 0;
  const waitRemaining = item.waitRemainingMs ?? waitTotal;

  // 询问用户的结果：后端已经给出「问题：答案」的可读文本，转录里别再甩一坨 JSON
  const askSummary =
    item.name === 'ask_user' && ok
      ? str((item.result as Record<string, unknown> | undefined)?.summary)
      : '';
  const resultText = askSummary || formatPayload(ok ? item.result : item.error);
  const hasDetails = Boolean(summary || resultText || item.arguments || changeList);
  const longResult = resultText.length > 400;

  // 权限被拒不是故障，是用户的决定：状态标签说"已拒绝"，免得看着像系统出错。
  // 「用户没有在」是旧会话里"审批到点自动拒绝"留下的文案——现在没有超时了，留着这句
  // 只是为了让老转录仍显示成"已拒绝"而不是"失败"。
  const denied =
    !ok && typeof item.error === 'string' && /^(用户拒绝权限|用户没有在|回合被停止)/.test(item.error);
  // 状态标签以**这一行自己的进度**为准（见 toolRowPhases）：没有结果就不是"完成"。
  // 以前按"是不是最后一行"判断，整批里等在中间的那行会写成完成——最刺眼的就是
  // "还在等你批准，却已经显示完成"。排队中/未完成走默认的灰，不给"在跑"那种蓝色呼吸点。
  const state = waiting
    ? { label: formatCountdown(waitRemaining), tone: 'running', countdown: true }
    : awaiting
      ? { label: '待批准', tone: 'running', countdown: false }
      : isRunning
        ? { label: '进行中', tone: 'running', countdown: false }
        : phase === 'queued'
          ? { label: '排队中', tone: '', countdown: false }
          : phase === 'stale'
            ? { label: '未完成', tone: '', countdown: false }
            : isWait && item.waitInterrupted
              ? { label: '已中断', tone: '', countdown: false }
              : denied
                ? { label: '已拒绝', tone: 'error', countdown: false }
                : ok
                  ? { label: '完成', tone: 'done', countdown: false }
                  : { label: '失败', tone: 'error', countdown: false };

  return (
    <div className={`agent-tool${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="agent-tool__header"
        onClick={() => hasDetails && setOpen((v) => !v)}
        disabled={!hasDetails}
        aria-expanded={open}
      >
        <span className="agent-tool__icon"><Icon name={meta.icon} /></span>
        <span className={`agent-tool__name${state.tone === 'running' ? ' is-running' : ''}`}>{meta.action}</span>
        {summary ? <span className="agent-tool__summary">{summary}</span> : null}
        {changeList ? <span className="agent-tool__diffbadge">±{changeList.total}</span> : null}
        <span className={`agent-tool__state${state.tone ? ` is-${state.tone}` : ''}${state.countdown ? ' is-countdown' : ''}`}>
          <span className="agent-tool__state-dot" />
          {state.label}
        </span>
        <span className="agent-tool__caret">›</span>
      </button>
      {open ? (
        <div className="agent-tool__body">
          {hasSubagents ? <SubagentList runs={subagents} /> : null}
          {changeList ? (
            <ChangeListCard data={changeList} />
          ) : reason ? (
            <ChangeReason text={reason} />
          ) : null}
          {foldRaw ? (
            <RawToolData
              args={item.arguments}
              resultText={resultText}
              resultTitle={ok ? '结果' : '错误'}
              tone={ok ? 'default' : 'error'}
              durationMs={item.durationMs}
              truncate={longResult ? 1200 : 0}
            />
          ) : (
            <>
              {item.arguments !== undefined ? (
                <ToolBlock title="参数" content={formatPayload(item.arguments)} mono />
              ) : null}
              {resultText ? (
                <ToolBlock
                  title={ok ? '结果' : '错误'}
                  content={resultText}
                  mono
                  truncate={longResult ? 1200 : 0}
                  tone={ok ? 'default' : 'error'}
                  durationMs={item.durationMs}
                />
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** 参数 / 结果块：一行标题 + 一块等宽正文（内容被截断时给「展开全部」）。 */
export function ToolBlock({
  title,
  content,
  mono,
  truncate = 0,
  tone = 'default',
  durationMs,
}: {
  title: string;
  content: string;
  mono?: boolean;
  truncate?: number;
  tone?: 'default' | 'error';
  durationMs?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const clipped = truncate > 0 && content.length > truncate && !expanded;
  const shown = clipped ? content.slice(0, truncate) + '…' : content;
  return (
    <div className={`agent-toolblock${tone === 'error' ? ' is-error' : ''}`}>
      <div className="agent-toolblock__head">
        <span className="agent-toolblock__title">{title}</span>
        {typeof durationMs === 'number' && title === '结果' ? (
          <span className="agent-toolblock__duration">{formatDuration(durationMs)}</span>
        ) : null}
      </div>
      <pre className={`agent-toolblock__pre${mono ? ' is-mono' : ''}`}>{shown}</pre>
      {truncate > 0 && content.length > truncate ? (
        <button type="button" className="agent-toolblock__toggle" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : `展开全部（${content.length} 字符）`}
        </button>
      ) : null}
    </div>
  );
}

/** 写入类调用的「原始参数/结果」：合成一个折叠菜单，默认折起。

    变更卡已经说清改了什么，原始 JSON 与整份结果只是证据——要看再展开。折成两行
    （参数一行、结果一行）点起来目标太小、还容易点错，合成一行开门更像"翻原始数据"；
    折叠态右侧给总字符数，一眼知道里面有多少东西。 */
function RawToolData({
  args,
  resultText,
  resultTitle,
  tone,
  durationMs,
  truncate,
}: {
  args: unknown;
  resultText: string;
  resultTitle: string;
  tone: 'default' | 'error';
  durationMs?: number;
  truncate?: number;
}) {
  const [open, setOpen] = useState(false);
  const argsText = args === undefined ? '' : formatPayload(args);
  if (!argsText && !resultText) return null;
  return (
    <div className="agent-rawdata">
      <button
        type="button"
        className="agent-toolblock__head is-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-toolblock__label">
          <span className="agent-toolblock__caret" aria-hidden>›</span>
          <span className="agent-toolblock__title">原始参数/结果</span>
        </span>
        <span className="agent-toolblock__meta">
          {open ? null : (
            <span className="agent-toolblock__len">{argsText.length + resultText.length} 字符</span>
          )}
        </span>
      </button>
      {open ? (
        <>
          {argsText ? <ToolBlock title="参数" content={argsText} mono /> : null}
          {resultText ? (
            <ToolBlock
              title={resultTitle}
              content={resultText}
              mono
              truncate={truncate}
              tone={tone}
              durationMs={durationMs}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/* ── 写入类工具的变更卡片（diff 风格） ──
   后端在写入结果里带回三类结构之一/组合：
   - changes: [{path, before, after, kind}] —— update_project_config、
     manage_problem_filter、patch_transl_cache、save_name_table
   - line_diff: {rows, truncated} —— save_dict 的整文本行级 diff
   - deleted_preview: [{index, text}] —— delete_transl_cache 被删条目

   审批卡上的「将要变更」用的是**同一份结构**（后端在挂起前只读算出来，见
   _preview_tool_changes），所以两处共用这一个渲染，没有第二套 diff 视图。 */

type ChangeEntry = { path: string; before?: unknown; after?: unknown; kind?: string };

type DiffRow = { op: 'add' | 'del'; line: string };

type ChangeData = {
  changes?: ChangeEntry[];
  line_diff?: { rows?: DiffRow[]; truncated?: boolean };
  deleted_preview?: { index: number; text: string }[];
  /** 模型填的「为什么改」（写类工具的可选入参 reason，后端原样回传） */
  reason?: string;
  total: number;
};

/** 写类工具结果里模型交代的原因（可选）。空/非字符串都当没填。 */
function extractReason(result: unknown): string {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return '';
  const value = (result as Record<string, unknown>).reason;
  return typeof value === 'string' ? value.trim() : '';
}

export function extractChangeList(result: unknown): ChangeData | null {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
  const r = result as Record<string, unknown>;
  const changes = Array.isArray(r.changes) ? (r.changes as ChangeEntry[]) : undefined;
  const lineDiff =
    r.line_diff && typeof r.line_diff === 'object' && Array.isArray((r.line_diff as Record<string, unknown>).rows)
      ? (r.line_diff as { rows?: DiffRow[]; truncated?: boolean })
      : undefined;
  const deletedPreview = Array.isArray(r.deleted_preview) ? (r.deleted_preview as { index: number; text: string }[]) : undefined;
  const total =
    (changes?.length || 0) +
    (lineDiff?.rows?.length || 0) +
    (deletedPreview?.length || 0);
  if (!total) return null;
  return {
    changes,
    line_diff: lineDiff,
    deleted_preview: deletedPreview,
    reason: extractReason(result) || undefined,
    total,
  };
}

function fmtChangeValue(v: unknown): string {
  if (v === null || v === undefined) return '（空）';
  if (typeof v === 'string') return v.length > 80 ? v.slice(0, 77) + '…' : v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** 模型填的「为什么改」：变更卡顶部一行，没有变更卡时（如 save_dict 只新建空文件时不产生
    changes）在工具正文里单独显示，不然填了原因却没地方看。 */
function ChangeReason({ text }: { text: string }) {
  return (
    <div className="agent-changes__reason">
      <span className="agent-changes__reason-label">原因</span>
      <span className="agent-changes__reason-text">{text}</span>
    </div>
  );
}

/** 变更卡。title 只有审批卡会换（那边的同一份数据是"将要变更"，还没真写）。 */
export function ChangeListCard({ data, title = '变更' }: { data: ChangeData; title?: string }) {
  const rows: ReactNode[] = [];

  if (data.line_diff?.rows?.length) {
    // diff 全量渲染、不再折叠：容器限高 + 内部滚动（见 .agent-changes__body 的
    // max-height）。改了什么应当一眼看完，不该先点一次"展开全部"。
    const diffRows = data.line_diff.rows;
    for (const [i, r] of diffRows.entries()) {
      rows.push(
        <div key={`d-${i}`} className={`agent-changes__dline agent-changes__dline--${r.op}`}>
          <span className="agent-changes__sign">{r.op === 'add' ? '+' : '−'}</span>
          <span className="agent-changes__dtext">{r.line || ' '}</span>
        </div>,
      );
    }
    if (data.line_diff.truncated) {
      // 后端生成 diff 时就截断过（总行数上限），如实说明，不是界面的折叠
      rows.push(<div key="d-trunc" className="agent-changes__more">diff 过长已截断</div>);
    }
  }

  if (data.deleted_preview?.length) {
    for (const p of data.deleted_preview) {
      rows.push(
        <div key={`del-${p.index}`} className="agent-changes__dline agent-changes__dline--del">
          <span className="agent-changes__sign">−</span>
          <span className="agent-changes__path">#{p.index}</span>
          <span className="agent-changes__dtext">{p.text || '（空译文）'}</span>
        </div>,
      );
    }
  }

  if (data.changes?.length) {
    for (const [i, c] of data.changes.entries()) {
      rows.push(
        <div key={`c-${i}`} className="agent-changes__item">
          <span className="agent-changes__path">{c.path}</span>
          {c.kind !== 'add' ? (
            <span className="agent-changes__old">
              <span className="agent-changes__sign">−</span>
              {fmtChangeValue(c.before)}
            </span>
          ) : null}
          {c.kind !== 'remove' ? (
            <span className="agent-changes__new">
              <span className="agent-changes__sign">+</span>
              {fmtChangeValue(c.after)}
            </span>
          ) : null}
        </div>,
      );
    }
  }

  return (
    <div className="agent-changes">
      <div className="agent-changes__head">
        <span className="agent-changes__title">{title}</span>
        <span className="agent-changes__count">±{data.total}</span>
      </div>
      {data.reason ? <ChangeReason text={data.reason} /> : null}
      {rows.length ? <div className="agent-changes__body">{rows}</div> : null}
    </div>
  );
}
