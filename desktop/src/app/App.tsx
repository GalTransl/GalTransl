import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { HashRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom';
import {
  CUSTOM_BACKGROUND_CHANGE_EVENT,
  OPEN_PROJECTS_CHANGE_EVENT,
  THEME_MODE_CHANGE_EVENT,
  type CustomBackgroundPreference,
  decodeProjectDir,
  encodeProjectDir,
  getCustomBackgroundPreference,
  getThemeModePreference,
  loadOpenProjects,
  persistOpenProjects,
  saveConfigFileName,
} from '../lib/api';
import { Sidebar } from '../components/Sidebar';
import { RenderErrorBoundary } from '../components/RenderErrorBoundary';
import { ConnectionProvider } from '../features/connection/ConnectionContext';
import { HomePage, addProjectToHistory } from '../pages/HomePage';

const ProjectLayout = lazy(async () => {
  const mod = await import('../components/ProjectLayout');
  return { default: mod.ProjectLayout };
});

const BackendProfilesPage = lazy(async () => {
  const mod = await import('../pages/BackendProfilesPage');
  return { default: mod.BackendProfilesPage };
});

const SettingsPage = lazy(async () => {
  const mod = await import('../pages/SettingsPage');
  return { default: mod.SettingsPage };
});

const PromptTemplatesPage = lazy(async () => {
  const mod = await import('../pages/PromptTemplatesPage');
  return { default: mod.PromptTemplatesPage };
});

const CommonGuidelinesPage = lazy(async () => {
  const mod = await import('../pages/CommonGuidelinesPage');
  return { default: mod.CommonGuidelinesPage };
});

const CommonDictionaryPage = lazy(async () => {
  const mod = await import('../pages/CommonDictionaryPage');
  return { default: mod.CommonDictionaryPage };
});

const NewProjectWizard = lazy(async () => {
  const mod = await import('../pages/NewProjectWizard');
  return { default: mod.NewProjectWizard };
});

const AgentPage = lazy(async () => {
  const mod = await import('../pages/AgentPage');
  return { default: mod.AgentPage };
});

const LAST_ACTIVE_PROJECT_KEY = 'galtransl-last-active-project';

function RouteLoadingFallback() {
  return <div className="inline-feedback">页面加载中…</div>;
}

function loadLastActiveProject(): string | null {
  try {
    return localStorage.getItem(LAST_ACTIVE_PROJECT_KEY);
  } catch {
    return null;
  }
}

function saveLastActiveProject(projectDir: string) {
  try {
    localStorage.setItem(LAST_ACTIVE_PROJECT_KEY, projectDir);
  } catch {
    // ignore storage errors
  }
}

function clearLastActiveProject() {
  try {
    localStorage.removeItem(LAST_ACTIVE_PROJECT_KEY);
  } catch {
    // ignore storage errors
  }
}

export function App() {
  const [openProjects, setOpenProjects] = useState<string[]>(() => loadOpenProjects());

  useEffect(() => {
    const root = document.documentElement;
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');

    const applyTheme = () => {
      const mode = getThemeModePreference();
      const resolved = mode === 'system' ? (mediaQuery.matches ? 'dark' : 'light') : mode;
      root.dataset.themeMode = mode;
      root.dataset.theme = resolved;
    };

    const handleThemeModeChange = () => {
      applyTheme();
    };

    const handleSystemSchemeChange = () => {
      if (getThemeModePreference() === 'system') {
        applyTheme();
      }
    };

    applyTheme();
    window.addEventListener(THEME_MODE_CHANGE_EVENT, handleThemeModeChange as EventListener);
    mediaQuery.addEventListener('change', handleSystemSchemeChange);

    return () => {
      window.removeEventListener(THEME_MODE_CHANGE_EVENT, handleThemeModeChange as EventListener);
      mediaQuery.removeEventListener('change', handleSystemSchemeChange);
    };
  }, []);

  // Persist open projects to localStorage whenever the list changes (silent —
  // no broadcast, to avoid a self-loop with the OPEN_PROJECTS_CHANGE listener).
  useEffect(() => {
    persistOpenProjects(openProjects);
  }, [openProjects]);

  // Listen for open-projects changed elsewhere (e.g. Agent page calling
  // addOpenProject) and sync App's state so the global sidebar updates too.
  useEffect(() => {
    const sync = (e: Event) => {
      const next = (e as CustomEvent<string[]>).detail;
      if (!Array.isArray(next)) return;
      setOpenProjects((prev) => {
        if (prev.length === next.length && prev.every((d, i) => d === next[i])) return prev;
        return next;
      });
    };
    window.addEventListener(OPEN_PROJECTS_CHANGE_EVENT, sync as EventListener);
    return () => window.removeEventListener(OPEN_PROJECTS_CHANGE_EVENT, sync as EventListener);
  }, []);

  const handleOpenProject = useCallback((projectDir: string, config: string) => {
    const cfg = config || 'config.yaml';
    setOpenProjects((prev) => {
      if (prev.includes(projectDir)) return prev;
      return [projectDir, ...prev];
    });
    saveConfigFileName(projectDir, cfg);
    addProjectToHistory(projectDir, cfg);
  }, []);

  const handleCloseProject = useCallback((projectDir: string) => {
    setOpenProjects((prev) => prev.filter((d) => d !== projectDir));
  }, []);

  const handleCloseOtherProjects = useCallback((keepProjectDir: string) => {
    setOpenProjects((prev) => prev.filter((d) => d === keepProjectDir));
  }, []);

  const handleCloseAllProjects = useCallback(() => {
    setOpenProjects([]);
  }, []);

  return (
    <HashRouter>
      <ConnectionProvider>
        <AppInner
          openProjects={openProjects}
          onOpenProject={handleOpenProject}
          onCloseProject={handleCloseProject}
          onCloseOtherProjects={handleCloseOtherProjects}
          onCloseAllProjects={handleCloseAllProjects}
        />
      </ConnectionProvider>
    </HashRouter>
  );
}

type AppInnerProps = {
  openProjects: string[];
  onOpenProject: (projectDir: string, config: string) => void;
  onCloseProject: (projectDir: string) => void;
  onCloseOtherProjects: (keepProjectDir: string) => void;
  onCloseAllProjects: () => void;
};

function AppInner({ openProjects, onOpenProject, onCloseProject, onCloseOtherProjects, onCloseAllProjects }: AppInnerProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const contentRef = useRef<HTMLElement | null>(null);
  const [displayLocation, setDisplayLocation] = useState(location);
  const [transitionStage, setTransitionStage] = useState<'fadeIn' | 'fadeOut'>('fadeIn');
  const [customBackground, setCustomBackground] = useState<CustomBackgroundPreference>(() => getCustomBackgroundPreference());

  useEffect(() => {
    const projectMatch = location.pathname.match(/^\/project\/([^/]+)/);
    if (projectMatch) {
      try {
        const projectDir = decodeProjectDir(projectMatch[1]);
        saveLastActiveProject(projectDir);
      } catch {
        // ignore invalid project id in URL
      }
    }
  }, [location.pathname]);

  useEffect(() => {
    const lastActiveProject = loadLastActiveProject();
    if (!lastActiveProject) {
      return;
    }

    if (openProjects.length === 0) {
      clearLastActiveProject();
      return;
    }

    if (!openProjects.includes(lastActiveProject)) {
      saveLastActiveProject(openProjects[0]);
    }
  }, [openProjects]);

  useEffect(() => {
    const handleCustomBackgroundChange = () => {
      setCustomBackground(getCustomBackgroundPreference());
    };

    window.addEventListener(CUSTOM_BACKGROUND_CHANGE_EVENT, handleCustomBackgroundChange as EventListener);
    return () => {
      window.removeEventListener(CUSTOM_BACKGROUND_CHANGE_EVENT, handleCustomBackgroundChange as EventListener);
    };
  }, []);

  // Timeout fallback: if animationend never fires (e.g. prefers-reduced-motion
  // disables animations), force the transition to complete so the page doesn't
  // get stuck on the old route.
  const transitionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (location.pathname !== displayLocation.pathname) {
      setTransitionStage('fadeOut');
      // The fadeOut animation is 150 ms; give a generous margin before forcing.
      if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
      transitionTimerRef.current = setTimeout(() => {
        setDisplayLocation(location);
        setTransitionStage('fadeIn');
      }, 200);
    }
    return () => {
      if (transitionTimerRef.current) {
        clearTimeout(transitionTimerRef.current);
        transitionTimerRef.current = null;
      }
    };
  }, [location, displayLocation]);

  useLayoutEffect(() => {
    contentRef.current?.scrollTo({ top: 0, left: 0, behavior: 'instant' as ScrollBehavior });
  }, [displayLocation.pathname]);

  const handleTransitionEnd = () => {
    if (transitionStage === 'fadeOut') {
      if (transitionTimerRef.current) {
        clearTimeout(transitionTimerRef.current);
        transitionTimerRef.current = null;
      }
      setDisplayLocation(location);
      setTransitionStage('fadeIn');
    }
  };

  const handleCloseProjectAndNavigate = useCallback((projectDir: string) => {
    onCloseProject(projectDir);
    // Navigate to home if this was the current project, or if no projects remain
    const projectMatch = location.pathname.match(/^\/project\/([^/]+)/);
    const isCurrentProject = projectMatch && decodeProjectDir(projectMatch[1]) === projectDir;
    const willHaveNoProjects = openProjects.length <= 1;
    if (isCurrentProject || willHaveNoProjects) {
      navigate('/');
    }
  }, [navigate, onCloseProject, location.pathname, openProjects]);

  const handleCloseOtherProjectsAndNavigate = useCallback((keepProjectDir: string) => {
    onCloseOtherProjects(keepProjectDir);
    // Navigate to home if the current project is not the one being kept
    const projectMatch = location.pathname.match(/^\/project\/([^/]+)/);
    const isCurrentKept = projectMatch && decodeProjectDir(projectMatch[1]) === keepProjectDir;
    if (!isCurrentKept) {
      const projectId = encodeProjectDir(keepProjectDir);
      navigate(`/project/${projectId}/translate`);
    }
  }, [navigate, onCloseOtherProjects, location.pathname]);

  const handleCloseAllProjectsAndNavigate = useCallback(() => {
    onCloseAllProjects();
    navigate('/');
  }, [navigate, onCloseAllProjects]);

  const surfaceOpacity = customBackground.surfaceOpacity / 100;
  const softSurfaceOpacity = Math.max(0.1, surfaceOpacity - 0.14);
  const appLayoutStyle = {
    '--custom-bg-surface-opacity': String(surfaceOpacity),
    '--custom-bg-surface-soft-opacity': String(softSurfaceOpacity),
  } as CSSProperties;

  return (
    <div
      className={`app-layout${customBackground.imageDataUrl ? ' app-layout--custom-background' : ''}`}
      style={appLayoutStyle}
    >
      <div
        className={`app-layout__custom-background ${customBackground.imageDataUrl ? 'app-layout__custom-background--visible' : ''}`}
        style={{
          backgroundImage: customBackground.imageDataUrl ? `url(${customBackground.imageDataUrl})` : 'none',
          opacity: customBackground.opacity / 100,
        }}
      />
      <Sidebar
        openProjects={openProjects}
        onCloseProject={handleCloseProjectAndNavigate}
        onCloseOtherProjects={handleCloseOtherProjectsAndNavigate}
        onCloseAllProjects={handleCloseAllProjectsAndNavigate}
      />
      <main
        ref={contentRef}
        className={`app-layout__content page-transition-${transitionStage}`}
        onAnimationEnd={handleTransitionEnd}
      >
        <Routes location={displayLocation}>
              <Route
                path="/"
                element={<HomePage onOpenProject={onOpenProject} />}
              />
              <Route
                path="/backend-profiles"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <BackendProfilesPage />
                  </Suspense>
                )}
              />
              <Route
                path="/common-dictionaries"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <CommonDictionaryPage />
                  </Suspense>
                )}
              />
              <Route
                path="/settings"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <SettingsPage />
                  </Suspense>
                )}
              />
              <Route
                path="/settings/prompt-templates"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <PromptTemplatesPage />
                  </Suspense>
                )}
              />
              <Route
                path="/settings/common-guidelines"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <CommonGuidelinesPage />
                  </Suspense>
                )}
              />
              <Route
                path="/new-project"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <NewProjectWizard onOpenProject={onOpenProject} />
                  </Suspense>
                )}
              />
              <Route
                path="/agent"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <RenderErrorBoundary>
                      <AgentPage />
                    </RenderErrorBoundary>
                  </Suspense>
                )}
              />
              <Route
                path="/project/:projectId/*"
                element={(
                  <Suspense fallback={<RouteLoadingFallback />}>
                    <ProjectLayout />
                  </Suspense>
                )}
              />
        </Routes>
      </main>
    </div>
  );
}
