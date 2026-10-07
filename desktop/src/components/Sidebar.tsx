import { message as uiMessage, resolveMessage, t as translate, useUiLanguage, type LocalizedText } from "../i18n";
import { useCallback, useEffect, useRef, useState, type TransitionEvent } from 'react';
import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import {
  BACKEND_PROFILES_CHANGE_EVENT,
  encodeProjectDir,
  decodeProjectDir,
  fetchJob,
  fetchProjectProblems,
  fetchProjectRuntime,
  getBackendProfileNames,
  isProjectConfigDirty,
  listAgentSessions,
  PROJECT_CONFIG_DIRTY_CHANGE_EVENT,
  setProjectConfigDirty,
  submitJob,
  type ProjectRuntimeResponse,
} from '../lib/api';
import { loadLastProjectTab } from '../lib/projectTabMemory';
import { basenamePath, joinPath } from '../lib/paths';
import { Icon, type IconName } from './Icon';
import { InlineFeedback } from './page-state/InlineFeedback';
import logoUrl from '../assets/logo.png';
import { ProjectFolderPopover } from './ProjectFolderPopover';
import { readHistory } from '../pages/agent/storage';

const CONFIG_FILE_KEY = 'galtransl-config-file';
const LAST_ACTIVE_PROJECT_KEY = 'galtransl-last-active-project';
const OUTPUT_FOLDER_NAME = 'gt_output';

type RebuildToast = {
  id: number;
  tone: 'error' | 'warning' | 'success';
  title: LocalizedText;
  description: LocalizedText;
};

function loadConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
  }
}

function loadLastActiveProject(): string | null {
  try {
    return localStorage.getItem(LAST_ACTIVE_PROJECT_KEY);
  } catch {
    return null;
  }
}

const PROJECT_TABS: Array<{ path: string; label: string; icon: IconName }> = [
  { path: 'translate', get label() { return translate("common:sidebar.label_label_startTranslation"); }, icon: 'globe' },
  { path: 'cache', get label() { return translate("common:sidebar.label_label_text"); }, icon: 'database' },
  { path: 'dictionary', get label() { return translate("common:sidebar.label_label_projectDictionary"); }, icon: 'book' },
  { path: 'names', get label() { return translate("common:sidebar.label_label_nameTableTranslation"); }, icon: 'user' },
  { path: 'config', get label() { return translate("common:sidebar.label_label_configEdit"); }, icon: 'settings' },
];

/** 翻译或 Agent 正在跑任务时的呼吸蓝点（与 Agent 页「运行中」指示同款）。
 *  child = 展开态子项行（跟在文字后面靠右）；rail = 收起态只剩图标的导航项。 */
function RunningDot({ variant, agent = false }: { variant: 'child' | 'rail'; agent?: boolean }) {
  const uiLanguage = useUiLanguage();
  return (
    <span
      className={variant === 'child' ? 'sidebar__project-child-running-dot' : 'sidebar__nav-running-dot'}
      title={translate(agent ? "common:actions.running" : "common:sidebar.runningDot_title_pendingTranslation")}
      aria-label={translate(agent ? "common:actions.running" : "common:sidebar.runningDot_ariaLabel_pendingTranslation")}
    />
  );
}

type SidebarProps = {
  openProjects: string[];
  wizardOpen: boolean;
  wizardProjectName: string;
  onCloseProject: (projectDir: string) => void;
  onCloseOtherProjects: (projectDir: string) => void;
  onCloseAllProjects: () => void;
};

function buildInitialExpandedProjects(openProjects: string[], pathname: string): Record<string, boolean> {
  if (openProjects.length === 0) {
    return {};
  }

  let expandedProject: string | null = null;
  const match = pathname.match(/^\/project\/([^/]+)/);
  if (match) {
    try {
      const projectDir = decodeProjectDir(match[1]);
      if (openProjects.includes(projectDir)) {
        expandedProject = projectDir;
      }
    } catch {
      expandedProject = null;
    }
  }

  const lastActiveProject = loadLastActiveProject();
  const rememberedProject = lastActiveProject && openProjects.includes(lastActiveProject)
    ? lastActiveProject
    : null;

  const target = expandedProject ?? rememberedProject ?? openProjects[0];
  const result: Record<string, boolean> = {};
  for (const projectDir of openProjects) {
    result[projectDir] = projectDir === target;
  }
  return result;
}

