import { t as translate, useUiLanguage } from "../../i18n";
import { useEffect, useState } from 'react';
import type { AgentSession as AgentSessionMeta } from '../../lib/api';
import { Icon } from '../../components/Icon';
import { shortName } from './storage';

/* ── Session sidebar ──
   一个项目下可以有多个会话；这里负责新建、切换、删除。
   标题由后端生成：新建时是占位「新会话」，首条消息发出后变成这条消息。 */

/** 一个项目下的会话再多也只先渲染这么多条：首次打开的开销与"会话总数"脱钩。
 *  后端返回的列表仍是完整的（分组计数、状态灯都用全量），点「显示其余 N 个」
 *  纯本地展开、不再请求。 */
const SESSION_RENDER_LIMIT = 60;

export function AgentSessionSidebar({
  sessionsByProject,
  projects,
  activeProject,
  activeSessionId,
  collapsed,
  disabled,
  activeRunning,
  unseenLights,
  onCreateBlank,
  onCreateInProject,
  onToggleProject,
  onCloseProjectGroup,
  onSelectSession,
  onDeleteSession,
}: {
  sessionsByProject: Record<string, AgentSessionMeta[]>;
  projects: string[];
  activeProject: string;
  activeSessionId: string;
  collapsed: Record<string, boolean>;
  disabled: boolean;
  /** 当前活动会话自己的回合是否在跑（本地状态，比后端列表快一拍） */
  activeRunning: boolean;
  /** 跑完但还没被点开看过的会话：session_id -> done / failed */
  unseenLights: Record<string, 'done' | 'failed'>;
  onCreateBlank: () => void;
  onCreateInProject: (dir: string) => void;
  onToggleProject: (dir: string) => void;
  /** 关闭一个项目分组（收起来，不删会话；见 AgentPage.handleCloseProjectGroup） */
  onCloseProjectGroup: (dir: string) => void;
  onSelectSession: (dir: string, sid: string) => void;
  onDeleteSession: (dir: string, session: AgentSessionMeta) => void;
}) {
  useUiLanguage();
  // 哪些项目分组已经点开过「显示其余 N 个」（纯本地状态，不涉及请求）
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>({});
  // 相对时间（刚刚 / N分钟前）要定时重算，否则页面静止时数字会一直停着不动
  const [, setTimeTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTimeTick((n) => n + 1), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <aside className="agent-sessions">
      <div className="agent-sessions__head">
        <span className="agent-sessions__title">{translate("agent:agentSessionSidebar.agentSessionsHead_message_session")}</span>
        <button
          type="button"
          className="agent-sessions__new"
          onClick={onCreateBlank}
          disabled={disabled}
          title={disabled ? translate("agent:agentSessionSidebar.agentSessionsNew_title_agentRunningStopWait") : translate("agent:agentSessionSidebar.agentSessionsNew_title_newSessionSelectProject")}
        >
          ＋
        </button>
      </div>
      <div className="agent-sessions__list">
        {projects.length === 0 ? (
          <div className="agent-sessions__empty">{translate("agent:agentSessionSidebar.agentSessionsEmpty_div_emptyProject")}<span>{translate("agent:agentSessionSidebar.agentSessionsEmpty_message_newOpenCountProjectAgent")}</span>
          </div>
        ) : (
          projects.map((dir) => {
            const raw = sessionsByProject[dir];
            const list = raw || [];
            // undefined = 这个项目还没拉过列表（非活动项目是展开时才按需拉的）
            const loaded = raw !== undefined;
            const isCollapsed = collapsed[dir] ?? dir !== activeProject;
            const isGroupActive = dir === activeProject;
            const shortDir = shortName(dir);
            // 该组里还有会话在跑就不给关：收起来就看不到那个蓝灯了，容易忘了它还在跑
            const hasRunning = list.some((s) => s.status === 'running') || (isGroupActive && activeRunning);
            // 只渲染前 N 条；但当前正在看的那个会话无论多老都要在列表里，
            // 否则侧边栏上看不出"你在哪"，它的状态灯也没地方挂。
            const visible = expandedProjects[dir] ? list : list.slice(0, SESSION_RENDER_LIMIT);
            if (!expandedProjects[dir] && activeSessionId && !visible.some((s) => s.session_id === activeSessionId)) {
              const active = list.find((s) => s.session_id === activeSessionId);
              if (active) visible.push(active);
            }
            return (
              <div
                key={dir}
                className={`agent-sessions__group${isCollapsed ? ' is-collapsed' : ''}${isGroupActive ? ' is-active' : ''}`}
              >
                <div className="agent-sessions__group-head">
                  <button
                    type="button"
                    className="agent-sessions__group-toggle"
                    onClick={() => onToggleProject(dir)}
                    title={translate("agent:agentSessionSidebar.agentSessionsGroupToggle_title_text", { dir: dir })}
                  >
                    <span className="agent-sessions__group-icon" aria-hidden>
                      <Icon name={isCollapsed ? 'folder' : 'folder-open'} />
                    </span>
                    <span className="agent-sessions__group-name">{shortDir}</span>
                    <span className="agent-sessions__group-count">
                      {list.length > 0 ? list.length : ''}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="agent-sessions__group-close"
                    onClick={(e) => {
                      e.stopPropagation();
                      onCloseProjectGroup(dir);
                    }}
                    disabled={hasRunning}
                    title={
                      hasRunning
                        ? translate("agent:agentSessionSidebar.agentSessionsGroupClose_title_projectSessionPendingRunningStopDisable")
                        : translate("agent:agentSessionSidebar.agentSessionsGroupClose_title_disableSessionHistoryKeepOpen", { shortDir: shortDir })
                    }
                    aria-label={translate("agent:agentSessionSidebar.agentSessionsGroupClose_ariaLabel_disableProject", { shortDir: shortDir })}
                  >
                    <Icon name="close" />
                  </button>
                  <button
                    type="button"
                    className="agent-sessions__group-new"
                    onClick={(e) => {
                      e.stopPropagation();
                      onCreateInProject(dir);
                    }}
                    // 只有"当前正在跑的那个项目"要拦：在它下面新建会切走当前会话
                    // （并停掉正在跑的回合）。别的项目互不干扰——后端按 (项目, 会话)
                    // 各自独立运行，随时可以在它们下面新建会话、甚至同时各跑一个 Agent。
                    disabled={disabled && dir === activeProject}
                    title={
                      disabled && dir === activeProject
                        ? translate("agent:agentSessionSidebar.agentSessionsGroupNew_title_projectAgentPendingRunningStopWait")
                        : translate("agent:agentSessionSidebar.agentSessionsGroupNew_title_newSession", { shortDir: shortDir })
                    }
                    aria-label={translate("agent:agentSessionSidebar.agentSessionsGroupNew_ariaLabel_newSession", { shortDir: shortDir })}
                  >
                    ＋
                  </button>
                </div>
                <div className="agent-sessions__group-collapse">
                  <div className="agent-sessions__group-collapse-inner">
                    <div className="agent-sessions__group-list">
                      {list.length === 0 ? (
                        <div className="agent-sessions__group-empty">{loaded ? translate("agent:agentSessionSidebar.agentSessionsGroupEmpty_message_emptySession") : translate("common:actions.loading")}</div>
                      ) : (
                        visible.map((s) => {
                          const isRowActive = isGroupActive && s.session_id === activeSessionId;
                          const isRowRunning = s.status === 'running' || (isRowActive && activeRunning);
                          // 灯：工作中蓝灯常亮；跑完但没被你点开看过亮绿灯/橙灯（橙=失败）；
                          // 正看着的那一行不亮灯（点开即熄灭，见 AgentPage 的 unseenLights）
                          const light = isRowRunning
                            ? 'running'
                            : isRowActive
                              ? ''
                              : unseenLights[s.session_id] || '';
                          return (
                            <div
                              key={s.session_id}
                              className={`agent-session-item${isRowActive ? ' is-active' : ''}`}
                            >
                              <button
                                type="button"
                                className="agent-session-item__main"
                                onClick={() => onSelectSession(dir, s.session_id)}
                                title={s.title}
                              >
                                <span className="agent-session-item__title">{s.title}</span>
                                <span className="agent-session-item__time">
                                  {formatSessionTime(s.updated_at)}
                                </span>
                              </button>
                              {light ? (
                                <span
                                  className={`agent-session-item__light is-${light}`}
                                  title={
                                    light === 'running'
                                      ? translate("agent:agentSessionSidebar.agentSessionsGroupList_title_pendingRunning")
                                      : light === 'failed'
                                        ? translate("agent:agentSessionSidebar.agentSessionsGroupList_title_done")
                                        : translate("agent:agentSessionSidebar.agentSessionsGroupList_title_doneVariant2")
                                  }
                                  aria-hidden
                                />
                              ) : null}
                              <button
                                type="button"
                                className="agent-session-item__delete"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  onDeleteSession(dir, s);
                                }}
                                // 正在跑的那个会话不能删（灯亮着）：先停止再删
                                disabled={isRowRunning}
                                title={isRowRunning ? translate("agent:agentSessionSidebar.agentSessionItemDelete_title_pendingRunningStopDelete") : translate("agent:agentSessionSidebar.agentSessionItemDelete_title_deleteSession")}
                                aria-label={translate("agent:agentSessionSidebar.agentSessionItemDelete_ariaLabel_deleteSession", { title: s.title })}
                              >
                                <Icon name="close" />
                              </button>
                            </div>
                          );
                        })
                      )}
                      {list.length > visible.length ? (
                        <button
                          type="button"
                          className="agent-sessions__show-more"
                          onClick={() => setExpandedProjects((prev) => ({ ...prev, [dir]: true }))}
                        >{translate("agent:agentSessionSidebar.agentSessionsGroupList_message_countSession", { value: list.length - visible.length })}</button>
                      ) : null}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

/** 会话时间改成相对时间：刚刚 / N分钟前 / N小时前 / N天前，超过一周退回日期。 */
function formatSessionTime(ts: number): string {
  if (!ts) return '';
  const then = new Date(ts * 1000);
  const time = then.getTime();
  if (Number.isNaN(time)) return '';
  const diffMs = Date.now() - time;
  if (diffMs < 60_000) return translate("agent:agentSessionSidebar.formatSessionTime_message_text"); // 含时钟偏差导致的「未来时间」
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return translate("agent:agentSessionSidebar.formatSessionTime_message_minutes", { minutes: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return translate("agent:agentSessionSidebar.formatSessionTime_message_hours", { hours: hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return translate("agent:agentSessionSidebar.formatSessionTime_message_textVariant2", { days: days });
  const showYear = then.getFullYear() !== new Date().getFullYear();
  const md = `${then.getMonth() + 1}/${then.getDate()}`;
  return showYear ? `${then.getFullYear()}/${md}` : md;
}
