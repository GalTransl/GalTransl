import { t as translate, useUiLanguage } from "../../../i18n";
import { useEffect, useState } from 'react';
import type { SubagentRun, SubagentStep } from '../timeline';
import { asArgs, clipText, formatDuration, toolMeta } from '../toolMeta';

/* ── Tool row (disclosure, not a boxed card) ── */

/** 一批子代理：挂在发起它们的那次 run_subagents 调用下面，一行一个。

    参考 PI-Desktop 的 topology（一条主线 + 若干子行），这里只做一层——子代理没有子代理。
    每行能展开看它自己的工具调用与报告：跑着的时候默认展开（要看它在干什么），跑完自动收起
    （报告在行摘要的下一层，点开就能读）；用户手动点过就听用户的。 */
export function SubagentList({ runs }: { runs: SubagentRun[] }) {
  const uiLanguage = useUiLanguage();
  const running = runs.filter((run) => run.status === 'running').length;
  const done = runs.filter((run) => run.status === 'done').length;
  const failed = runs.filter((run) => run.status === 'failed').length;
  const stopped = runs.length - running - done - failed;
  // 有在跑的就报"几个在跑"；全停了就按结局分账——中止/失败不算"已完成"
  let meta: string;
  if (running > 0) {
    meta = translate("agent:subagentList.subagentList_message_count", { running: running, count: runs.length });
  } else if (stopped > 0 || failed > 0) {
    const parts = [done > 0 ? translate("agent:subagentList.parts_message_countComplete", { done: done }) : '', failed > 0 ? translate("agent:subagentList.parts_message_countFailed", { failed: failed }) : '', stopped > 0 ? translate("agent:subagentList.parts_message_count", { stopped: stopped }) : ''];
    meta = parts.filter(Boolean).join('、');
  } else {
    meta = translate("agent:subagentList.subagentList_message_countDoneComplete", { count: runs.length });
  }
  return (
    <div className="agent-subagents">
      <div className="agent-subagents__head">
        <span className="agent-subagents__title">{translate("agent:subagentList.agentSubagentsHead_message_proxy")}</span>
        <span className="agent-subagents__meta">{meta}</span>
      </div>
      {runs.map((run) => (
        <SubagentRow key={run.id} run={run} />
      ))}
    </div>
  );
}

