import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { t, useUiLanguage } from '../i18n';
import { addOpenProject, ApiError, encodeProjectDir, fetchProjectConfig } from '../lib/api';
import { addProjectToHistory } from '../pages/HomePage';
import { Icon } from './Icon';

export function NewProjectWizardRef({ directory }: { directory: string }) {
  useUiLanguage();
  const navigate = useNavigate();
  const request = useRef(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState('');
  const openProject = async () => {
    if (request.current) return;
    request.current = true; setOpening(true); setError('');
    try {
      const projectId = encodeProjectDir(directory);
      for (const config of ['config.yaml', 'config.yml']) {
        try { await fetchProjectConfig(projectId, config); }
        catch (error) {
          if (error instanceof ApiError && error.status === 404) continue;
          throw error;
        }
        addOpenProject(directory, config);
        addProjectToHistory(directory, config);
        navigate(`/project/${projectId}/translate`);
        return;
      }
      navigate('/new-project', { state: { newProjectWizardDirectory: directory } });
    } catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { request.current = false; setOpening(false); }
  };
  return <><button type="button" className="extract-project-wizard-ref" title={directory}
    disabled={opening} aria-busy={opening} onClick={() => void openProject()}>
    <Icon name="file-plus" /><span className="extract-project-wizard-ref__label">{t('extraction:createTranslationProject')}</span>
    <span className="extract-project-wizard-ref__path">{directory}</span>
  </button>{error && <span className="extract-error" role="alert">{error}</span>}</>;
}