export function Sidebar({ openProjects, wizardOpen, wizardProjectName, onCloseProject, onCloseOtherProjects, onCloseAllProjects }: SidebarProps) {
  useUiLanguage();
  const navigate = useNavigate();
  const location = useLocation();
  const [expanded, setExpanded] = useState(true);
  // Track which projects are expanded in the sidebar (keyed by projectDir)
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>(() =>
    buildInitialExpandedProjects(openProjects, location.pathname)
  );
  // Keep submenu content mounted long enough for close animations to complete
  const [renderedProjectChildren, setRenderedProjectChildren] = useState<Record<string, boolean>>({});
  // Track the visual open/closed state separately so expand animations can start from collapsed
  const [visibleProjectChildren, setVisibleProjectChildren] = useState<Record<string, boolean>>({});
  // Track which project is showing the close confirmation bubble
  const [confirmCloseDir, setConfirmCloseDir] = useState<string | null>(null);
  // Track which projects are currently rebuilding output
  const [rebuildingDirs, setRebuildingDirs] = useState<Record<string, boolean>>({});
  // Track which projects have active translation jobs (running or pending)
  const [translatingDirs, setTranslatingDirs] = useState<Record<string, boolean>>({});
  const [agentRunning, setAgentRunning] = useState(false);
  const runningAgentProjectsRef = useRef(new Map<string, boolean>());
  const [rebuildToasts, setRebuildToasts] = useState<RebuildToast[]>([]);
  const [hasBackendProfiles, setHasBackendProfiles] = useState(() => getBackendProfileNames().length > 0);
  const [dirtyConfigProjects, setDirtyConfigProjects] = useState<Record<string, boolean>>({});
  // Right-click context menu state
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; projectDir: string } | null>(null);
  const prevOpenProjectsRef = useRef<string[]>(openProjects);
  const confirmBubbleRef = useRef<HTMLDivElement>(null);
  const contextMenuRef = useRef<HTMLDivElement>(null);
  const expandAnimationFrameRef = useRef<Record<string, number>>({});
  const rebuildToastIdRef = useRef(0);

  const pushRebuildToast = useCallback((toast: Omit<RebuildToast, 'id'>) => {
    const id = ++rebuildToastIdRef.current;
    setRebuildToasts((prev) => [...prev, { ...toast, id }]);
  }, []);

  const dismissRebuildToast = useCallback((id: number) => {
    setRebuildToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  useEffect(() => {
    const updateBackendProfileNotice = () => {
      setHasBackendProfiles(getBackendProfileNames().length > 0);
    };

    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, updateBackendProfileNotice);
    return () => window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, updateBackendProfileNotice);
  }, []);

  useEffect(() => {
    setDirtyConfigProjects(() => {
      const next: Record<string, boolean> = {};
      for (const projectDir of openProjects) {
        next[projectDir] = isProjectConfigDirty(projectDir);
      }
      return next;
    });
  }, [openProjects]);

  useEffect(() => {
    const handleProjectConfigDirtyChange = (event: Event) => {
      const detail = (event as CustomEvent<{ projectDir?: string; dirty?: boolean }>).detail;
      if (!detail?.projectDir || typeof detail.dirty !== 'boolean') return;
      setDirtyConfigProjects((prev) => ({ ...prev, [detail.projectDir as string]: detail.dirty as boolean }));
    };

    window.addEventListener(PROJECT_CONFIG_DIRTY_CHANGE_EVENT, handleProjectConfigDirtyChange);
    return () => window.removeEventListener(PROJECT_CONFIG_DIRTY_CHANGE_EVENT, handleProjectConfigDirtyChange);
  }, []);

  // When a new project is opened, collapse all others and expand the new one
  useEffect(() => {
    const prev = prevOpenProjectsRef.current;
    for (const projectDir of prev) {
      if (!openProjects.includes(projectDir)) {
        setProjectConfigDirty(projectDir, false);
      }
    }
    // Detect newly added project
    if (openProjects.length > prev.length) {
      const newProject = openProjects.find((p) => !prev.includes(p));
      if (newProject) {
        setExpandedProjects(() => {
          const next: Record<string, boolean> = {};
          for (const key of openProjects) {
            next[key] = key === newProject;
          }
          return next;
        });
      }
    }
    prevOpenProjectsRef.current = openProjects;
  }, [openProjects]);

  // When navigating to a project page, auto-expand that project's menu (accordion)
  useEffect(() => {
    const match = location.pathname.match(/^\/project\/([^/]+)/);
    if (match) {
      try {
        const projectDir = decodeProjectDir(match[1]);
        if (openProjects.includes(projectDir)) {
          setExpandedProjects((prev) => {
            // Already expanded? No change needed
            if (prev[projectDir] === true) return prev;
            // Expand this project, collapse all others
            const next: Record<string, boolean> = {};
            for (const key of openProjects) {
              next[key] = key === projectDir;
            }
            return next;
          });
        }
      } catch {
        // Invalid project ID in URL, ignore
      }
    }
  }, [location.pathname, openProjects]);

  // Close confirmation bubble when clicking outside
  useEffect(() => {
    if (confirmCloseDir === null) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (confirmBubbleRef.current && !confirmBubbleRef.current.contains(e.target as Node)) {
        setConfirmCloseDir(null);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [confirmCloseDir]);

  // Close context menu when clicking outside
  useEffect(() => {
    if (!contextMenu) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target as Node)) {
        setContextMenu(null);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setContextMenu(null);
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [contextMenu]);

  const handleProjectContextMenu = useCallback((e: React.MouseEvent, projectDir: string) => {
    e.preventDefault();
    e.stopPropagation();
    // Clamp menu position to viewport
    const menuWidth = 180;
    const menuHeight = 120;
    const x = Math.min(e.clientX, window.innerWidth - menuWidth - 8);
    const y = Math.min(e.clientY, window.innerHeight - menuHeight - 8);
    setContextMenu({ x, y, projectDir });
    setConfirmCloseDir(null);
  }, []);

  const toggleExpanded = useCallback(() => {
    setExpanded((prev) => !prev);
  }, []);

  // Use compact project headers when many projects are open
  const compactProjectHeaders = openProjects.length + Number(wizardOpen) > 6;

  useEffect(() => {
    setRenderedProjectChildren((prev) => {
      let changed = false;
      const next: Record<string, boolean> = {};

      for (const projectDir of openProjects) {
        const isExpanded = projectDir in expandedProjects ? expandedProjects[projectDir] : false;
        const shouldRender = isExpanded || prev[projectDir] === true;
        next[projectDir] = shouldRender;
        if (prev[projectDir] !== shouldRender) {
          changed = true;
        }
      }

      if (!changed && Object.keys(prev).length === openProjects.length) {
        return prev;
      }

      return next;
    });
  }, [expandedProjects, openProjects]);

  // Keep the Agent indicator updated even when its page is unmounted.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const runningByProject = runningAgentProjectsRef.current;
    const pollAgentStatus = async () => {
      const projects = new Set([...openProjects, ...readHistory().map((entry) => entry.projectDir)]);
      // Keep watching a running project even if it is removed from the navigation.
      for (const [dir, running] of runningByProject) {
        if (running) projects.add(dir);
      }
      await Promise.all(Array.from(projects, async (dir) => {
        try {
          const sessions = await listAgentSessions(dir);
          if (cancelled) return;
          runningByProject.set(dir, sessions.some((session) => session.status === 'running'));
        } catch {
          // Preserve the last known state during temporary connection failures.
        }
      }));
      if (cancelled) return;
      setAgentRunning(Array.from(runningByProject.values()).some(Boolean));
      timer = window.setTimeout(() => void pollAgentStatus(), 3000);
    };
    void pollAgentStatus();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [openProjects]);

  // Poll project runtime status to detect active translation jobs
  useEffect(() => {
    const pollTranslationStatus = async () => {
      const statusMap: Record<string, boolean> = {};
      await Promise.all(
        openProjects.map(async (projectDir) => {
          try {
            const projectId = encodeProjectDir(projectDir);
            const runtime: ProjectRuntimeResponse = await fetchProjectRuntime(projectId);
            const isTranslating = runtime.job !== null &&
              (runtime.job.status === 'pending' || runtime.job.status === 'running');
            statusMap[projectDir] = isTranslating;
          } catch {
            statusMap[projectDir] = false;
          }
        })
      );
      setTranslatingDirs(statusMap);
    };

    void pollTranslationStatus();
    const poller = window.setInterval(() => {
      void pollTranslationStatus();
    }, 3000);
    return () => window.clearInterval(poller);
  }, [openProjects]);

  useEffect(() => {
    for (const frameId of Object.values(expandAnimationFrameRef.current)) {
      window.cancelAnimationFrame(frameId);
    }
    expandAnimationFrameRef.current = {};

    setVisibleProjectChildren((prev) => {
      const next: Record<string, boolean> = {};
      let changed = false;

      for (const projectDir of openProjects) {
        const isRendered = renderedProjectChildren[projectDir] ?? false;
        const isExpanded = projectDir in expandedProjects ? expandedProjects[projectDir] : false;
        const wasVisible = prev[projectDir] ?? false;

        if (!isRendered) {
          next[projectDir] = false;
          if (wasVisible) {
            changed = true;
          }
          continue;
        }

        if (!isExpanded) {
          next[projectDir] = false;
          if (wasVisible) {
            changed = true;
          }
          continue;
        }

        if (wasVisible) {
          next[projectDir] = true;
          continue;
        }

        next[projectDir] = false;
        expandAnimationFrameRef.current[projectDir] = window.requestAnimationFrame(() => {
          setVisibleProjectChildren((current) => {
            if (current[projectDir]) {
              return current;
            }

            return {
              ...current,
              [projectDir]: true,
            };
          });
          delete expandAnimationFrameRef.current[projectDir];
        });

        if (wasVisible !== next[projectDir]) {
          changed = true;
        }
      }

      if (!changed && Object.keys(prev).length === openProjects.length) {
        return prev;
      }

      return next;
    });

    return () => {
      for (const frameId of Object.values(expandAnimationFrameRef.current)) {
        window.cancelAnimationFrame(frameId);
      }
      expandAnimationFrameRef.current = {};
    };
  }, [expandedProjects, openProjects, renderedProjectChildren]);

  const toggleProjectExpanded = useCallback((projectDir: string) => {
    setExpandedProjects((prev) => {
      const isCurrentlyExpanded = prev[projectDir] ?? false;
      if (isCurrentlyExpanded) {
        // Collapsing: just collapse this one
        return {
          ...prev,
          [projectDir]: false,
        };
      } else {
        // Expanding: collapse all others, expand this one (accordion)
        const next: Record<string, boolean> = {};
        for (const key of openProjects) {
          next[key] = key === projectDir;
        }
        // Navigate to the project's last visited page
        const projectId = encodeProjectDir(projectDir);
        navigate(`/project/${projectId}/${loadLastProjectTab(projectDir)}`);
        return next;
      }
    });
  }, [openProjects, navigate]);

  const handleRequestClose = useCallback((projectDir: string) => {
    setConfirmCloseDir(projectDir);
  }, []);

  const handleConfirmClose = useCallback((projectDir: string) => {
    setConfirmCloseDir(null);
    onCloseProject(projectDir);
  }, [onCloseProject]);

  const handleCancelClose = useCallback(() => {
    setConfirmCloseDir(null);
  }, []);

  const handleRebuildOutput = useCallback(async (projectDir: string) => {
    const configFileName = loadConfigFileName(projectDir);
    setRebuildingDirs((prev) => ({ ...prev, [projectDir]: true }));
    try {
      const job = await submitJob({
        project_dir: projectDir,
        config_file_name: configFileName,
        translator: 'rebuilda',
      });
      // Poll until job completes
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const status = await fetchJob(job.job_id);
        if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
          if (status.success) {
            // Rebuild refreshes the cache's derived problem fields. Check the
            // refreshed list so stale translation failures are called out.
            let translationFailureCount = 0;
            try {
              const problems = await fetchProjectProblems(encodeProjectDir(projectDir), configFileName);
              translationFailureCount = problems.problems.filter((entry) => String(entry.problem || '').includes('翻译失败')).length;
            } catch {
              // A problem-list refresh should not turn a successful build into
              // a failure toast; the output can still be opened normally.
            }

            if (translationFailureCount > 0) {
              pushRebuildToast({
                tone: 'warning',
                title: uiMessage("common:sidebar.title_title_translationFailedProblem"),
                description: uiMessage("common:sidebar.description_description_problemListEntryTranslationFailedProblemNot", { translationFailureCount: translationFailureCount }),
              });
            }

            const outputDir = joinPath(projectDir, OUTPUT_FOLDER_NAME);
            pushRebuildToast({
              tone: 'success',
              title: uiMessage("common:sidebar.title_title_text"),
              description: uiMessage("common:sidebar.description_description_fileDone"),
            });
            try {
              await invoke('open_folder', { path: outputDir });
            } catch (err) {
              pushRebuildToast({
                tone: 'error',
                title: uiMessage("common:sidebar.title_title_openFileFailed"),
                description: err instanceof Error ? err.message : String(err),
              });
            }
          } else {
            pushRebuildToast({
              tone: 'error',
              title: uiMessage("common:sidebar.title_title_failed"),
              description: uiMessage("common:sidebar.description_description_fileFailed", { value: status.error || uiMessage("common:actions.unknownError") }),
            });
          }
          return;
        }
      }
      pushRebuildToast({
        tone: 'error',
        title: uiMessage("common:sidebar.title_title_failed"),
        description: uiMessage("common:sidebar.description_description_file"),
      });
    } catch (err) {
      pushRebuildToast({
        tone: 'error',
        title: uiMessage("common:sidebar.title_title_failed"),
        description: uiMessage("common:sidebar.description_description_fileVariant2", { value: err instanceof Error ? err.message : String(err) }),
      });
    } finally {
      setRebuildingDirs((prev) => ({ ...prev, [projectDir]: false }));
    }
  }, []);

  // When a new project is opened, collapse all others and expand the new one
  // We detect this by checking if a project in openProjects doesn't have an expanded state yet
  const getProjectExpanded = useCallback((projectDir: string) => {
    // Default to collapsed if not yet set
    if (!(projectDir in expandedProjects)) {
      return false;
    }
    return expandedProjects[projectDir];
  }, [expandedProjects]);

  const handleProjectChildrenTransitionEnd = useCallback(
    (projectDir: string, event: TransitionEvent<HTMLDivElement>) => {
      if (event.target !== event.currentTarget || event.propertyName !== 'max-height') {
        return;
      }

      if (getProjectExpanded(projectDir)) {
        return;
      }

      setRenderedProjectChildren((prev) => {
        if (!prev[projectDir]) {
          return prev;
        }

        return {
          ...prev,
          [projectDir]: false,
        };
      });
    },
    [getProjectExpanded]
  );

  return (
    <aside className={`sidebar ${expanded ? 'sidebar--expanded' : 'sidebar--collapsed'}`}>
      <div className="sidebar__header">
        <img src={logoUrl} alt="" className="sidebar__logo-img" />
        {expanded && <span className="sidebar__logo">{translate("common:sidebar.sidebarHeader_message_galTransl")}</span>}
      </div>

      <div className="sidebar__top-nav">
        <NavLink
          to="/"
          end
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title={translate("common:sidebar.sidebarTopNav_title_text")}
        >
          <span className="sidebar__nav-icon"><Icon name="home" /></span>
          {expanded && <span className="sidebar__nav-label">{translate("common:sidebar.sidebarTopNav_message_text")}</span>}
        </NavLink>
        <NavLink
          to="/agent"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title={translate("common:sidebar.sidebarTopNav_title_agent")}
        >
          <span className="sidebar__nav-icon"><Icon name="bot" /></span>
          {expanded && <span className="sidebar__nav-label">{translate("common:sidebar.sidebarTopNav_message_agent")}</span>}
          {agentRunning && <RunningDot variant={expanded ? 'child' : 'rail'} agent />}
        </NavLink>
      </div>

      <nav className="sidebar__nav">
        {wizardOpen && (
          <div className="sidebar__project-group">
            <NavLink
              to="/new-project"
              className={({ isActive }) =>
                expanded
                  ? `sidebar__project-header sidebar__wizard-header${compactProjectHeaders ? ' sidebar__project-header--compact' : ''}${isActive ? ' sidebar__wizard-header--active' : ''}`
                  : `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
              }
              title={wizardProjectName ? translate("common:sidebar.sidebarProjectGroup_title_newProject", { wizardProjectName: wizardProjectName }) : translate("common:sidebar.sidebarProjectGroup_title_newProjectVariant2")}
            >
              <span className={`sidebar__nav-icon${expanded ? ' sidebar__project-icon' : ''}`}><Icon name="file-plus" /></span>
              {expanded && (
                <span className="sidebar__project-name">
                  {wizardProjectName ? translate("common:sidebar.sidebarProjectName_message_new", { wizardProjectName: wizardProjectName }) : translate("common:sidebar.sidebarProjectName_message_newProject")}
                </span>
              )}
            </NavLink>
          </div>
        )}
        {openProjects.map((projectDir) => {
          const projectName = basenamePath(projectDir) || projectDir;
          const projectId = encodeProjectDir(projectDir);
          const isProjectExpanded = getProjectExpanded(projectDir);
          const shouldRenderProjectChildren = renderedProjectChildren[projectDir] ?? isProjectExpanded;
          const isProjectChildrenVisible = visibleProjectChildren[projectDir] ?? isProjectExpanded;
          const isConfirming = confirmCloseDir === projectDir;

          return (
            <div className={`sidebar__project-group${compactProjectHeaders ? ' sidebar__project-group--compact' : ''}`} key={projectDir}>
              {expanded ? (
                <>
                  <button
                    className={`sidebar__project-header${compactProjectHeaders ? ' sidebar__project-header--compact' : ''}`}
                    title={projectDir}
                    type="button"
                    onClick={() => toggleProjectExpanded(projectDir)}
                    onContextMenu={(e) => handleProjectContextMenu(e, projectDir)}
                  >
                    <ProjectFolderPopover
                      projectDir={projectDir}
                      expanded={isProjectExpanded}
                      className={`sidebar__nav-icon sidebar__project-icon sidebar__project-icon--link${isProjectExpanded ? ' sidebar__project-icon--open' : ''}${compactProjectHeaders ? ' sidebar__project-icon--compact' : ''}`}
                      onError={(description) => pushRebuildToast({ tone: 'error', title: uiMessage("common:sidebar.title_title_openFileFailedVariant2"), description })}
                    />
                    <span className="sidebar__project-name">{projectName}</span>
                    <button
                      className="sidebar__project-close"
                      type="button"
                      onClick={(e) => { e.stopPropagation(); handleRequestClose(projectDir); }}
                      title={translate("common:sidebar.sidebarProjectClose_title_disableProject")}
                    >
                      <Icon name="close" />
                    </button>
                    {isConfirming && (
                      <div
                        className="sidebar__project-confirm-bubble"
                        ref={confirmBubbleRef}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <span className="sidebar__project-confirm-text">{translate("common:sidebar.sidebarProjectConfirmBubble_message_disable")}</span>
                        <button
                          className="sidebar__project-confirm-yes"
                          type="button"
                          onClick={() => handleConfirmClose(projectDir)}
                        >{translate("common:actions.confirm")}</button>
                        <button
                          className="sidebar__project-confirm-no"
                          type="button"
                          onClick={handleCancelClose}
                        >{translate("common:actions.cancel")}</button>
                      </div>
                    )}
                  </button>
                  {shouldRenderProjectChildren && (
                    <div
                      className={`sidebar__project-children ${isProjectChildrenVisible ? 'sidebar__project-children--expanded' : 'sidebar__project-children--collapsed'}`}
                      aria-hidden={!isProjectChildrenVisible}
                      onTransitionEnd={(event) => handleProjectChildrenTransitionEnd(projectDir, event)}
                    >
                      {PROJECT_TABS.map((tab) => (
                        <NavLink
                          key={tab.path}
                          to={`/project/${projectId}/${tab.path}`}
                          className={({ isActive }) =>
                            `sidebar__project-child ${isActive ? 'sidebar__project-child--active' : ''}`
                          }
                        >
                          <span className="sidebar__project-child-icon"><Icon name={tab.icon} /></span>
                          <span className="sidebar__project-child-label">{tab.label}</span>
                          {tab.path === 'translate' && translatingDirs[projectDir] && (
                            <RunningDot variant="child" />
                          )}
                          {tab.path === 'config' && dirtyConfigProjects[projectDir] && (
                            <span className="sidebar__project-child-notice-dot" aria-label={translate("common:sidebar.sidebarNav_ariaLabel_configNotSaveChange")} />
                          )}
                        </NavLink>
                      ))}
                      <div className="sidebar__project-child-separator" />
                      <NavLink
                        to="."
                        onClick={(e) => { e.preventDefault(); if (!rebuildingDirs[projectDir] && !translatingDirs[projectDir]) void handleRebuildOutput(projectDir); }}
                        className={() => `sidebar__project-child sidebar__project-child--action${translatingDirs[projectDir] ? ' sidebar__project-child--disabled' : ''}`}
                        title={translatingDirs[projectDir] ? translate("common:sidebar.sidebarNav_title_projectPendingTranslationUnable") : translate("common:sidebar.sidebarNav_title_fileOpenFile")}
                        style={(rebuildingDirs[projectDir] || translatingDirs[projectDir]) ? { opacity: 0.6, pointerEvents: 'none' } : undefined}
                      >
                        <span className="sidebar__project-child-icon">
                          <Icon name={rebuildingDirs[projectDir] ? 'hourglass' : translatingDirs[projectDir] ? 'ban' : 'upload'} />
                        </span>
                        <span className="sidebar__project-child-label">{translate("common:sidebar.sidebarNav_message_text")}</span>
                      </NavLink>
                    </div>
                  )}
                </>
              ) : isProjectExpanded ? (
                <>
                  <NavLink
                    to={`/project/${projectId}/${loadLastProjectTab(projectDir)}`}
                    className={({ isActive }) =>
                      `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
                    }
                    title={projectName}
                  >
                    <ProjectFolderPopover projectDir={projectDir} className="sidebar__nav-icon" onError={(description) => pushRebuildToast({ tone: 'error', title: uiMessage("common:sidebar.title_title_openFileFailedVariant2"), description })} />
                  </NavLink>
                  {PROJECT_TABS.map((tab) => (
                    <NavLink
                      key={tab.path}
                      to={`/project/${projectId}/${tab.path}`}
                      className={({ isActive }) =>
                        `sidebar__nav-item sidebar__nav-item--sub ${isActive ? 'sidebar__nav-item--active' : ''}`
                      }
                      title={tab.label}
                    >
                      <span className="sidebar__nav-icon"><Icon name={tab.icon} /></span>
                      {tab.path === 'translate' && translatingDirs[projectDir] && <RunningDot variant="rail" />}
                      {tab.path === 'config' && dirtyConfigProjects[projectDir] && (
                        <span className="sidebar__nav-notice-dot sidebar__project-config-notice-dot" aria-label={translate("common:sidebar.sidebarNav_ariaLabel_configNotSaveChange")} />
                      )}
                    </NavLink>
                  ))}
                  <NavLink
                    to="."
                    onClick={(e) => { e.preventDefault(); if (!rebuildingDirs[projectDir] && !translatingDirs[projectDir]) void handleRebuildOutput(projectDir); }}
                    className={() => `sidebar__nav-item sidebar__nav-item--sub${translatingDirs[projectDir] ? ' sidebar__nav-item--disabled' : ''}`}
                    title={translatingDirs[projectDir] ? translate("common:sidebar.sidebarNav_title_projectPendingTranslation") : translate("common:sidebar.sidebarNav_title_text")}
                    style={(rebuildingDirs[projectDir] || translatingDirs[projectDir]) ? { opacity: 0.6, pointerEvents: 'none' } : undefined}
                  >
                    <span className="sidebar__nav-icon">
                      <Icon name={rebuildingDirs[projectDir] ? 'hourglass' : translatingDirs[projectDir] ? 'ban' : 'upload'} />
                    </span>
                  </NavLink>
                </>
              ) : (
                <NavLink
                  to={`/project/${projectId}/${loadLastProjectTab(projectDir)}`}
                  className={({ isActive }) =>
                    `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
                  }
                  title={projectName}
                >
                  <ProjectFolderPopover projectDir={projectDir} className="sidebar__nav-icon" onError={(description) => pushRebuildToast({ tone: 'error', title: uiMessage("common:sidebar.title_title_openFileFailedVariant2"), description })} />
                </NavLink>
              )}
            </div>
          );
        })}
      </nav>

      <nav className="sidebar__bottom-nav">
        <NavLink
          to="/backend-profiles"
          className={({ isActive }) =>
            `sidebar__nav-item${!hasBackendProfiles ? ' sidebar__nav-item--notice' : ''} ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title={translate("common:sidebar.sidebarBottomNav_title_modelSettings")}
        >
          <span className="sidebar__nav-icon"><Icon name="bot" /></span>
          {expanded && <span className="sidebar__nav-label">{translate("common:sidebar.sidebarBottomNav_message_modelSettings")}</span>}
          {!hasBackendProfiles && <span className="sidebar__nav-notice-dot" aria-label={translate("common:sidebar.sidebarBottomNav_ariaLabel_notConfiguredModelSettings")} />}
        </NavLink>

        <NavLink
          to="/common-dictionaries"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title={translate("common:sidebar.sidebarBottomNav_title_dictionary")}
        >
          <span className="sidebar__nav-icon"><Icon name="books" /></span>
          {expanded && <span className="sidebar__nav-label">{translate("common:sidebar.sidebarBottomNav_message_dictionary")}</span>}
        </NavLink>

        <NavLink
          to="/settings"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title={translate("common:sidebar.sidebarBottomNav_title_settings")}
        >
          <span className="sidebar__nav-icon"><Icon name="settings" /></span>
          {expanded && <span className="sidebar__nav-label">{translate("common:sidebar.sidebarBottomNav_message_settings")}</span>}
        </NavLink>
      </nav>

      <div className="sidebar__footer">
        <button
          className="sidebar__toggle-btn"
          type="button"
          onClick={toggleExpanded}
          title={expanded ? translate("common:sidebar.sidebarToggleBtn_title_text") : translate("common:sidebar.sidebarToggleBtn_title_textVariant2")}
        >
          <span className={`sidebar__toggle-icon ${expanded ? 'sidebar__toggle-icon--flip' : ''}`}>
            <Icon name="chevron-right" />
          </span>
          {expanded && <span className="sidebar__toggle-label">{translate("common:sidebar.sidebarToggleBtn_message_text")}</span>}
        </button>
      </div>

      {contextMenu && (
        <div
          className="sidebar__context-menu"
          ref={contextMenuRef}
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            className="sidebar__context-menu-item"
            type="button"
            onClick={() => {
              handleRequestClose(contextMenu.projectDir);
              setContextMenu(null);
            }}
          >{translate("common:sidebar.sidebarContextMenu_message_disableProject")}</button>
          <button
            className="sidebar__context-menu-item"
            type="button"
            disabled={openProjects.length <= 1}
            onClick={() => {
              onCloseOtherProjects(contextMenu.projectDir);
              setContextMenu(null);
            }}
          >{translate("common:sidebar.sidebarContextMenu_message_disableProjectVariant2")}</button>
          <button
            className="sidebar__context-menu-item sidebar__context-menu-item--danger"
            type="button"
            disabled={openProjects.length === 0}
            onClick={() => {
              onCloseAllProjects();
              setContextMenu(null);
            }}
          >{translate("common:sidebar.sidebarContextMenu_message_disableProjectVariant3")}</button>
        </div>
      )}

      {rebuildToasts.length > 0 ? (
        <div className="sidebar__toast-host" aria-live="assertive">
          {rebuildToasts.map((toast) => (
            <InlineFeedback
              key={toast.id}
              tone={toast.tone}
              title={resolveMessage(toast.title)}
              description={resolveMessage(toast.description)}
              autoDismiss={toast.tone === 'error' ? 4200 : undefined}
              onDismiss={() => dismissRebuildToast(toast.id)}
            />
          ))}
        </div>
      ) : null}
    </aside>
  );
}
