import { useCallback, useEffect, useRef, useState, type TransitionEvent } from 'react';
import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import { invoke } from '@tauri-apps/api/core';
import { encodeProjectDir, decodeProjectDir } from '../lib/api';

const PROJECT_TABS = [
  { path: 'translate', label: '翻译工作台', icon: '🌐' },
  { path: 'config', label: '配置编辑', icon: '⚙️' },
  { path: 'dictionary', label: '项目字典', icon: '📖' },
  { path: 'names', label: '人名翻译', icon: '👤' },
  { path: 'cache', label: '缓存编辑', icon: '💾' },
  { path: 'problems', label: '问题审查', icon: '🔍' },
];

type SidebarProps = {
  openProjects: string[];
  onCloseProject: (projectDir: string) => void;
};

export function Sidebar({ openProjects, onCloseProject }: SidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [expanded, setExpanded] = useState(true);
  // Track which projects are expanded in the sidebar (keyed by projectDir)
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>({});
  // Keep submenu content mounted long enough for close animations to complete
  const [renderedProjectChildren, setRenderedProjectChildren] = useState<Record<string, boolean>>({});
  // Track the visual open/closed state separately so expand animations can start from collapsed
  const [visibleProjectChildren, setVisibleProjectChildren] = useState<Record<string, boolean>>({});
  // Track which project is showing the close confirmation bubble
  const [confirmCloseDir, setConfirmCloseDir] = useState<string | null>(null);
  const prevOpenProjectsRef = useRef<string[]>(openProjects);
  const confirmBubbleRef = useRef<HTMLDivElement>(null);
  const expandAnimationFrameRef = useRef<Record<string, number>>({});

  // When a new project is opened, collapse all others and expand the new one
  useEffect(() => {
    const prev = prevOpenProjectsRef.current;
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

  const toggleExpanded = useCallback(() => {
    setExpanded((prev) => !prev);
  }, []);

  useEffect(() => {
    setRenderedProjectChildren((prev) => {
      let changed = false;
      const next: Record<string, boolean> = {};

      for (const projectDir of openProjects) {
        const isExpanded = projectDir in expandedProjects ? expandedProjects[projectDir] : true;
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
        const isExpanded = projectDir in expandedProjects ? expandedProjects[projectDir] : true;
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
      const isCurrentlyExpanded = prev[projectDir] ?? true;
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
        // Navigate to the project's translate page
        const projectId = encodeProjectDir(projectDir);
        navigate(`/project/${projectId}/translate`);
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

  // When a new project is opened, collapse all others and expand the new one
  // We detect this by checking if a project in openProjects doesn't have an expanded state yet
  const getProjectExpanded = useCallback((projectDir: string) => {
    // Default to expanded if not yet set
    if (!(projectDir in expandedProjects)) {
      return true;
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
        {expanded && <span className="sidebar__logo">GalTransl</span>}
        {!expanded && <span className="sidebar__logo-icon">G</span>}
      </div>

      <div className="sidebar__top-nav">
        <NavLink
          to="/"
          end
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title="首页"
        >
          <span className="sidebar__nav-icon">🏠</span>
          {expanded && <span className="sidebar__nav-label">首页</span>}
        </NavLink>
      </div>

      <nav className="sidebar__nav">
        {openProjects.map((projectDir) => {
          const projectName = projectDir.replace(/[/\\]/g, '/').split('/').filter(Boolean).pop() || projectDir;
          const projectId = encodeProjectDir(projectDir);
          const isProjectExpanded = getProjectExpanded(projectDir);
          const shouldRenderProjectChildren = renderedProjectChildren[projectDir] ?? isProjectExpanded;
          const isProjectChildrenVisible = visibleProjectChildren[projectDir] ?? isProjectExpanded;
          const isConfirming = confirmCloseDir === projectDir;

          return (
            <div className="sidebar__project-group" key={projectDir}>
              {expanded ? (
                <>
                  <button
                    className="sidebar__project-header"
                    title={projectDir}
                    type="button"
                    onClick={() => toggleProjectExpanded(projectDir)}
                  >
                    <span
                      className="sidebar__nav-icon sidebar__project-icon sidebar__project-icon--link"
                      role="button"
                      tabIndex={0}
                      title="打开项目文件夹"
                      onClick={(e) => { e.stopPropagation(); void invoke('open_folder', { path: projectDir }); }}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); e.preventDefault(); void invoke('open_folder', { path: projectDir }); } }}
                    >
                      {isProjectExpanded ? '📂' : '📁'}
                    </span>
                    <span className="sidebar__project-name">{projectName}</span>
                    <button
                      className="sidebar__project-close"
                      type="button"
                      onClick={(e) => { e.stopPropagation(); handleRequestClose(projectDir); }}
                      title="关闭项目"
                    >
                      ✕
                    </button>
                    {isConfirming && (
                      <div
                        className="sidebar__project-confirm-bubble"
                        ref={confirmBubbleRef}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <span className="sidebar__project-confirm-text">关闭?</span>
                        <button
                          className="sidebar__project-confirm-yes"
                          type="button"
                          onClick={() => handleConfirmClose(projectDir)}
                        >
                          确定
                        </button>
                        <button
                          className="sidebar__project-confirm-no"
                          type="button"
                          onClick={handleCancelClose}
                        >
                          取消
                        </button>
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
                          <span className="sidebar__project-child-icon">{tab.icon}</span>
                          <span className="sidebar__project-child-label">{tab.label}</span>
                        </NavLink>
                      ))}
                    </div>
                  )}
                </>
              ) : isProjectExpanded ? (
                <>
                  <NavLink
                    to={`/project/${projectId}/translate`}
                    className={({ isActive }) =>
                      `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
                    }
                    title={projectName}
                  >
                    <span className="sidebar__nav-icon">📁</span>
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
                      <span className="sidebar__nav-icon">{tab.icon}</span>
                    </NavLink>
                  ))}
                </>
              ) : (
                <NavLink
                  to={`/project/${projectId}/translate`}
                  className={({ isActive }) =>
                    `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
                  }
                  title={projectName}
                >
                  <span className="sidebar__nav-icon">📁</span>
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
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title="翻译后端配置"
        >
          <span className="sidebar__nav-icon">🤖</span>
          {expanded && <span className="sidebar__nav-label">翻译后端配置</span>}
        </NavLink>

        <NavLink
          to="/common-dictionaries"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title="通用字典管理"
        >
          <span className="sidebar__nav-icon">📚</span>
          {expanded && <span className="sidebar__nav-label">通用字典管理</span>}
        </NavLink>

        <NavLink
          to="/plugins"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title="插件管理"
        >
          <span className="sidebar__nav-icon">🧩</span>
          {expanded && <span className="sidebar__nav-label">插件管理</span>}
        </NavLink>

        <NavLink
          to="/settings"
          className={({ isActive }) =>
            `sidebar__nav-item ${isActive ? 'sidebar__nav-item--active' : ''}`
          }
          title="设置"
        >
          <span className="sidebar__nav-icon">⚙️</span>
          {expanded && <span className="sidebar__nav-label">设置</span>}
        </NavLink>
      </nav>

      <div className="sidebar__footer">
        <button
          className="sidebar__toggle-btn"
          type="button"
          onClick={toggleExpanded}
          title={expanded ? '收起侧边栏' : '展开侧边栏'}
        >
          <span className={`sidebar__toggle-icon ${expanded ? 'sidebar__toggle-icon--flip' : ''}`}>
            ▶
          </span>
          {expanded && <span className="sidebar__toggle-label">收起</span>}
        </button>
      </div>
    </aside>
  );
}
