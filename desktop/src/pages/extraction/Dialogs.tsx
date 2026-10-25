import { useId, useState, type FormEvent, type ReactNode } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
import { useUiLanguage } from '../../i18n';
import { extractionApi } from './api';
import { x } from './labels';
import type { Environment } from './types';
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
  const [settings, setSettings] = useState(initial.settings);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError('');
    try { const result = await extractionApi<Environment>('settings', {}, settings); saved(result); close(); }
    catch (error) { setError(error instanceof Error ? error.message : String(error)); }
    finally { setSaving(false); }
  };
  return <Dialog title={x('environment')} close={close}><form onSubmit={event => void submit(event)}>
    <p className="extract-muted">{x('requirements')}</p>
    <PathField label={x('skillDir')} value={settings.skill_dir} onChange={skill_dir => setSettings({ ...settings, skill_dir })} />
    <PathField label={x('pythonPath')} directory={false} value={settings.python_path} onChange={python_path => setSettings({ ...settings, python_path })} />
    <p className="extract-muted">{initial.python_version}</p>
    {initial.problems.some(code => ['python_runner_missing'].includes(code)) && <p className="extract-warning">{x('setupRuntime')}</p>}
    {initial.problems.includes('skill_missing') && <p className="extract-warning">{x('missingSkill')}</p>}
    {initial.problems.some(code => code.startsWith('python_')) && <p className="extract-warning">{x('missingPython')}</p>}
    {error && <p role="alert" className="extract-error">{error}</p>}
    <footer><button type="button" onClick={close}>{x('cancel')}</button><button className="extract-primary" disabled={saving}>{x('save')}</button></footer>
  </form></Dialog>;
}