function SubagentRow({ run }: { run: SubagentRun }) {
  const uiLanguage = useUiLanguage();
  // **默认折叠**：一次派 16 个时是一行一个子代理，全铺开会把父行撑得很长；要看细节点开。
  // 折叠态不丢信息——"最新动作"就挂在行上（见 subagentLatest），在干什么一眼能扫到。
  const [open, setOpen] = useState(false);
  // 跑着的时候没有 duration_ms（那是完成时给的）：按起始时间 + 本地 tick 算。
  // 起始时间来自后端的 started_at，所以切页/刷新重建后不会归零；tick 保证没有新
  // 事件时秒数也在走（否则一行"5.0s"会一直不动，看着像卡住）。
  const running = run.status === 'running';
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [running]);
  const state = subagentState(run);
  const durationMs = run.durationMs ?? (run.startedAt ? now - run.startedAt : 0);
  const latest = subagentLatest(run);

  return (
    <div className={`agent-subagent is-${run.status}`}>
      <button
        type="button"
        className="agent-subagent__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="agent-subagent__caret" aria-hidden>›</span>
        <span className="agent-subagent__label">{run.label}</span>
        <span className="agent-subagent__file" title={run.file}>
          {run.file}
          {run.indexes ? ` · ${run.indexes}` : ''}
        </span>
        {latest ? (
          <span
            className={`agent-subagent__latest${latest.tone === 'error' ? ' is-error' : ''}`}
            title={latest.full}
          >
            {latest.short}
          </span>
        ) : null}
        {run.toolCalls ? <span className="agent-subagent__badge">{translate("agent:subagentList.agentSubagentHead_message_text", { toolCalls: run.toolCalls })}</span> : null}
        {run.proofreadComment ? <span className="agent-subagent__badge is-doubt">{translate("agent:subagentList.agentSubagentHead_message_textVariant2", { proofreadComment: run.proofreadComment })}</span> : null}
        <span className={`agent-subagent__state is-${state.tone}`}>{state.label}</span>
        {run.retry ? (
          <span
            className="agent-subagent__badge is-retry"
            title={
              run.retry.reason
                ? translate("agent:subagentList.agentSubagentBadgeIsRetry_title_text", { value: run.retry.code || translate("agent:subagentList.interpolation_fallback_failed"), reason: run.retry.reason })
                : translate("agent:subagentList.agentSubagentBadgeIsRetry_title_failedPendingAutoRetry")
            }
          >{translate("agent:subagentList.agentSubagentHead_message_retry", { attempt: run.retry.attempt, value: run.retry.maxAttempts || '…' })}</span>
        ) : null}
        {durationMs > 0 ? <span className="agent-subagent__time">{formatDuration(durationMs)}</span> : null}
      </button>
      {open ? (
        <div className="agent-subagent__body">
          {run.brief ? <div className="agent-subagent__brief">{translate("agent:subagentList.agentSubagentBody_message_text", { brief: run.brief })}</div> : null}
          {run.steps.map((step, i) =>
            step.kind === 'text' ? (
              <div key={`t-${i}`} className="agent-subagent__text">{step.text}</div>
            ) : (
              <SubagentStepRow key={step.id || `s-${i}`} step={step} />
            ),
          )}
          {run.report ? (
            <div className="agent-subagent__report">
              <span className="agent-subagent__report-label">{translate("agent:subagentList.agentSubagentReport_message_text")}</span>
              <span className="agent-subagent__report-text">{run.report}</span>
            </div>
          ) : null}
          {run.error ? <div className="agent-subagent__error">{run.error}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

/** 子代理写下的校对批注：从 patch_transl_cache 的入参里读（它写的就是 proofread_comment；
 *  历史会话里是旧名 doub_content，一并认）。 */
function stepDoubts(args: unknown): { index: number; text: string }[] {
  const patches = (args as Record<string, unknown> | undefined)?.patches;
  if (!Array.isArray(patches)) return [];
  return patches.flatMap((patch) => {
    const item = patch as Record<string, unknown> | undefined;
    const raw = item?.proofread_comment ?? item?.doub_content;
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (!text) return [];
    const index = Number(item?.index);
    return [{ index: Number.isFinite(index) ? index : -1, text }];
  });
}

/** 折叠态显示的那句"最新动作"：最后一步是什么（说了什么 / 调了什么工具）。

    读类工具给参数摘要（读哪个文件、哪几行），写意见那步给条数；失败给错误首句。
    跑完且一步都没有（刷新过：逐步活动是瞬态的、不落盘）就退回报告首行，至少还剩一句总结。 */
function subagentLatest(run: SubagentRun): { short: string; full: string; tone?: 'error' } | null {
  const step = run.steps[run.steps.length - 1];
  if (step?.kind === 'text') {
    const text = step.text.trim().replace(/\s+/g, ' ');
    return text ? { short: clipText(text, 68), full: text } : null;
  }
  if (step?.kind === 'tool') {
    if (step.ok === false) {
      const error = (step.error || translate("common:actions.failed")).trim();
      return { short: translate("agent:subagentList.short_short_failed", { value: clipText(error, 40) }), full: error, tone: 'error' };
    }
    const doubts = stepDoubts(step.args);
    if (step.name === 'patch_transl_cache' && doubts.length) {
      return {
        short: translate("agent:subagentList.short_short_entry", { count: doubts.length }),
        full: doubts.map((doubt) => `#${doubt.index} ${doubt.text}`).join('\n'),
      };
    }
    const meta = toolMeta(step.name);
    const detail = meta.summary(asArgs(step.args));
    return {
      short: detail ? `${meta.action} · ${detail}` : meta.action,
      full: detail ? `${meta.action}：${detail}` : meta.action,
    };
  }
  const report = (run.report || '').trim().replace(/\s+/g, ' ');
  return report ? { short: clipText(report, 68), full: report } : null;
}

/** 子代理的一步工具调用。写意见那一步特殊处理：把每条意见都摊出来——那才是用户要看的产出，
    只显示"修改译文 · 3 条"等于让他自己去翻缓存文件。 */
function SubagentStepRow({ step }: { step: Extract<SubagentStep, { kind: 'tool' }> }) {
  useUiLanguage();
  const doubts = stepDoubts(step.args);
  if (step.name === 'patch_transl_cache' && doubts.length) {
    return (
      <div className="agent-subagent__step is-doubt">
        <span className="agent-subagent__step-name">{translate("agent:subagentList.agentSubagentStepIsDoubt_message_text")}</span>
        <div className="agent-subagent__doubts">
          {doubts.map((doubt) => (
            <div key={doubt.index} className="agent-subagent__doubt">
              <span className="agent-subagent__doubt-index">#{doubt.index}</span>
              <span className="agent-subagent__doubt-text">{doubt.text}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className={`agent-subagent__step${step.ok === false ? ' is-error' : ''}`}>
      <span className="agent-subagent__step-name">{toolMeta(step.name).action}</span>
      <span className="agent-subagent__step-detail">
        {step.ok === false
          ? step.error || translate("common:actions.failed")
          : toolMeta(step.name).summary(asArgs(step.args)) || translate("common:actions.completed")}
      </span>
    </div>
  );
}

/** 子代理的状态标签与配色。 */
function subagentState(run: SubagentRun): {
  label: string;
  tone: 'running' | 'done' | 'error' | 'muted';
} {
  if (run.status === 'running') return { label: translate("agent:subagentList.label_label_text"), tone: 'running' };
  if (run.status === 'done') return { label: translate("common:actions.completed"), tone: 'done' };
  if (run.status === 'failed') return { label: translate("common:actions.failed"), tone: 'error' };
  if (run.status === 'stopped') return { label: translate("agent:subagentList.label_label_doneStop"), tone: 'muted' };
  return { label: translate("agent:subagentList.label_label_textVariant2"), tone: 'muted' };
}
