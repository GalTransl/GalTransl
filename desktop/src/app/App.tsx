import { useCallback, useEffect, useState } from 'react';
import { HashRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom';
import { decodeProjectDir, encodeProjectDir } from '../lib/api';
import { Sidebar } from '../components/Sidebar';
import { ProjectLayout } from '../components/ProjectLayout';
import { ConnectionProvider } from '../features/connection/ConnectionContext';
import { HomePage, addProjectToHistory } from '../pages/HomePage';
import { BackendProfilesPage } from '../pages/BackendProfilesPage';
import { SettingsPage } from '../pages/SettingsPage';
import { CommonDictionaryPage } from '../pages/CommonDictionaryPage';
import { NewProjectWizard } from '../pages/NewProjectWizard';

const CONFIG_FILE_KEY = 'galtransl-config-file';
const OPEN_PROJECTS_KEY = 'galtransl-open-projects';

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
  const [displayLocation, setDisplayLocation] = useState(location);
  const [transitionStage, setTransitionStage] = useState<'fadeIn' | 'fadeOut'>('fadeIn');

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

  return (
    <div className="app-layout">
      <Sidebar
        openProjects={openProjects}
        onCloseProject={handleCloseProjectAndNavigate}
        onCloseOtherProjects={handleCloseOtherProjectsAndNavigate}
        onCloseAllProjects={handleCloseAllProjectsAndNavigate}
      />
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
                path="/new-project"
                element={<NewProjectWizard onOpenProject={onOpenProject} />}
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
