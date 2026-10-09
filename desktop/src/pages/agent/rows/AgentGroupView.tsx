import { usePageActive } from '../../../components/PageActivity';
import { t as translate, useUiLanguage } from "../../../i18n";
import { memo, useEffect, useRef, useState } from 'react';
import { AnimatedDisclosure } from './AnimatedDisclosure';
import { AgentMarkdown } from '../../../components/AgentCacheRef';
import { Icon } from '../../../components/Icon';
import { ToolRow, toolRowPhases } from './ToolRow';
import { TranslationJobCard } from './TranslationJobCard';
import {
  type ActivityItem,
  liveTail,
  REASONING_MARQUEE_CHARS,
  type TimelineGroup,
} from '../timeline';
import { formatDuration, formatTokenCount, translationJobId } from '../toolMeta';
import { liveStartedState, manualOpenState } from '../uiState';

export const AgentGroupView = memo(function AgentGroupView({
  group,
  isLive,
  projectDir,
  persistKey,
}: {
  group: TimelineGroup;
  isLive: boolean;
  projectDir: string;
  persistKey: string;
}) {
  const uiLanguage = useUiLanguage();
  // Terminal groups render as notices and hold no disclosure state; dispatch
  // them before the activity component so its hooks never run conditionally.
  if (group.type === 'user') return <UserMessageRow message={group.message} />;
  if (group.type === 'error') return <ErrorNotice group={group} />;
  if (group.type === 'stopped') return <StoppedNotice group={group} />;
  if (group.type === 'activity' && group.finalContent) {
    return (
      <>
        {group.items.length > 0 ? (
          <AgentActivityGroup group={group} isLive={isLive} projectDir={projectDir} persistKey={persistKey} />
        ) : null}
        <FinalMessage item={group.finalContent} projectDir={projectDir} />
      </>
    );
  }
  return <AgentActivityGroup group={group} isLive={isLive} projectDir={projectDir} persistKey={persistKey} />;
});

