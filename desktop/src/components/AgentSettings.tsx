import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { t } from '../i18n';
import { fetchAppSettings, updateAppSettings } from '../lib/api';
import { openExternalUrl } from '../lib/externalLink';
import { Icon } from './Icon';
import { Switch } from './Switch';

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

export function AgentSettingsDialog({ agent, backendUrl, close, children, footer, className = '' }: {
  agent: 'translation' | 'extraction';
  backendUrl: string;
  close: () => void;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  const titleId = useId();
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') close(); };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [close]);
  return createPortal(<div className="agent-settings-backdrop" onMouseDown={event => {
    if (event.target === event.currentTarget) close();
  }}>
    <section className={`agent-settings-dialog ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <header className="agent-settings-dialog__header">
        <h2 id={titleId}>{t(agent === 'translation'
          ? 'common:agentSettings.translationTitle' : 'common:agentSettings.extractionTitle')}</h2>
        <button type="button" autoFocus onClick={close} aria-label={t('common:actions.close')}>
          <Icon name="close" />
        </button>
      </header>
      <div className="agent-settings-dialog__body">
        <AgentContextSettings agent={agent} backendUrl={backendUrl} />
        {children}
      </div>
      {footer && <footer className="agent-settings-dialog__footer">{footer}</footer>}
    </section>
  </div>, document.body);
}

export function AgentContextSettings({ agent, backendUrl }: { agent: 'translation' | 'extraction'; backendUrl: string }) {
  const setting = agent === 'translation' ? 'billionContextEnabled' : 'extractionBillionContextEnabled';
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
      <h3 className="agent-context-settings__title"><span className="agent-context-settings__icon"><Icon name="archive" /></span>{t('common:agentSettings.contextTitle')}</h3>
      <label className="agent-context-settings__toggle">
        <span>{'billion-context'}</span>
        <Switch checked={enabled} onChange={next => { void change(next); }}
          disabled={!loaded || loading || saving} ariaLabel={t('common:agentSettings.enabled')} />
      </label>
    </div>
    <p>{t('common:agentSettings.description')}</p>
    <p className="agent-context-settings__hint">{t('common:agentSettings.timing')}</p>
    <p className="agent-context-settings__credits">
      {t('common:agentSettings.attribution')}{' '}
      <a href="https://github.com/ranxianglei/billion-context" onClick={event => {
        event.preventDefault(); void openExternalUrl(event.currentTarget.href);
      }}>{'billion-context'}</a>{' / '}
      <a href="https://github.com/ranxianglei/acp-kernel" onClick={event => {
        event.preventDefault(); void openExternalUrl(event.currentTarget.href);
      }}>{'acp-kernel'}</a>
    </p>
    {error && <p className="agent-context-settings__error" role="alert">{error}</p>}
    {error && !loaded && <button type="button" onClick={() => setReload(value => value + 1)} disabled={loading}>
      {t('common:agentSettings.retry')}
    </button>}
  </section>;
}
