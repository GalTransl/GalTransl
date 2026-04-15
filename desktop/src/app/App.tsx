import { useCallback, useEffect, useRef, useState } from 'react';
import { HashRouter, Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import { decodeProjectDir } from '../lib/api';
import { Sidebar } from '../components/Sidebar';
import { ProjectLayout } from '../components/ProjectLayout';
import { ConnectionProvider } from '../features/connection/ConnectionContext';
import { HomePage, addProjectToHistory } from '../pages/HomePage';
import { BackendProfilesPage } from '../pages/BackendProfilesPage';
import { SettingsPage } from '../pages/SettingsPage';
import { CommonDictionaryPage } from '../pages/CommonDictionaryPage';

const CONFIG_FILE_KEY = 'galtransl-config-file';
const OPEN_PROJECTS_KEY = 'galtransl-open-projects';
const LAST_ROUTE_KEY = 'galtransl-last-route';

function saveConfigFileName(projectDir: string, configFileName: string) {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    map[projectDir] = configFileName;
    localStorage.setItem(CONFIG_FILE_KEY, JSON.stringify(map));
  } catch {
    // ignore storage errors
  }
}

function loadOpenProjects(): string[] {
  try {
    const raw = localStorage.getItem(OPEN_PROJECTS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveOpenProjects(projects: string[]) {
  try {
    localStorage.setItem(OPEN_PROJECTS_KEY, JSON.stringify(projects));
  } catch {
    // ignore storage errors
  }
}

function loadLastRoute(): string {
  try {
    return localStorage.getItem(LAST_ROUTE_KEY) || '/';
  } catch {
    return '/';
  }
}

function saveLastRoute(route: string) {
  try {
    localStorage.setItem(LAST_ROUTE_KEY, route);
  } catch {
    // ignore storage errors
  }
}

export function App() {
  const [openProjects, setOpenProjects] = useState<string[]>(() => loadOpenProjects());

  // Persist open projects to localStorage whenever the list changes
  useEffect(() => {
    saveOpenProjects(openProjects);
  }, [openProjects]);

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

  return (
    <HashRouter>
      <ConnectionProvider>
        <AppInner
          openProjects={openProjects}
          onOpenProject={handleOpenProject}
          onCloseProject={handleCloseProject}
        />
      </ConnectionProvider>
    </HashRouter>
  );
}

type AppInnerProps = {
  openProjects: string[];
  onOpenProject: (projectDir: string, config: string) => void;
  onCloseProject: (projectDir: string) => void;
};

function AppInner({ openProjects, onOpenProject, onCloseProject }: AppInnerProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [displayLocation, setDisplayLocation] = useState(location);
  const [transitionStage, setTransitionStage] = useState<'fadeIn' | 'fadeOut'>('fadeIn');
  const initialRestoreDone = useRef(false);

  // On first mount, restore the last active route if we have open projects
  useEffect(() => {
    if (initialRestoreDone.current) return;
    initialRestoreDone.current = true;
    if (openProjects.length > 0) {
      const lastRoute = loadLastRoute();
      // Only restore if the route is a project route that still has an open project
      const projectMatch = lastRoute.match(/^\/project\/([^/]+)/);
      if (projectMatch) {
        try {
          const projectDir = decodeProjectDir(projectMatch[1]);
          if (openProjects.includes(projectDir)) {
            const normalizedRoute = lastRoute === `/project/${projectMatch[1]}`
              ? `/project/${projectMatch[1]}/translate`
              : lastRoute;
            navigate(normalizedRoute, { replace: true });
            return;
          }
        } catch {
          // Invalid project ID, fall through to home
        }
        // Project no longer open, go home
        navigate('/', { replace: true });
      } else if (lastRoute !== '/') {
        // Non-project route (settings, plugins, etc.), restore it
        navigate(lastRoute, { replace: true });
      }
    }
  }, [openProjects, navigate]);

  // Persist the current route whenever it changes
  useEffect(() => {
    saveLastRoute(location.pathname);
  }, [location.pathname]);

  useEffect(() => {
    if (location.pathname !== displayLocation.pathname) {
      setTransitionStage('fadeOut');
    }
  }, [location, displayLocation]);

  const handleTransitionEnd = () => {
    if (transitionStage === 'fadeOut') {
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

  return (
    <div className="app-layout">
      <Sidebar openProjects={openProjects} onCloseProject={handleCloseProjectAndNavigate} />
      <main
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
                element={<BackendProfilesPage />}
              />
              <Route
                path="/common-dictionaries"
                element={<CommonDictionaryPage />}
              />
              <Route
                path="/settings"
                element={<SettingsPage />}
              />
              <Route
                path="/project/:projectId/*"
                element={<ProjectLayout />}
              />
        </Routes>
      </main>
    </div>
  );
}
