import { useId, useState, type FormEvent, type ReactNode } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
import { AgentSettingsDialog } from '../../components/AgentSettings';
import { CustomSelect } from '../../components/CustomSelect';
import { Icon } from '../../components/Icon';
import { useConnection } from '../../features/connection/ConnectionContext';
import { useUiLanguage } from '../../i18n';
import { extractionApi } from './api';
import { x } from './labels';
import type { Environment, SkillStatus } from './types';
import { useDirectoryDrop } from './useDirectoryDrop';

export function Dialog({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  return <div className="extract-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}>
    <section className="extract-dialog" role="dialog" aria-modal="true" aria-label={title} onKeyDown={event => { if (event.key === 'Escape') close(); }}>
      <header><h2>{title}</h2><button type="button" onClick={close} aria-label={x('close')}>×</button></header>{children}
    </section>
  </div>;
}
export function PathField({ label, value, onChange, directory = true }: { label: string; value: string; onChange: (value: string) => void; directory?: boolean }) {
  const id = useId();
  const [error, setError] = useState('');
  const browse = async () => {
    try {
      const result = await open({ directory, multiple: false });
      if (typeof result === 'string') onChange(result);
    } catch (error) { setError(error instanceof Error ? error.message : String(error)); }
  };
  return <div className="extract-field"><label htmlFor={id}>{label}</label><div><input id={id} value={value} onChange={event => onChange(event.target.value)} autoComplete="off" /><button type="button" onClick={() => void browse()}>{x('browse')}</button></div>{error && <small role="alert">{error}</small>}</div>;
}
export function CreateDialog({ close, create, busy }: { close: () => void; create: (game: string, output: string) => Promise<void>; busy: boolean }) {
  useUiLanguage();
  const [game, setGame] = useState('');
  const [output, setOutput] = useState('');
  const directoryDrop = useDirectoryDrop(setGame, busy);
  return <Dialog title={x('newSession')} close={close}><form onSubmit={event => { event.preventDefault(); void create(game, output); }}>
    <div ref={directoryDrop.target} className={`extract-directory-drop${directoryDrop.dragging ? ' is-dragging' : ''}`} {...directoryDrop.handlers}>
      <PathField label={x('gameDir')} value={game} onChange={setGame} />
      <p className="extract-muted">{x('directoryDropHint')}</p>
      {directoryDrop.error && <p className="extract-error" role="alert">{directoryDrop.error}</p>}
    </div>
    <label className="extract-field"><span>{x('outputDir')}</span><input value={output} onChange={event => setOutput(event.target.value)} placeholder={x('outputHint')} autoComplete="off" /></label>
    <p className="extract-muted">{x('newOutput')}</p>
    <footer><button type="button" onClick={close}>{x('cancel')}</button><button className="extract-primary" disabled={!game.trim() || busy}>{x('create')}</button></footer>
  </form></Dialog>;
}
export function EnvironmentDialog({ initial, close, saved }: { initial: Environment; close: () => void; saved: (environment: Environment) => void }) {
  useUiLanguage();
  const connection = useConnection();
  const pythonId = useId();
  const formId = useId();
  const [manualPython, setManualPython] = useState(false);
  const [runtime, setRuntime] = useState(initial);
  const [settings, setSettings] = useState(initial.settings);
  const [skill, setSkill] = useState<SkillStatus | null>(initial.skill);
  const [error, setError] = useState('');
  const [action, setAction] = useState<'' | 'saving' | 'checking' | 'installing' | 'detecting'>('');
  const busy = action !== '';
  const refreshPythons = () => {
    setAction('detecting'); setError('');
    return extractionApi<Environment>('environment').then(setRuntime)
      .catch(error => setError(error instanceof Error ? error.message : String(error)))
      .finally(() => setAction(''));
  };
  const checkSkill = async () => {
    setAction('checking'); setError('');
    try { setSkill(await extractionApi<SkillStatus>('skill-check', {}, { skill_dir: settings.skill_dir })); }
    catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setAction(''); }
  };
  const installSkill = async () => {
    setAction('installing'); setError('');
    try {
      const result = await extractionApi<Environment>('skill-install', {}, {});
      setRuntime(result);
      setSettings({ ...settings, skill_dir: result.settings.skill_dir });
      setSkill({ ...result.skill, latest_commit: result.skill.current_commit, update_available: false });
      saved(result);
    } catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setAction(''); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setAction('saving'); setError('');
    try { const result = await extractionApi<Environment>('settings', {}, settings); saved(result); close(); }
    catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setAction(''); }
  };
  return <AgentSettingsDialog agent="extraction" backendUrl={connection.backendUrl} close={close}
    className="extract-environment-dialog" footer={<>
      <span className="extract-environment-save-hint">{x('runtimeSaveHint')}</span>
      <button type="button" disabled={busy} onClick={close}>{x('cancel')}</button>
      <button type="submit" form={formId} className="extract-primary" disabled={busy}>{x('save')}</button>
    </>}>
    <form id={formId} onSubmit={event => void submit(event)}>
      <fieldset className="extract-environment-fields" disabled={busy}>
        <section className="extract-environment-card">
          <header className="extract-environment-card__header">
            <span className="extract-environment-card__icon"><Icon name="folder" /></span>
            <h3>{x('skillSectionTitle')}</h3>
            <span className={`extract-environment-badge ${skill?.installed ? 'is-ready' : ''}`}>
              {x(skill ? skill.installed ? 'skillReady' : 'skillNotReady' : 'skillUnchecked')}
            </span>
          </header>
          <p className="extract-environment-description">{x('skillSectionDescription')}</p>
          <PathField label={x('skillDir')} value={settings.skill_dir} onChange={skill_dir => { setSettings({ ...settings, skill_dir }); setSkill(null); setError(''); }} />
          <div className="extract-skill-toolbar">
            <div className="extract-skill-status" aria-live="polite">
              {skill?.installed && <div className="extract-environment-version"><span>{x('skillVersion')}</span><code title={skill.current_commit}>{skill.current_commit ? skill.current_commit.slice(0, 8) : x('skillUnknownVersion')}</code></div>}
              {skill?.installed === false && <p className="extract-warning">{x('missingSkill')}</p>}
            </div>
            <div className="extract-skill-actions">
              <button type="button" disabled={busy || !settings.skill_dir.trim()} onClick={() => void checkSkill()}>{x(action === 'checking' ? 'skillChecking' : 'skillCheck')}</button>
              <button type="button" className="extract-primary" disabled={busy || Boolean(skill?.latest_commit && !skill.update_available)} onClick={() => void installSkill()}>{x(action === 'installing' ? 'skillInstalling' : skill?.installed ? 'skillUpdate' : 'skillDownload')}</button>
            </div>
          </div>
          {skill?.latest_commit && <p className={skill.update_available ? 'extract-warning' : 'extract-muted'}>{x(skill.update_available ? 'skillUpdateAvailable' : 'skillUpToDate')} <code title={skill.latest_commit}>{skill.latest_commit.slice(0, 8)}</code></p>}
          <p className="extract-muted">{x('skillDownloadHint')}</p>
        </section>
        <section className="extract-environment-card">
          <header className="extract-environment-card__header">
            <span className="extract-environment-card__icon"><Icon name="settings" /></span>
            <h3>{x('pythonSectionTitle')}</h3>
          </header>
          <div className="extract-python-builtin">
            <div><strong>{x('pythonAppInterpreter')}</strong><p>{x('pythonAppDescription')}</p></div>
            <span className="extract-environment-badge">{runtime.python_version}</span>
          </div>
          {runtime.problems.includes('python_runner_missing') && <p className="extract-warning">{x('setupRuntime')}</p>}
          {runtime.problems.some(code => code.startsWith('python_')) && <p className="extract-warning">{x('missingPython')}</p>}
          <div className="extract-field">
            <label htmlFor={pythonId}>{x('pythonInterpreterLabel')}</label>
            <div className="extract-python-picker">
              <CustomSelect id={pythonId} className="extract-python-select" value={manualPython ? 'custom' : settings.python_path}
                disabled={busy} aria-label={x('pythonInterpreterLabel')} onChange={event => {
                  setManualPython(event.target.value === 'custom');
                  if (event.target.value === 'custom') return;
                  setSettings({ ...settings, python_path: event.target.value });
                }}>
                <option value="">{x('pythonAutomatic')}</option>
                {runtime.system_pythons.map(python => <option key={python.executable} value={python.executable}>
                  {x('pythonInterpreter', { version: python.version, architecture: python.architecture, path: python.executable })}
                </option>)}
                {settings.python_path && !runtime.system_pythons.some(python => python.executable === settings.python_path)
                  && <option value={settings.python_path}>{x('pythonCustom')}{' · '}{settings.python_path}</option>}
                <option value="custom">{x('pythonOtherPath')}</option>
              </CustomSelect>
              <button type="button" disabled={busy} onClick={() => void refreshPythons()}>
                {x(action === 'detecting' ? 'pythonDetecting' : 'pythonRefresh')}
              </button>
            </div>
            {settings.python_path && <div className="extract-python-selection">
              <span>{x('pythonSelectedPath')}</span><p className="extract-python-path">{settings.python_path}</p>
            </div>}
            {!settings.python_path && !runtime.system_pythons.length && <p className="extract-muted">{x('pythonNone')}</p>}
          </div>
          {manualPython && <PathField label={x('pythonPath')} directory={false} value={settings.python_path}
            onChange={python_path => setSettings({ ...settings, python_path })} />}
          <p className="extract-muted">{x('pythonLocalDescription')}</p>
        </section>
      </fieldset>
      {error && <p role="alert" className="extract-error">{error}</p>}
    </form>
  </AgentSettingsDialog>;
}