/** 回合收尾回复：顶层普通消息，像聊天里最后一条回答。 */
const FinalMessage = memo(function FinalMessage({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  const uiLanguage = useUiLanguage();
  return <AgentMarkdown text={item.content || ''} projectDir={projectDir} className="agent-final" />;
});

function UserMessageRow({ message }: { message: string }) {
  const uiLanguage = useUiLanguage();
  return (
    <div className="agent-row agent-row--user">
      <div className="agent-bubble agent-bubble--user">
        <div className="agent-bubble__label">{translate("agent:agentGroupView.agentBubbleAgentBubbleUser_message_text")}</div>
        <div className="agent-bubble__text">{message}</div>
      </div>
    </div>
  );
}

function AgentActivityGroup({
  group,
  isLive,
  projectDir,
  persistKey,
}: {
  group: Extract<TimelineGroup, { type: 'activity' }>;
  isLive: boolean;
  projectDir: string;
  persistKey: string;
}) {
  const uiLanguage = useUiLanguage();
  const active = usePageActive();
  const stateKey = `${persistKey}::${group.id}`;
  const [open, setOpenRaw] = useState(() => {
    const saved = manualOpenState.get(stateKey);
    return saved === undefined ? isLive : saved;
  });
  // 恢复过用户选择的组从挂载之初就属于“手动控制”。否则下面的 effect 会在
  // 历史回合 isLive=false 时立刻把刚恢复的展开态重新收起。
  const userToggledRef = useRef(manualOpenState.has(stateKey));
  const setManualOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };
  const items = group.items;

  // Follow the live run: auto-expand while working, auto-collapse when settled,
  // unless the user took manual control of this group.
  useEffect(() => {
    if (userToggledRef.current) return;
    // 自动状态不写入 manualOpenState；只有用户点击才应取得永久控制权。
    setOpenRaw(isLive);
  }, [isLive]);

  // 运行中墙钟计时：live 时每秒跳动，结束冻结在最后值。
  const [now, setNow] = useState(() => Date.now());
  const liveStartedRef = useRef<number | null>(null);
  const wasLiveRef = useRef(isLive);
  const [frozenSec, setFrozenSec] = useState<number | null>(null);
  useEffect(() => {
    // 墙钟起点：进入 live 时记一次。首次挂载时 wasLiveRef 已等于 isLive，只会走
    // 「还没有起点」这一支——否则新建的活动组永远拿不到起点，头部会一直显示
    // 0ms（重试这类没有 durationMs 的活动尤其明显）。
    if (isLive) {
      // 起点优先取模块级缓存：重挂（切页面回来）时接着上次的时刻走，而不是重新计时
      if (liveStartedRef.current == null) {
        liveStartedRef.current = liveStartedState.get(stateKey) ?? Date.now();
      }
      liveStartedState.set(stateKey, liveStartedRef.current);
    } else if (wasLiveRef.current && liveStartedRef.current != null) {
      setFrozenSec(Math.max(0, Math.floor((Date.now() - liveStartedRef.current) / 1000)));
      // 回合已结束：起点没有保留价值，别让这张表越积越大
      liveStartedState.delete(stateKey);
    }
    wasLiveRef.current = isLive;
    if (!active || !isLive) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active, isLive, stateKey]);
  // 结束后展示用「事件耗时之和」兜底：恢复会话/刷新后没有墙钟起点。
  const totalMs = items.reduce((sum, it) => sum + (it.durationMs || 0), 0);
  const wallSec = liveStartedRef.current != null ? Math.max(0, Math.floor((now - liveStartedRef.current) / 1000)) : 0;
  const shownSec = isLive
    ? wallSec > 0
      ? wallSec
      : Math.max(0, Math.floor(totalMs / 1000))
    : frozenSec != null && frozenSec > 0
      ? frozenSec
      : Math.max(0, Math.floor(totalMs / 1000));

  // 「想」（reasoning）也算思考内容：只有思考流的回合标签用「思考」而非「处理」
  const hasContent = items.some((it) => it.kind === 'content' || it.kind === 'reasoning');
  const toolCount = items.filter((it) => it.kind === 'tool').length;
  // 压缩与重试提示都不算"工作步骤"：前者是后台维护动作，后者只是同一次请求
  // 的重发（一次重试一个步骤会把「重试 10 次」显示成 10 个步骤）
  const visibleCount = items.filter((it) => it.kind !== 'compact' && it.kind !== 'retry').length;

  // 文案：运行中「思考中/处理中 · Ns」，结束「已思考/已处理 Ns」
  const label = isLive
    ? translate("agent:agentGroupView.label_message_text", { value: hasContent && !toolCount ? translate("agent:agentGroupView.interpolation_fallback_text") : translate("agent:agentGroupView.interpolation_fallback_processing"), value2: formatDuration(shownSec * 1000) })
    : hasContent && !toolCount
      ? shownSec > 0 ? translate("agent:agentGroupView.label_message_done", { value: formatDuration(shownSec * 1000) }) : translate("agent:agentGroupView.label_message_textVariant2")
      : shownSec > 0
        ? translate("agent:agentGroupView.label_message_doneProcess", { value: formatDuration(shownSec * 1000) })
        : translate("agent:agentGroupView.label_message_textVariant3");

  const parts: string[] = [];
  if (visibleCount > 1) parts.push(translate("agent:agentGroupView.agentActivityGroup_push_count", { visibleCount: visibleCount }));

  // 预览小字：只在折叠且回合仍在跑时显示（展开时内容全可见，无需预览）
  const tail = isLive && !open ? liveTail(items) : '';

  // 每个工具行"轮到哪一步"（在跑 / 等批准 / 排队 / 断了）：见 toolRowPhases 的说明
  const phases = toolRowPhases(items, isLive);

  return (
    <div className={`agent-activity${open ? ' is-open' : ''}${isLive ? ' is-live' : ''}`}>
      <button
        type="button"
        className="agent-activity__header"
        onClick={() => {
          userToggledRef.current = true;
          setManualOpen((v) => !v);
        }}
        aria-expanded={open}
      >
        <span className="agent-activity__icon"><Icon name="spark" /></span>
        <span className={`agent-activity__label${isLive ? ' is-running' : ''}`}>{label}</span>
        {parts.length ? <span className="agent-activity__meta">{parts.join(' · ')}</span> : null}
        <span className="agent-activity__caret">›</span>
      </button>
      {tail ? <div className="agent-activity__preview">{tail}</div> : null}
      <AnimatedDisclosure open={open} className="agent-activity__collapse">
        {() => (
          <div className="agent-activity__collapse-inner">
            <div className="agent-activity__body">
              {items.map((item, i) =>
                item.kind === 'content' ? (
                  <ContentRow key={`t-${i}`} item={item} projectDir={projectDir} />
                ) : item.kind === 'reasoning' ? (
                  <ReasoningRow key={`r-${i}`} item={item} projectDir={projectDir} persistKey={persistKey} />
                ) : item.kind === 'compact' ? (
                  <CompactRow key={`c-${i}`} item={item} />
                ) : item.kind === 'retry' ? (
                  <RetryRow key={`rt-${i}`} item={item} />
                ) : item.kind === 'tool' && item.name === 'start_translation' && translationJobId(item) ? (
                  // 启动翻译换成工作台顶部卡的迷你版（带实时进度），不再是一坨 JSON
                  <TranslationJobCard key={`j-${item.id || i}`} item={item} projectDir={projectDir} />
                ) : (
                  <ToolRow key={`x-${item.id || i}`} item={item} phase={phases[i]} persistKey={persistKey} />
                ),
              )}
            </div>
          </div>
        )}
      </AnimatedDisclosure>
    </div>
  );
}

