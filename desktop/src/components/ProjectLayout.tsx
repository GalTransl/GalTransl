import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { useEffect, useMemo } from 'react';
import { decodeProjectDir } from '../lib/api';
import { ProjectTranslatePage } from '../pages/ProjectTranslatePage';
import { ProjectConfigPage } from '../pages/ProjectConfigPage';
import { ProjectDictionaryPage } from '../pages/ProjectDictionaryPage';
import { ProjectNamePage } from '../pages/ProjectNamePage';
import { ProjectCachePage } from '../pages/ProjectCachePage';

const CONFIG_FILE_KEY = 'galtransl-config-file';

function loadConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
  }
}

/** Tab path → component mapping */
const TAB_MAP: { path: string; label: string }[] = [
  { path: 'translate', label: '翻译工作台' },
  { path: 'config', label: '配置编辑' },
  { path: 'dictionary', label: '项目字典' },
  { path: 'names', label: '人名翻译' },
  { path: 'cache', label: '缓存编辑' },
];

/** Shared context passed to every child page */
export interface ProjectPageContext {
  projectDir: string;
  projectId: string;
  configFileName: string;
}

export function ProjectLayout() {
  const { projectId } = useParams<{ projectId: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  const projectDir = projectId ? decodeProjectDir(projectId) : '';
  const configFileName = useMemo(() => loadConfigFileName(projectDir), [projectDir]);

  // Extract current tab from URL: /project/:projectId/cache → "cache"
  const segments = location.pathname.split('/');
  const currentTab = segments[3] || 'translate';

  // If accessing /project/:projectId without a tab, redirect to translate
  useEffect(() => {
    if (!segments[3]) {
      navigate(location.pathname + '/translate', { replace: true });
    }
  }, [segments[3], location.pathname, navigate]);

  const ctx: ProjectPageContext = useMemo(
    () => ({ projectDir, projectId: projectId || '', configFileName }),
    [projectDir, projectId, configFileName],
  );

  // ── Scroll to top on tab switch ──
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [currentTab]);

  return (
    <div className="project-layout">
      {TAB_MAP.map((tab) => (
        <div
          key={tab.path}
          style={{ display: currentTab === tab.path ? 'contents' : 'none' }}
        >
          {tab.path === 'translate' && <ProjectTranslatePage ctx={ctx} />}
          {tab.path === 'config' && <ProjectConfigPage ctx={ctx} />}
          {tab.path === 'dictionary' && <ProjectDictionaryPage ctx={ctx} />}
          {tab.path === 'names' && <ProjectNamePage ctx={ctx} />}
          {tab.path === 'cache' && <ProjectCachePage ctx={ctx} />}
        </div>
      ))}
    </div>
  );
}
