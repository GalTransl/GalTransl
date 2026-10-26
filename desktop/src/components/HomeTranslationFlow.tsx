import { Link } from 'react-router-dom';
import { t as translate } from '../i18n';
import { Icon, type IconName } from './Icon';

export function HomeTranslationFlow() {
  const extractionAgent = translate('projects:homePage.translationFlow.extractionAgent');
  const steps: { label: string; icon: IconName; action?: string; actionIcon?: IconName; agent?: string; agentIcon?: IconName; to?: string }[] = [
    {
      label: translate('projects:homePage.translationFlow.gameFiles'), icon: 'folder',
      action: translate('projects:homePage.translationFlow.extract'), agent: extractionAgent, to: '/extraction',
      actionIcon: 'tool', agentIcon: 'agent-extract',
    },
    {
      label: translate('projects:homePage.translationFlow.japaneseDialogue'), icon: 'file-text',
      action: translate('projects:homePage.translationFlow.translate'),
      agent: translate('projects:homePage.translationFlow.translationAgent'), to: '/agent',
      actionIcon: 'globe', agentIcon: 'agent-translate',
    },
    {
      label: translate('projects:homePage.translationFlow.chineseDialogue'), icon: 'file-text',
      action: translate('projects:homePage.translationFlow.writeBack'), agent: extractionAgent, to: '/extraction',
      actionIcon: 'tool', agentIcon: 'agent-extract',
    },
    { label: translate('projects:homePage.translationFlow.gamePatch'), icon: 'check' },
  ];

  return (
    <div className="home-translation-flow">
      <ol className="home-translation-flow__steps">
        {steps.map((step, index) => (
          <li key={index}>
            <div className="home-translation-flow__node">
              <span className={`home-translation-flow__icon${index === steps.length - 1 ? ' is-final' : ''}`} aria-hidden>
                <Icon name={step.icon} />
              </span>
              <span>{step.label}</span>
            </div>
            {step.to && (
              <div className="home-translation-flow__transition">
                <span className="home-translation-flow__arrow" aria-hidden />
                <div className="home-translation-flow__action">
                  <span className="home-translation-flow__method">
                    {step.actionIcon && <Icon name={step.actionIcon} size={14} />}
                    {step.action}
                  </span>
                  <span>{translate('projects:homePage.translationFlow.or')}</span>
                  <Link className="home-translation-flow__agent" to={step.to}>
                    {step.agentIcon && <Icon name={step.agentIcon} size={14} />}
                    {step.agent}
                  </Link>
                </div>
              </div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