const ContentRow = memo(function ContentRow({ item, projectDir }: { item: ActivityItem; projectDir: string }) {
  const uiLanguage = useUiLanguage();
  // 模型「说」的回复：直接渲染为普通黑体纯文本，不再用可折叠卡片包裹。
  const text = item.content || '';
  const streaming = Boolean(item.streaming);

  return (
    <div className={`agent-content${streaming ? ' is-streaming' : ''}`}>
      <AgentMarkdown
        text={text}
        projectDir={projectDir}
        cursor={streaming}
        className="agent-content__text"
      />
    </div>
  );
});

/** 折叠跑马灯显示的文本：整段思考**压成一行**后取末尾一段。
 *
 *  刻意不按"最后一行"取：模型换行后新行往往只有一两个字（甚至先来一串空行），
 *  窗口会瞬间缩成空白。压成一行（换行/连续空白折叠为空格）则前后文字连成一句，
 *  换行在预览里不显示，滚动也不断线。 */
function reasoningOneLiner(text: string): string {
  return text
    .replace(/^[ \t]*#{1,6}[ \t]*/gm, '') // 行首标题标记
    .replace(/\*\*/g, '') // 加粗标记
    .replace(/\s+/g, ' ') // 换行 / 连续空白 -> 单个空格
    .trim()
    .slice(-REASONING_MARQUEE_CHARS);
}

const ReasoningRow = memo(function ReasoningRow({
  item,
  projectDir,
  persistKey,
}: {
  item: ActivityItem;
  projectDir: string;
  persistKey: string;
}) {
  const uiLanguage = useUiLanguage();
  const streaming = Boolean(item.streaming);
  // 默认折叠，且不跟随流式自动展开（用户手动开过就一直是开的——包括切页面重挂后）
  const stateKey = `${persistKey}::r-${item.step}-${item.id || ''}`;
  const [open, setOpenRaw] = useState(() => manualOpenState.get(stateKey) === true);
  const setOpen = (value: boolean | ((prev: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof value === 'function' ? value(prev) : value;
      manualOpenState.set(stateKey, next);
      return next;
    });
  };

  const text = item.content || '';
  const label = streaming ? translate("agent:agentGroupView.label_message_textVariant4") : item.durationMs ? translate("agent:agentGroupView.label_message_done", { value: formatDuration(item.durationMs) }) : translate("agent:agentGroupView.label_message_doneVariant2");
  // 只在「折叠 + 正在思考」时滚动最近内容；展开后不再显示
  const marquee = !open && streaming ? reasoningOneLiner(text) : '';

  return (
    <div className={`agent-reasoning${open ? ' is-open' : ''}${streaming ? ' is-streaming' : ''}`}>
      <button
        type="button"
        className="agent-reasoning__header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-reasoning__icon"><Icon name="spark" /></span>
        <span className={`agent-reasoning__label${streaming ? ' is-running' : ''}`}>{label}</span>
        {marquee ? (
          // 装饰性的一行滚动预览：整段思考在展开区里，读屏不必重复
          <span className="agent-reasoning__marquee" aria-hidden="true">
            <span className="agent-reasoning__marquee-text">{marquee}</span>
          </span>
        ) : null}
        <span className="agent-reasoning__caret">›</span>
      </button>
      <AnimatedDisclosure open={open} className="agent-reasoning__collapse">
        {() => (
          <div className="agent-reasoning__collapse-inner">
            <AgentMarkdown
              text={text}
              projectDir={projectDir}
              cursor={streaming}
              className="agent-reasoning__text"
            />
          </div>
        )}
      </AnimatedDisclosure>
    </div>
  );
});

/* ── Compact row (上下文压缩提示) ──
   压缩是后台维护动作，不是用户要读的内容，所以只做一行轻量提示。 */

function CompactRow({ item }: { item: ActivityItem }) {
  const uiLanguage = useUiLanguage();
  const removed = item.removed || 0;
  const before = item.tokensBefore || 0;
  const after = item.tokensAfter || 0;
  // 压缩后的大小是后端在**重建出来的真实历史**上估的：保留的尾部（可能带着很大的工具
  // 结果）都算在内。所以这里显示 before → after，别拿摘要长度当"压缩后的大小"。
  const size = after > 0
    ? translate("agent:agentGroupView.size_message_tokens", { value: before > 0 ? formatTokenCount(before) : '?', value2: formatTokenCount(after) })
    : '';
  return (
    <div className="agent-compact-note" title={translate("agent:agentGroupView.agentCompactNote_title_doneContext")}>
      <span className="agent-compact-note__icon"><Icon name="compress" /></span>
      <span className="agent-compact-note__text">{translate("agent:agentGroupView.agentCompactNote_message_doneContextEntry", { removed: removed, size: size })}</span>
    </div>
  );
}

/* ── Retry row (LLM 请求失败自动重试) ──
   退避等待期间每秒刷新剩余秒数，读起来像「3 秒后重试 · 第 1/3 次」；
   退避结束（llm_retry_end）后定格成「已重试」，不再跳动。
   同一次请求的多次重试共用这一行，只有计数在变；失败原因写在下方一行。 */

const RETRY_CODE_LABELS: Record<string, string> = {
  get NETWORK_ERROR() { return translate("agent:agentGroupView.nETWORKERROR_nETWORKERROR_connectionFailed"); },
  get TIMEOUT() { return translate("agent:agentGroupView.tIMEOUT_tIMEOUT_text"); },
  get RATE_LIMITED() { return translate("agent:agentGroupView.rATELIMITED_rATELIMITED_text"); },
  get PROVIDER_ERROR() { return translate("agent:agentGroupView.pROVIDERERROR_pROVIDERERROR_error"); },
  get STREAM_FAILED() { return translate("agent:agentGroupView.sTREAMFAILED_sTREAMFAILED_text"); },
};

function RetryRow({ item }: { item: ActivityItem }) {
  const uiLanguage = useUiLanguage();
  const active = usePageActive();
  const live = !item.retryDone;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active || !live) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [active, live]);

  const startedAt = item.retryStartedAtMs ?? now;
  const delayMs = item.retryDelayMs ?? 0;
  const remainingSec = Math.max(0, Math.ceil((delayMs - (now - startedAt)) / 1000));
  const attempt = item.attempt ?? 1;
  const maxAttempts = item.maxAttempts ?? 0;
  const attemptText = maxAttempts > 0 ? translate("agent:agentGroupView.attemptText_message_text", { attempt: attempt, maxAttempts: maxAttempts }) : translate("agent:agentGroupView.attemptText_message_textVariant2", { attempt: attempt });
  const cause = item.retryCode ? RETRY_CODE_LABELS[item.retryCode] || translate("agent:agentGroupView.cause_message_failed") : translate("agent:agentGroupView.cause_message_failed");
  const detail = item.retryReason ? `${cause}：${item.retryReason}` : cause;

  return (
    <div className={`agent-retry-note${live ? ' is-live' : ''}`} title={detail}>
      <span className="agent-retry-note__icon"><Icon name="refresh" /></span>
      <div className="agent-retry-note__body">
        <span className="agent-retry-note__text">
          {live ? translate("agent:agentGroupView.agentRetryNoteText_message_secondsRetry", { cause: cause, remainingSec: remainingSec }) : translate("agent:agentGroupView.agentRetryNoteText_message_doneRetry")}
          <span className="agent-retry-note__count"> · {attemptText}</span>
        </span>
        <span className="agent-retry-note__reason">{detail}</span>
      </div>
    </div>
  );
}

/* ── Terminal notices ── */

function ErrorNotice({ group }: { group: Extract<TimelineGroup, { type: 'error' }> }) {
  const uiLanguage = useUiLanguage();
  return (
    <div className="agent-notice agent-notice--error">
      <span className="agent-notice__icon"><Icon name="warning" /></span>
      <div className="agent-notice__body">
        <div className="agent-notice__title">{translate("agent:agentGroupView.agentNoticeBody_message_text")}</div>
        <div className="agent-notice__text">{group.message}</div>
      </div>
    </div>
  );
}

/** 回合停止：一条灰线 + 一行说明就够了，不用整块提示卡片（太重、还抢眼）。
 *  后端文案原样保留（runtime 不动）：只有"用户点停止"那条按界面口径显示成
 *  「用户已停止」，其他原因（如「立即」打断）原样展示。 */
function StoppedNotice({ group }: { group: Extract<TimelineGroup, { type: 'stopped' }> }) {
  useUiLanguage();
  const text = group.reason === '用户停止' ? translate("agent:agentGroupView.text_message_doneStop") : group.reason;
  return (
    <div className="agent-stopped">
      <span className="agent-stopped__text">{text}</span>
    </div>
  );
}
