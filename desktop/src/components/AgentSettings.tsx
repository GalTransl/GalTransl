import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { AnimatedDialog } from './AnimatedDialog';
import { t } from '../i18n';
import { fetchAppSettings, updateAppSettings } from '../lib/api';
import { openExternalUrl } from '../lib/externalLink';
import { Icon } from './Icon';
import { Switch } from './Switch';
import { MemoryManager } from './MemoryManager';

export function AgentSettingsButton({ onClick, label, ready, updateAvailable = false }: {
  onClick: () => void;
  label?: string;
  ready?: boolean;
  updateAvailable?: boolean;
}) {
  return <button type="button" className="agent-settings-button" onClick={onClick}
    aria-haspopup="dialog" title={t('common:agentSettings.configure')}>
    <Icon name="settings" />
    <span>{label ?? t('common:agentSettings.configure')}</span>
    {ready !== undefined && <i className={ready && !updateAvailable ? 'is-ready' : ''} aria-hidden="true" />}
  </button>;
}

export function AgentSettingsDialog({ agent, backendUrl, projectDir = '', close, children, footer, className = '' }: {
  agent: 'translation' | 'extraction';
  backendUrl: string;
  projectDir?: string;
  close: () => void;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  const titleId = useId();
  return <AnimatedDialog backdropClassName="agent-settings-backdrop" className={`agent-settings-dialog ${className}`} labelledBy={titleId} close={close}>
    <header className="agent-settings-dialog__header">
      <h2 id={titleId}>{t(agent === 'translation'
        ? 'common:agentSettings.translationTitle' : 'common:agentSettings.extractionTitle')}</h2>
      <button type="button" onClick={close} aria-label={t('common:actions.close')}>
        <Icon name="close" />
      </button>
    </header>
    <div className="agent-settings-dialog__body">
      <AgentContextSettings agent={agent} backendUrl={backendUrl} />
      <AgentContextSettings agent={agent} backendUrl={backendUrl} plugin="agent-mnemon" />
      <MemoryManager backendUrl={backendUrl} projectDir={projectDir} />
      {children}
    </div>
    {footer && <footer className="agent-settings-dialog__footer">{footer}</footer>}
  </AnimatedDialog>;
}

export function AgentContextSettings({ agent, backendUrl, plugin = 'billion-context' }: {
  agent: 'translation' | 'extraction'; backendUrl: string; plugin?: 'billion-context' | 'agent-mnemon';
}) {
  const mnemon = plugin === 'agent-mnemon';
  const setting = mnemon
    ? (agent === 'translation' ? 'mnemonEnabled' : 'extractionMnemonEnabled')
    : (agent === 'translation' ? 'billionContextEnabled' : 'extractionBillionContextEnabled');
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [reload, setReload] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setLoading(true);
    setLoaded(false);
    setSaving(false);
    setError('');
    fetchAppSettings().then(settings => {
      if (generation.current !== current) return;
      setEnabled(settings[setting] === true);
      setLoaded(true);
    }).catch(error => {
      if (generation.current === current) setError(String(error));
    }).finally(() => {
      if (generation.current === current) setLoading(false);
    });
    return () => { generation.current++; };
  }, [backendUrl, setting, reload]);

  const change = (next: boolean) => {
    if (!loaded || loading || saving) return;
    const current = generation.current;
    setSaving(true);
    setError('');
    return updateAppSettings({ [setting]: next }).then(settings => {
      if (generation.current === current) setEnabled(settings[setting] === true);
    }).catch(error => {
      if (generation.current === current) setError(String(error));
    }).finally(() => {
      if (generation.current === current) setSaving(false);
    });
  };

  return <section className="agent-context-settings">
    <div className="agent-context-settings__header">
      <h3 className="agent-context-settings__title"><span className="agent-context-settings__icon"><Icon name={mnemon ? 'bookmark' : 'archive'} /></span>{t(mnemon ? 'common:mnemonSettings.contextTitle' : 'common:agentSettings.contextTitle')}</h3>
      <label className="agent-context-settings__toggle">
        <span>{plugin}</span>
        <Switch checked={enabled} onChange={next => { void change(next); }}
          disabled={!loaded || loading || saving} ariaLabel={t(mnemon ? 'common:mnemonSettings.enabled' : 'common:agentSettings.enabled')} />
      </label>
    </div>
    <p>{t(mnemon ? 'common:mnemonSettings.description' : 'common:agentSettings.description')}</p>
    <p className="agent-context-settings__hint">{t(mnemon ? 'common:mnemonSettings.timing' : 'common:agentSettings.timing')}</p>
    <p className="agent-context-settings__credits">
      {t('common:agentSettings.attribution')}{' '}
      <a href={mnemon ? 'https://github.com/omdsh-dev/dsh-mnemon' : 'https://github.com/ranxianglei/billion-context'} onClick={event => {
        event.preventDefault(); void openExternalUrl(event.currentTarget.href);
      }}>{mnemon ? 'dsh-mnemon' : plugin}</a>{!mnemon && <>{' / '}
      <a href="https://github.com/ranxianglei/acp-kernel" onClick={event => {
        event.preventDefault(); void openExternalUrl(event.currentTarget.href);
      }}>{'acp-kernel'}</a></>}
    </p>
    {error && <p className="agent-context-settings__error" role="alert">{error}</p>}
    {error && !loaded && <button type="button" onClick={() => setReload(value => value + 1)} disabled={loading}>
      {t('common:agentSettings.retry')}
    </button>}
  </section>;
}
