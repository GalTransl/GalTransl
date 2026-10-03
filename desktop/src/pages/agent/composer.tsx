import { t as translate, useUiLanguage } from "../../i18n";
import type { AgentContextUsage } from '../../lib/api';
import { formatTokenCount } from './toolMeta';

/** 发送/插话按钮的图标：上箭头。原来是一个箭头字符，字重/基线随字体走，
 *  改成 SVG 后与页面其它图标（开文件夹、上下文环）口径一致。 */
export function SendIcon() {
  const uiLanguage = useUiLanguage();
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 19V5m0 0-5.5 5.5M12 5l5.5 5.5"
      />
    </svg>
  );
}

/** 停止按钮的图标：实心圆角方块（原来是 CSS 画的方块）。 */
export function StopIcon() {
  const uiLanguage = useUiLanguage();
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor" />
    </svg>
  );
}

/** 已用上下文/上下文窗口的环形指示器（悬停显示百分比与具体 token 数）。
 *  达到压缩触发线（80%）后转为警示色。 */
export function ContextMeter({ usage }: { usage: AgentContextUsage }) {
  const uiLanguage = useUiLanguage();
  const window = usage.window_tokens > 0 ? usage.window_tokens : 0;
  const used = Math.max(0, usage.used_tokens);
  const ratio = window > 0 ? Math.min(1, used / window) : 0;
  const percent = ratio * 100;
  const r = 7;
  const circumference = 2 * Math.PI * r;
  const detail = translate("agent:composer.detail_message_contextDone", { value: percent.toFixed(1), value2: formatTokenCount(used), value3: formatTokenCount(window) });
  return (
    <span
      className={`agent-context-meter${percent >= 80 ? ' is-warn' : ''}`}
      title={detail}
      aria-label={detail}
      role="img"
    >
      <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
        <circle className="agent-context-meter__track" cx="10" cy="10" r={r} fill="none" strokeWidth="2.5" />
        <circle
          className="agent-context-meter__value"
          cx="10"
          cy="10"
          r={r}
          fill="none"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={`${circumference * ratio} ${circumference}`}
          transform="rotate(-90 10 10)"
        />
      </svg>
    </span>
  );
}

/* ── 顶部空态的推荐提示词 ──
   不是操作按钮：点一下只是把这句话填进输入框（不直接发送），用户还能补两句再发。
   条目写成"能直接当第一条消息发出"的口气，所以文案本身就是提示词。 */
export const AGENT_PROMPT_SUGGESTIONS = [
  '做一下翻译前准备',
  '通过子agent探索全文补齐字典',
  '做一下译后流程',
  '修一下问题',
];

export function StatusPill({ status, running }: { status: string; running: boolean }) {
  useUiLanguage();
  const tone = running ? 'running' : status;
  const label = running
    ? translate("common:actions.running")
    : status === 'awaiting_input' || status === 'done'
      ? translate("agent:composer.label_message_wait")
      : status === 'stopped'
        ? translate("agent:composer.label_message_doneStop")
        : status === 'failed'
          ? translate("common:actions.error")
          : translate("common:actions.idle");
  return (
    <span className={`agent-status-pill agent-status-pill--${tone}`}>{label}</span>
  );
}
