import { useNavigate } from 'react-router-dom';
import { t, useUiLanguage } from '../i18n';
import { Icon } from './Icon';

export function NewProjectWizardRef({ directory }: { directory: string }) {
  useUiLanguage();
  const navigate = useNavigate();
  return <button type="button" className="extract-project-wizard-ref" title={directory}
    onClick={() => navigate('/new-project', { state: { newProjectWizardDirectory: directory } })}>
    <Icon name="file-plus" /><span className="extract-project-wizard-ref__label">{t('extraction:createTranslationProject')}</span>
    <span className="extract-project-wizard-ref__path">{directory}</span>
  </button>;
}
