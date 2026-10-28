import { useId, useState, type FormEvent, type ReactNode } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
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
  const [runtime, setRuntime] = useState(initial);
  const [settings, setSettings] = useState(initial.settings);
  const [skill, setSkill] = useState<SkillStatus | null>(initial.skill);
  const [error, setError] = useState('');
  const [action, setAction] = useState<'' | 'saving' | 'checking' | 'installing'>('');
  const busy = action !== '';
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
  return <Dialog title={x('environment')} close={close}><form onSubmit={event => void submit(event)}>
    <p className="extract-muted">{x('requirements')}</p>
    <fieldset className="extract-environment-fields" disabled={busy}>
      <PathField label={x('skillDir')} value={settings.skill_dir} onChange={skill_dir => { setSettings({ ...settings, skill_dir }); setSkill(null); setError(''); }} />
      <div className="extract-skill-status" aria-live="polite">
        {skill?.installed && <p>{x('skillVersion')} <code title={skill.current_commit}>{skill.current_commit ? skill.current_commit.slice(0, 8) : x('skillUnknownVersion')}</code></p>}
        {skill?.installed === false && <p className="extract-warning">{x('missingSkill')}</p>}
        {skill?.latest_commit && <p className={skill.update_available ? 'extract-warning' : 'extract-muted'}>{x(skill.update_available ? 'skillUpdateAvailable' : 'skillUpToDate')} <code title={skill.latest_commit}>{skill.latest_commit.slice(0, 8)}</code></p>}
      </div>
      <div className="extract-skill-actions">
        <button type="button" disabled={busy || !settings.skill_dir.trim()} onClick={() => void checkSkill()}>{x(action === 'checking' ? 'skillChecking' : 'skillCheck')}</button>
        <button type="button" className="extract-primary" disabled={busy || Boolean(skill?.latest_commit && !skill.update_available)} onClick={() => void installSkill()}>{x(action === 'installing' ? 'skillInstalling' : skill?.installed ? 'skillUpdate' : 'skillDownload')}</button>
      </div>
      <p className="extract-muted">{x('skillDownloadHint')}</p>
      <PathField label={x('pythonPath')} directory={false} value={settings.python_path} onChange={python_path => setSettings({ ...settings, python_path })} />
    </fieldset>
    <p className="extract-muted">{runtime.python_version}</p>
    {runtime.problems.includes('python_runner_missing') && <p className="extract-warning">{x('setupRuntime')}</p>}
    {runtime.problems.some(code => code.startsWith('python_')) && <p className="extract-warning">{x('missingPython')}</p>}
    {error && <p role="alert" className="extract-error">{error}</p>}
    <footer><button type="button" disabled={busy} onClick={close}>{x('cancel')}</button><button className="extract-primary" disabled={busy}>{x('save')}</button></footer>
  </form></Dialog>;
}
