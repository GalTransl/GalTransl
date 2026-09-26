import { useState } from 'react';
import {
  normalizePermissionMode,
  PERMISSION_MODE_LABELS,
  type PermissionDecision,
} from '../../../lib/permissionMode';
import { Icon } from '../../../components/Icon';
import { ChangeListCard, extractChangeList } from './ToolRow';
import type { ActivityItem } from '../timeline';
import { toolMeta } from '../toolMeta';

/* ── 权限确认卡片 ──
   写操作执行前，后端会挂起并推一条 permission_request；卡片就摆在那次工具调用下面
   （与 ask_user 同一套位置）。三个动作对应后端的三种答复：允许一次 / 本会话允许
   （只对这个工具、只在这个会话）/ 拒绝。**没有倒计时、也不会自动拒绝**：后端不设超时，
   不点就一直等着（刷新、切走再回来卡片都还在）；要收场就点三个按钮之一，或者点停止
   让整个回合收尾。拒绝会让模型收到一条"用户拒绝权限"的工具错误（可附你填的原因），
   它据此换策略——而不是把"没执行"当成"执行成功"。

   卡上还会带上「将要变更」：后端在挂起前就用同一套规则把 before→after 算好了
   （见后端的 _preview_tool_changes，只读、不落盘），写类工具（改译文数据、改项目配置、
   改问题过滤、写项目规范）因此不必先展开工具行的原始参数才敢点允许——你看到的就是这次
   要写下去的东西。算不出来（整文件删缓存、启动翻译、派子代理这些没有可比对的 diff）
   就没有这块，卡片只给摘要。 */

export function PermissionCard({
  item,
  submitting,
  error,
  onDecide,
}: {
  item: ActivityItem;
  submitting: boolean;
  error: string | null;
  onDecide: (decision: PermissionDecision, reason?: string) => void;
}) {
  const perm = item.permission;
  // 拒绝原因（可选，输入框里那份）：只有点「拒绝」才送出去，会随那条工具结果一起给模型看。
  // 别和下面那个 `reason`（模型填在入参里的"为什么做这件事"）搞混，那个是只读展示用的。
  const [denyReason, setDenyReason] = useState('');
  if (!perm) return null;
  const meta = toolMeta(perm.name || item.name || '');
  const args = perm.arguments;
  const summary = meta.summary(args);
  const reason = typeof args?.reason === 'string' ? args.reason.trim() : '';
  const toolLabel = perm.label || meta.action;
  const editable = perm.risk === 'edit';
  // 徽标只留最要紧的几个字（会改什么），完整解释挪进 tooltip——照 PI-Desktop 那张卡：
  // 标题行一行说完，正文只留"允许什么 + 为什么"，说明性长句不再铺在卡面上。
  // 派子代理单独一档说法：它既不改设置、也不是改译文，但「允许编辑」档照样要问它
  // （见后端 PERMISSION_TOOL_RISK），归进"改设置 / 启动任务"会让人看不懂为什么要问。
  const riskKind: 'edit' | 'delegate' | 'high' =
    perm.name === 'run_subagents' ? 'delegate' : editable ? 'edit' : 'high';
  const riskLabel = { edit: '改译文数据', delegate: '派子代理', high: '改设置 / 启动任务' }[riskKind];
  const riskHint = {
    edit: '改动译文数据（缓存 / 字典 / 人名表）',
    delegate:
      '派一批子代理并行跑：每个都会调模型（校对子代理还会往缓存里写意见，原文探索会通读原文、很费 token）',
    high: '改动项目设置 / 规范，或启动翻译任务',
  }[riskKind];
  // 正文第二行的细节：参数摘要 + 当前档位（为什么现在要问）。都是短标签，逗号分不开的
  // 那种长句就省了——用户要的是"这次要动什么"，不是复述一遍权限模型。
  const detail = [PERMISSION_MODE_LABELS[normalizePermissionMode(perm.mode)], summary]
    .filter(Boolean)
    .join(' · ');
  // 「将要变更」（后端只读算出来的 diff）：认不出来就是没有——卡上不给空壳。
  const preview = extractChangeList(perm.preview);

  return (
    <section className="agent-perm" aria-label={`权限请求：${toolLabel}`}>
      <header className="agent-perm__head">
        <span className="agent-perm__icon"><Icon name="shield" /></span>
        <span className="agent-perm__title" role="status" aria-live="polite">{toolLabel}</span>
        <span
          className={`agent-perm__risk${editable ? ' is-edit' : ''}`}
          title={riskHint}
        >
          {riskLabel}
        </span>
      </header>
      <p className="agent-perm__lead">允许「{toolLabel}」运行吗？</p>
      <p className="agent-perm__meta">{detail}</p>
      {reason ? <p className="agent-perm__reason">原因：{reason}</p> : null}
      {/* 将要变更：摆在这一屏里而不是藏在工具行的展开里——用户要点的就是这个 */}
      {preview ? <ChangeListCard data={preview} title="将要变更" /> : null}
      {error ? <div className="agent-perm__error">{error}</div> : null}
      <div className="agent-perm__foot">
        <button
          type="button"
          className="agent-perm__btn is-primary"
          onClick={() => onDecide('allow-once')}
          disabled={submitting}
          title="只批准这一次调用"
        >
          允许一次
        </button>
        <button
          type="button"
          className="agent-perm__btn"
          onClick={() => onDecide('allow-session')}
          disabled={submitting}
          title={`本会话内不再询问「${toolLabel}」，会话结束即失效`}
        >
          本会话允许
        </button>
        <button
          type="button"
          className="agent-perm__btn"
          onClick={() => onDecide('deny', denyReason.trim())}
          disabled={submitting}
          title="这次调用不执行，Agent 会收到「用户拒绝」并换策略"
        >
          拒绝
        </button>
        {/* 拒绝原因（可选）：「不要」和「不要，因为 X」对模型是两回事——后者能让它
            直接换对方向，省掉一轮来回。留空就是单纯拒绝；填了按回车等于点「拒绝」。 */}
        <input
          type="text"
          className="agent-perm__reason-input"
          value={denyReason}
          onChange={(e) => setDenyReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || submitting) return;
            e.preventDefault();
            onDecide('deny', denyReason.trim());
          }}
          placeholder="拒绝原因（可选）"
          aria-label="拒绝原因（可选）"
          title="填了会在点「拒绝」时一起送给 Agent（显示在那次调用的结果里）"
          maxLength={500}
          disabled={submitting}
        />
      </div>
    </section>
  );
}
