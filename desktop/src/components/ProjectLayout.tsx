import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { decodeProjectDir } from '../lib/api';
import { loadLastProjectTab, saveLastProjectTab } from '../lib/projectTabMemory';

const ProjectTranslatePage = lazy(async () => {
  const mod = await import('../pages/ProjectTranslatePage');
  return { default: mod.ProjectTranslatePage };
});

const ProjectConfigPage = lazy(async () => {
  const mod = await import('../pages/ProjectConfigPage');
  return { default: mod.ProjectConfigPage };
});

const ProjectDictionaryPage = lazy(async () => {
  const mod = await import('../pages/ProjectDictionaryPage');
  return { default: mod.ProjectDictionaryPage };
});

const ProjectNamePage = lazy(async () => {
  const mod = await import('../pages/ProjectNamePage');
  return { default: mod.ProjectNamePage };
});

const ProjectCachePage = lazy(async () => {
  const mod = await import('../pages/ProjectCachePage');
  return { default: mod.ProjectCachePage };
});

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
  { path: 'cache', label: '缓存与问题' },
  { path: 'config', label: '配置编辑' },
  { path: 'dictionary', label: '项目字典' },
  { path: 'names', label: '人名翻译' },
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

  // If accessing /project/:projectId without a tab, redirect to the last visited tab
  useEffect(() => {
    if (!segments[3]) {
      const lastTab = loadLastProjectTab(projectDir);
      navigate(location.pathname + '/' + lastTab, { replace: true });
    }
  }, [segments[3], location.pathname, navigate, projectDir]);

  const ctx: ProjectPageContext = useMemo(
    () => ({ projectDir, projectId: projectId || '', configFileName }),
    [projectDir, projectId, configFileName],
  );

  // ── Scroll to top on tab switch ──
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [currentTab]);

  const activeTab = TAB_MAP.some((tab) => tab.path === currentTab) ? currentTab : 'translate';

  // Save the active tab whenever it changes
  useEffect(() => {
    if (projectDir && activeTab) {
      saveLastProjectTab(projectDir, activeTab);
    }
  }, [projectDir, activeTab]);

  // 对"缓存与问题"页、人名翻译页、项目字典页：一旦访问过就保持挂载，
  // 避免重复加载，并让页内长任务在切换标签后继续运行。
  const [cacheVisited, setCacheVisited] = useState(() => activeTab === 'cache');
  const [dictionaryVisited, setDictionaryVisited] = useState(() => activeTab === 'dictionary');
  const [nameVisited, setNameVisited] = useState(() => activeTab === 'names');
  useEffect(() => {
    if (activeTab === 'cache') {
      setCacheVisited(true);
    }
  }, [activeTab]);

  useEffect(() => {
    if (activeTab === 'dictionary') {
      setDictionaryVisited(true);
    }
  }, [activeTab]);

  useEffect(() => {
    if (activeTab === 'names') {
      setNameVisited(true);
    }
  }, [activeTab]);

  const shouldRenderCache = cacheVisited || activeTab === 'cache';
  const shouldRenderDictionary = dictionaryVisited || activeTab === 'dictionary';
  const shouldRenderNames = nameVisited || activeTab === 'names';

  return (
    <div className="project-layout">
      <Suspense fallback={<div className="inline-feedback">页面加载中…</div>}>
        {activeTab === 'translate' ? <ProjectTranslatePage ctx={ctx} /> : null}
        {activeTab === 'config' ? <ProjectConfigPage ctx={ctx} /> : null}
        {shouldRenderDictionary ? (
          <div
            className="project-layout__keep-alive"
            hidden={activeTab !== 'dictionary'}
            style={activeTab !== 'dictionary' ? { display: 'none' } : undefined}
          >
            <ProjectDictionaryPage ctx={ctx} active={activeTab === 'dictionary'} />
          </div>
        ) : null}
        {shouldRenderNames ? (
          <div
            className="project-layout__keep-alive"
            hidden={activeTab !== 'names'}
            style={activeTab !== 'names' ? { display: 'none' } : undefined}
          >
            <ProjectNamePage ctx={ctx} active={activeTab === 'names'} />
          </div>
        ) : null}
        {shouldRenderCache ? (
          <div
            className="project-layout__keep-alive"
            hidden={activeTab !== 'cache'}
            style={activeTab !== 'cache' ? { display: 'none' } : undefined}
          >
            <ProjectCachePage ctx={ctx} />
          </div>
        ) : null}
      </Suspense>
    </div>
  );
}
