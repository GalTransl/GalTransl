import { Outlet, useParams } from 'react-router-dom';
import { useMemo } from 'react';
import { decodeProjectDir } from '../lib/api';

const CONFIG_FILE_KEY = 'galtransl-config-file';

function loadConfigFileName(projectDir: string): string {
  try {
    const map = JSON.parse(localStorage.getItem(CONFIG_FILE_KEY) || '{}');
    return map[projectDir] || 'config.yaml';
  } catch {
    return 'config.yaml';
  }
}

export function ProjectLayout() {
  const { projectId } = useParams<{ projectId: string }>();

  const projectDir = projectId ? decodeProjectDir(projectId) : '';
  const configFileName = useMemo(() => loadConfigFileName(projectDir), [projectDir]);

  return (
    <div className="project-layout">
      <Outlet context={{ projectDir, projectId, configFileName }} />
    </div>
  );
}
