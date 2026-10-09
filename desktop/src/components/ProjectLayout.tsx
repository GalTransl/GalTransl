import { t as translate, useUiLanguage } from "../i18n";
import { Suspense, lazy, useEffect, useMemo } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { usePageActive } from './PageActivity';
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
  { path: 'translate', get label() { return translate("common:projectLayout.label_label_startTranslation"); } },
  { path: 'cache', get label() { return translate("common:projectLayout.label_label_text"); } },
  { path: 'config', get label() { return translate("common:projectLayout.label_label_configEdit"); } },
  { path: 'dictionary', get label() { return translate("common:projectLayout.label_label_projectDictionary"); } },
  { path: 'names', get label() { return translate("common:projectLayout.label_label_nameTableTranslation"); } },
];

/** Shared context passed to every child page */
export interface ProjectPageContext {
  projectDir: string;
  projectId: string;
  configFileName: string;
}

export function ProjectLayout() {
  const uiLanguage = useUiLanguage();
  const active = usePageActive();
  const { projectId } = useParams<{ projectId: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  const projectDir = projectId ? decodeProjectDir(projectId) : '';
  const configFileName = useMemo(() => loadConfigFileName(projectDir), [projectDir, active]);

  // Extract current tab from URL: /project/:projectId/cache → "cache"
  const segments = location.pathname.split('/');
  const currentTab = segments[3] || 'translate';

  // If accessing /project/:projectId without a tab, redirect to the last visited tab
  useEffect(() => {
    if (active && !segments[3]) {
      const lastTab = loadLastProjectTab(projectDir);
      navigate(location.pathname + '/' + lastTab, { replace: true });
    }
  }, [segments[3], location.pathname, navigate, projectDir, active]);

  const ctx: ProjectPageContext = useMemo(
    () => ({ projectDir, projectId: projectId || '', configFileName }),
    [projectDir, projectId, configFileName],
  );

  const activeTab = TAB_MAP.some((tab) => tab.path === currentTab) ? currentTab : 'translate';

  // Save the active tab whenever it changes
  useEffect(() => {
    if (active && projectDir && activeTab) {
      saveLastProjectTab(projectDir, activeTab);
    }
  }, [projectDir, activeTab, active]);

  // A redirect-only route must not mount a second hidden translation runner.
  if (!segments[3]) return null;

  return (
    <div className="project-layout">
      <Suspense fallback={<div className="inline-feedback">{translate("common:projectLayout.projectLayout_fallback_load")}</div>}>
        {activeTab === 'translate' ? <ProjectTranslatePage ctx={ctx} /> : null}
        {activeTab === 'config' ? <ProjectConfigPage ctx={ctx} /> : null}
        {activeTab === 'dictionary' ? (
          <div
            className="project-layout__keep-alive"
          >
            <ProjectDictionaryPage ctx={ctx} active={active} />
          </div>
        ) : null}
        {activeTab === 'names' ? (
          <div
            className="project-layout__keep-alive"
          >
            <ProjectNamePage ctx={ctx} active={active} />
          </div>
        ) : null}
        {activeTab === 'cache' ? (
          <div
            className="project-layout__keep-alive"
          >
            <ProjectCachePage ctx={ctx} active={active} />
          </div>
        ) : null}
      </Suspense>
    </div>
  );
}
