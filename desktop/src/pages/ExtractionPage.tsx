import '../styles/pages/agent.css';
import '../styles/pages/extraction.css';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../components/Icon';
import { AgentSettingsButton } from '../components/AgentSettings';
import { useUiLanguage } from '../i18n';
import { addOpenProject, encodeProjectDir, getBackendProfile } from '../lib/api';
import { basenamePath } from '../lib/paths';
import { addProjectToHistory } from './HomePage';
import { Conversation, PendingRequest } from './extraction/Conversation';
import { CreateDialog, EnvironmentDialog } from './extraction/Dialogs';
import { Workspace } from './extraction/Workspace';
import { x, statusLabel } from './extraction/labels';
import { isRunning, type Session } from './extraction/types';
import { useExtraction } from './extraction/useExtraction';
import { moveMessageDraft, useMessageDraft } from './agent/drafts';
import { ExtractionComposer } from './extraction/Composer';
import { useImageInput, imageUploads } from './agent/images';
import { useModelProfile } from './extraction/useModelProfile';
import { gameFolderKey, SessionFolders } from './extraction/SessionFolders';
import { StatusPill } from './agent/composer';
import { MessageQueue } from './agent/MessageQueue';

const DRAFT_SCOPE = 'galtransl-extraction';

export function ExtractionPage() {
  useUiLanguage();
  const agent = useExtraction();
  const navigate = useNavigate();
  const [mode, setMode] = useState('auto');
  const [editingQueued, setEditingQueued] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => setEditingQueued(null), [agent.selected]);
  const [dialog, setDialog] = useState<'create' | 'environment' | null>(null);
  const [draft, setDraft] = useMessageDraft(DRAFT_SCOPE, agent.selected);
  const [showWorkspace, setShowWorkspace] = useState(() => window.innerWidth >= 1440);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const imageInput = useImageInput(`extraction::${agent.selected}`, textarea, agent.busy || dialog !== null);
  const session = agent.view?.session;
  const selection = useRef({ id: agent.selected, session }); selection.current = { id: agent.selected, session };
  const { models: modelNames, profile, boundProfile, defaultProfile, setProfile, moveProfile } = useModelProfile(session, agent.selected);
  const running = isRunning(session);
  const toolCount = agent.view?.rows.filter(row => row.role === 'tool' && row.done).length || 0;
  useEffect(() => { if (session) setMode(session.permission_mode || 'auto'); }, [session?.id, session?.permission_mode]);
  const changeMode = (value: string) => {
    if (session) void agent.mutate('permission-mode', { permission_mode: value });
    else setMode(value);
  };
  const showHome = () => { setDialog(null); agent.select(''); };
  const create = async (game: string, output: string) => {
    const result = await agent.mutate<{ session: Session }>('create', { game_dir: game, output_dir: output });
    if (result) {
      moveProfile(result.session.id);
      moveMessageDraft(DRAFT_SCOPE, agent.selected, result.session.id);
      imageInput.move(`extraction::${result.session.id}`);
      agent.select(result.session.id); setDialog(null);
    }
  };
  const createInFolder = async (game: string) => {
    const result = await agent.mutate<{ session: Session }>('create', { game_dir: game, output_dir: '' });
    if (result) {
      moveProfile(result.session.id);
      agent.select(result.session.id); setDialog(null);
    }
  };
  const deleteSession = async (item: Session) => {
    if (agent.busy || isRunning(item.id === session?.id ? session : item)) return;
    if (!window.confirm(`${x('confirmDeleteSession')}\n${item.title}`)) return;
    const result = await agent.mutate('delete', { session_id: item.id });
    if (result && selection.current.id === item.id) {
      agent.select(agent.sessions.sessions.find(row => row.id !== item.id && gameFolderKey(row.game_dir) === gameFolderKey(item.game_dir))?.id || '');
    }
  };
  const closeFolder = async (directory: string) => {
    const key = gameFolderKey(directory);
    if (agent.busy || agent.sessions.running_game_dirs?.some(directory => gameFolderKey(directory) === key)
      || agent.sessions.sessions.some(row => gameFolderKey(row.game_dir) === key && isRunning(row.id === session?.id ? session : row))) return;
    if (!window.confirm(`${x('confirmCloseFolder')}\n${directory}`)) return;
    const result = await agent.mutate('close-folder', { game_dir: directory });
    const current = selection.current;
    const currentSession = current.session?.id === current.id ? current.session : agent.sessions.sessions.find(row => row.id === current.id);
    if (result && currentSession && gameFolderKey(currentSession.game_dir) === key) agent.select('');
  };
  const send = async () => {
    const message = draft.trim();
    const sentImages = imageInput.images;
    if ((!message && !sentImages.length) || imageInput.processing || agent.busy || !profile || !agent.environment?.ready) return;
    if (!session) { setDialog('create'); return; }
    const result = await agent.mutate('message', { message, images: imageUploads(sentImages), profile: getBackendProfile(profile), profile_name: boundProfile, permission_mode: mode });
    if (result) {
      imageInput.removeSent(sentImages);
      setDraft(current => current === draft ? '' : current);
    }
  };
  const importProject = async () => {
    const result = await agent.mutate<{ project_dir: string; config_file_name: string | null }>('import', {});
    if (!result) return;
    if (!result.config_file_name) {
      navigate('/new-project', { state: { newProjectWizardDirectory: result.project_dir } });
      return;
    }
    addOpenProject(result.project_dir, result.config_file_name);
    addProjectToHistory(result.project_dir, result.config_file_name);
    navigate(`/project/${encodeProjectDir(result.project_dir)}/translate`);
  };
  return <div className={`agent-console extract-page ${showWorkspace && session ? 'extract-page--workspace' : ''}`}>
    <aside className="agent-sessions extract-sessions">
      <header className="agent-sessions__head"><span className="agent-sessions__title">{x('sessions')} · {agent.sessions.total}</span>
        <button type="button" className="agent-sessions__new" disabled={agent.busy} onClick={showHome} title={x('newSession')} aria-label={x('newSession')}>＋</button>
      </header>
      <nav className="agent-sessions__list" aria-label={x('sessions')}>
      <SessionFolders sessions={agent.sessions.sessions} selected={agent.selected} activeSession={session} onSelect={agent.select}
        onCreateInFolder={game => void createInFolder(game)} onDeleteSession={item => void deleteSession(item)}
        onCloseFolder={directory => void closeFolder(directory)} runningDirectories={agent.sessions.running_game_dirs} busy={agent.busy} />
      {!agent.sessions.total && <div className="agent-sessions__empty">{x('emptySessions')}</div>}
      {agent.sessions.next_offset != null && <button type="button" className="agent-sessions__show-more" onClick={() => void agent.refreshSessions(agent.sessions.next_offset!).catch(e => agent.setError(String(e)))}>{x('more')}</button>}</nav>
      <AgentSettingsButton ready={agent.environment?.ready ?? false} updateAvailable={agent.environment?.skill.update_available}
        label={x(agent.environment?.skill.update_available ? 'skillHasUpdate' : agent.environment?.ready ? 'ready' : 'setup')}
        onClick={() => setDialog('environment')} />
    </aside>
    <div className="extract-content">
      <header className="agent-console__bar">
        <div className="agent-console__bar-left">
          <span className="agent-console__avatar" aria-hidden><Icon name="agent-extract" /></span>
          <div className="agent-console__bar-copy"><div className="agent-console__bar-title"><span className="agent-console__bar-name">{x('title')}</span></div>
            <div className="agent-console__project-static">{session ? <><span className="agent-console__project-name">{basenamePath(session.game_dir) || session.game_dir}</span><span className="agent-console__project-path" title={session.game_dir}>{session.game_dir}</span></> : <span className="agent-console__project-empty">{x('subtitle')}</span>}</div>
          </div>
        </div>
        <div className="agent-console__bar-right">
          <StatusPill status={session?.status || 'idle'} running={running} label={statusLabel(session?.status || 'idle')} />
          <button type="button" className="agent-console__icon-btn" disabled={agent.busy} onClick={showHome} title={x('newSession')} aria-label={x('newSession')}><Icon name="pencil" /></button>
          {session && <button type="button" className={`agent-console__icon-btn extract-workspace-toggle${showWorkspace ? ' is-active' : ''}`} onClick={() => setShowWorkspace(value => !value)} title={x('showWorkspace')} aria-label={x('showWorkspace')} aria-pressed={showWorkspace} aria-expanded={showWorkspace} aria-controls="extraction-workspace"><Icon name="folder" /></button>}
        </div>
      </header>
      <main className="agent-console__main agent-cockpit extract-main">
      {!session || !agent.view?.rows.length ? <div className="agent-console__thread extract-welcome"><div className="agent-thread"><div className="agent-hero">
        <div className="agent-hero__mark"><Icon name="agent-extract" /></div><h2 className="agent-hero__title">{session ? session.title : x('startTitle')}</h2><p className="agent-hero__subtitle">{x('startText')}</p>
        <div className="agent-hero__steps">{(['quickExtract', 'quickCheck', 'quickReinsert'] as const).map(key => <button type="button" className="agent-hero__step" key={key} onClick={() => { setDraft(x(key)); if (!session) setDialog('create'); else textarea.current?.focus(); }}><span className="agent-hero__step-icon"><Icon name="sparkle" /></span>{x(key)}</button>)}</div>
        <div className="agent-hero__project-panel">{session ? <div className="extract-welcome-source" title={session.game_dir}><Icon name="folder" />{session.game_dir}</div>
          : <><span className="agent-hero__open-projects-label">{x('chooseSource')}</span><div className="agent-hero__actions"><button type="button" className="agent-hero__action" disabled={agent.busy} onClick={() => setDialog('create')}><Icon name="folder" />{x('newSession')}</button></div></>}
        </div>
      </div></div></div> : <Conversation rows={agent.view.rows} motionEvents={agent.view.motionEvents} sessionId={session.id} running={running} status={session.status} pending={agent.view.pending} older={agent.view.has_more} beforeSeq={agent.view.before_seq} loadingOlder={agent.loadingOlder} activityPrefix={agent.view.activity_prefix} loadOlder={agent.loadOlder} />}
      <div className="agent-console__composer extract-bottom">
        {agent.error && <div className="agent-notice agent-notice--error extract-inline-error" role="alert"><Icon name="warning" /><span>{agent.error}</span><button type="button" className="extract-text-button" onClick={agent.reload}>{x('reload')}</button></div>}
        {agent.view?.pending && <PendingRequest key={agent.view.pending.requestId} pending={agent.view.pending} busy={agent.busy} answer={body => { void agent.mutate('answer', body); }} />}
        <MessageQueue queued={agent.view?.queued || []} editing={editingQueued} onEditingChange={setEditingQueued} busy={agent.busy}
          scope={{ sessionId: agent.selected, extraction: true }}
          onSave={() => { if (editingQueued) void agent.mutate('queue-update', { item_id: editingQueued.id, text: editingQueued.text }).then(result => { if (result) setEditingQueued(null); }); }}
          onDelete={id => { void agent.mutate('queue-delete', { item_id: id }).then(result => { if (result && editingQueued?.id === id) setEditingQueued(null); }); }}
          onSend={id => { void agent.mutate('queue-send', { item_id: id, profile: getBackendProfile(profile), profile_name: boundProfile, permission_mode: mode }).then(result => { if (result) setEditingQueued(null); }); }} />
        <ExtractionComposer draft={draft} setDraft={setDraft} textarea={textarea} session={session} running={running} busy={agent.busy} ready={Boolean(agent.environment?.ready)}
          imageInput={imageInput} hasQueue={Boolean(agent.view?.queued?.length)}
          profile={profile} profileSelection={boundProfile} defaultProfile={defaultProfile} setProfile={setProfile} models={modelNames} mode={mode} setMode={changeMode} context={agent.view?.contextUsage} compacting={agent.view?.compacting}
          send={() => void send()} stop={() => { void agent.mutate('stop', {}); }} chooseSource={() => setDialog('create')} configure={() => navigate('/backend-profiles')} />
        {!modelNames.length && <div className="extract-hint">{x('noModel')} <button type="button" className="extract-text-button" onClick={() => navigate('/backend-profiles')}>{x('configureModel')}</button></div>}
        {session && !agent.environment?.ready && <div className="extract-hint"><button type="button" className="extract-text-button" onClick={() => setDialog('environment')}>{x('setup')}</button></div>}
      </div>
    </main>
      {session && <div id="extraction-workspace" className="extract-workspace-shell" aria-hidden={!showWorkspace} {...(!showWorkspace ? { inert: '' } : {})}>
        <Workspace key={session.id} session={session} refresh={toolCount} running={running} importProject={() => void importProject()} busy={agent.busy} />
      </div>}
    </div>
    {dialog === 'create' && <CreateDialog close={() => setDialog(null)} create={create} busy={agent.busy} />}
    {dialog === 'environment' && <EnvironmentDialog initial={agent.environment} refresh={agent.refreshEnvironment} close={() => setDialog(null)} saved={agent.setEnvironment} />}
  </div>;
}
