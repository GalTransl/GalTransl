import '../styles/pages/agent.css';
import '../styles/pages/extraction.css';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Icon } from '../components/Icon';
import { AgentSettingsButton } from '../components/AgentSettings';
import { AnimatePresence } from '../components/UiMotion';
import { useUiLanguage } from '../i18n';
import { addOpenProject, encodeProjectDir, getBackendProfile } from '../lib/api';
import { forgetAgentSessions } from '../lib/agentActivity';
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
import { useDirectoryDrop } from './extraction/useDirectoryDrop';
import { firstSessionInFolder, gameFolderKey, SessionFolders } from './extraction/SessionFolders';
import { StatusPill } from './agent/composer';
import { liveActivityDisclosures } from './agent/uiState';
import { MessageQueue } from './agent/MessageQueue';

const DRAFT_SCOPE = 'galtransl-extraction';
/** 工作区开合只跟随用户的点击：默认收起，记在 localStorage 里。页面是路由级组件，
 *  切页面再回来会整个重建，若初始值按窗口宽度推断，宽屏用户每次回来都会被"自动弹出"。 */
const WORKSPACE_KEY = 'galtransl-extraction-workspace';
const rememberedWorkspace = () => { try { return localStorage.getItem(WORKSPACE_KEY) === '1'; } catch { return false; } };
const rememberWorkspace = (open: boolean) => { try { localStorage.setItem(WORKSPACE_KEY, open ? '1' : '0'); } catch { /* Toggle still works without storage. */ } };

export function ExtractionPage() {
  useUiLanguage();
  const agent = useExtraction();
  const navigate = useNavigate();
  const [mode, setMode] = useState('auto');
  const [editingQueued, setEditingQueued] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => setEditingQueued(null), [agent.selected]);
  const [dialog, setDialog] = useState<'create' | 'environment' | null>(null);
  const [dropped, setDropped] = useState({ game: '', error: '' });
  const openCreate = (game = '', error = '') => { setDropped({ game, error }); setDialog('create'); };
  const [draft, setDraft] = useMessageDraft(DRAFT_SCOPE, agent.selected);
  const [showWorkspace, setShowWorkspace] = useState(rememberedWorkspace);
  const toggleWorkspace = () => { const next = !showWorkspace; rememberWorkspace(next); setShowWorkspace(next); };
  const textarea = useRef<HTMLTextAreaElement>(null);
  const imageInput = useImageInput(`extraction::${agent.selected}`, textarea, agent.busy || dialog !== null);
  // A folder dropped anywhere on the page jumps to its first session when the game is already open,
  // otherwise opens the new-extraction dialog prefilled; the composer keeps its image drop.
  const pageDrop = useDirectoryDrop(path => {
    const existing = firstSessionInFolder(agent.sessions.sessions, path);
    if (existing) agent.select(existing.id); else openCreate(path);
  }, agent.busy || dialog !== null, { exclude: '.agent-composer', onError: message => openCreate('', message) });
  const session = agent.view?.session;
  const selection = useRef({ id: agent.selected, session }); selection.current = { id: agent.selected, session };
  const { models: modelNames, profile, boundProfile, defaultProfile, setProfile, moveProfile } = useModelProfile(session, agent.selected);
  const running = isRunning(session);
  const ready = Boolean(agent.environment?.ready && agent.environment.skill.installed);
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
    if (result) forgetAgentSessions('extraction', [item.id]);
    if (result && selection.current.id === item.id) {
      agent.select(agent.sessions.sessions.find(row => row.id !== item.id && gameFolderKey(row.game_dir) === gameFolderKey(item.game_dir))?.id || '');
    }
  };
  const closeFolder = async (directory: string) => {
    const key = gameFolderKey(directory);
    if (agent.busy || agent.sessions.running_game_dirs?.some(directory => gameFolderKey(directory) === key)
      || agent.sessions.sessions.some(row => gameFolderKey(row.game_dir) === key && isRunning(row.id === session?.id ? session : row))) return;
    if (!window.confirm(`${x('confirmCloseFolder')}\n${directory}`)) return;
    const folderSessions = agent.sessions.sessions.filter(row => gameFolderKey(row.game_dir) === key).map(row => row.id);
    const result = await agent.mutate('close-folder', { game_dir: directory });
    if (result) forgetAgentSessions('extraction', folderSessions);
    const current = selection.current;
    const currentSession = current.session?.id === current.id ? current.session : agent.sessions.sessions.find(row => row.id === current.id);
    if (result && currentSession && gameFolderKey(currentSession.game_dir) === key) agent.select('');
  };
  const send = async () => {
    const message = draft.trim();
    const sentImages = imageInput.images;
    if ((!message && !sentImages.length) || imageInput.processing || agent.busy || !profile || !ready) return;
    if (!session) { openCreate(); return; }
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
  return <div ref={pageDrop.target} className={`agent-console extract-page ${showWorkspace && session ? 'extract-page--workspace' : ''}`} {...pageDrop.handlers}>
    <aside className="agent-sessions extract-sessions">
      <header className="agent-sessions__head"><span className="agent-sessions__title">{x('sessions')} · {agent.sessions.total}</span>
        <button type="button" className="agent-sessions__new" disabled={agent.busy || !agent.selected} onClick={showHome} title={x('newSession')} aria-label={x('newSession')}><Icon name="plus" size={12} /></button>
      </header>
      <nav className="agent-sessions__list" aria-label={x('sessions')}>
      <SessionFolders sessions={agent.sessions.sessions} selected={agent.selected} activeSession={session} onSelect={agent.select}
        onCreateInFolder={game => void createInFolder(game)} onDeleteSession={item => void deleteSession(item)}
        onCloseFolder={directory => void closeFolder(directory)} runningDirectories={agent.sessions.running_game_dirs} busy={agent.busy} />
      {!agent.sessions.total && <div className="agent-sessions__empty">{x('emptySessions')}</div>}
      {agent.sessions.next_offset != null && <button type="button" className="agent-sessions__show-more" onClick={() => void agent.refreshSessions(agent.sessions.next_offset!).catch(e => agent.setError(String(e)))}>{x('more')}</button>}</nav>
      <AgentSettingsButton ready={ready} updateAvailable={ready && agent.environment?.skill.update_available}
        label={x(!ready ? 'notReady' : agent.environment?.skill.update_available ? 'skillHasUpdate' : 'ready')}
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
          {session && <button type="button" className={`agent-console__icon-btn extract-workspace-toggle${showWorkspace ? ' is-active' : ''}`} onClick={toggleWorkspace} title={x('showWorkspace')} aria-label={x('showWorkspace')} aria-pressed={showWorkspace} aria-expanded={showWorkspace} aria-controls="extraction-workspace"><Icon name="folder" /></button>}
        </div>
      </header>
      <main className="agent-console__main agent-cockpit extract-main">
      {!session || !agent.view?.rows.length ? <div className="agent-console__thread extract-welcome"><div className="agent-thread"><div className="agent-hero">
        <div className="agent-hero__mark"><Icon name="agent-extract" /></div><h2 className="agent-hero__title">{session ? session.title : x('startTitle')}</h2><p className="agent-hero__subtitle">{x('startText')}</p>
        <div className="agent-hero__steps">{(['quickExtract', 'quickCheck', 'quickReinsert'] as const).map(key => <button type="button" className="agent-hero__step" key={key} onClick={() => { setDraft(x(key)); if (!session) openCreate(); else textarea.current?.focus(); }}><span className="agent-hero__step-icon"><Icon name="sparkle" /></span>{x(key)}</button>)}</div>
        <div className="agent-hero__project-panel">{session ? <div className="extract-welcome-source" title={session.game_dir}><Icon name="folder" />{session.game_dir}</div>
          : <><span className="agent-hero__open-projects-label">{x('chooseSource')}</span><div className="agent-hero__actions"><button type="button" className="agent-hero__action" disabled={agent.busy} onClick={() => openCreate()}><Icon name="folder" />{x('newSession')}</button></div></>}
        </div>
      </div></div></div> : <Conversation rows={agent.view.rows} motionEvents={agent.view.motionEvents} sessionId={session.id} running={running} status={session.status} pending={agent.view.pending} older={agent.view.has_more} beforeSeq={agent.view.before_seq} loadingOlder={agent.loadingOlder} activityPrefix={agent.view.activity_prefix} loadOlder={agent.loadOlder} />}
      <div className="agent-console__composer extract-bottom">
        {agent.error && <div className="agent-notice agent-notice--error extract-inline-error" role="alert"><Icon name="warning" /><span>{agent.error}</span><button type="button" className="extract-text-button" onClick={agent.reload}>{x('reload')}</button></div>}
        {agent.view?.pending && <PendingRequest key={agent.view.pending.requestId} pending={agent.view.pending} busy={agent.busy} answer={body => { void agent.mutate('answer', body); }} />}
        <MessageQueue queued={agent.view?.queued || []} editing={editingQueued} onEditingChange={setEditingQueued} busy={agent.busy} canSend={ready}
          scope={{ sessionId: agent.selected, extraction: true }}
          onSave={() => { if (editingQueued) void agent.mutate('queue-update', { item_id: editingQueued.id, text: editingQueued.text }).then(result => { if (result) setEditingQueued(null); }); }}
          onDelete={id => { void agent.mutate('queue-delete', { item_id: id }).then(result => { if (result && editingQueued?.id === id) setEditingQueued(null); }); }}
          onSend={id => { if (!ready) return; void agent.mutate('queue-send', { item_id: id, profile: getBackendProfile(profile), profile_name: boundProfile, permission_mode: mode }).then(result => { if (result) setEditingQueued(null); }); }} />
        <ExtractionComposer draft={draft} setDraft={setDraft} textarea={textarea} session={session} running={running} busy={agent.busy} ready={ready}
          imageInput={imageInput} hasQueue={Boolean(agent.view?.queued?.length)}
          profile={profile} profileSelection={boundProfile} defaultProfile={defaultProfile} setProfile={setProfile} models={modelNames} mode={mode} setMode={changeMode} context={agent.view?.contextUsage} compacting={agent.view?.compacting}
          send={() => void send()} stop={() => {
            liveActivityDisclosures.get(`extraction::${agent.selected}`)?.();
            void agent.mutate('stop', {});
          }} chooseSource={() => openCreate()} configure={() => navigate('/backend-profiles')} />
        {!modelNames.length && <div className="extract-hint">{x('noModel')} <button type="button" className="extract-text-button" onClick={() => navigate('/backend-profiles')}>{x('configureModel')}</button></div>}
        {session && !ready && <div className="extract-hint"><button type="button" className="extract-text-button" onClick={() => setDialog('environment')}>{x('setup')}</button></div>}
      </div>
    </main>
      {session && <div id="extraction-workspace" className="extract-workspace-shell" aria-hidden={!showWorkspace} {...(!showWorkspace ? { inert: '' } : {})}>
        <Workspace key={session.id} session={session} refresh={toolCount} running={running} importProject={() => void importProject()} busy={agent.busy} />
      </div>}
    </div>
    <AnimatePresence>{dialog === 'create' && <CreateDialog key="create" close={() => setDialog(null)} create={create} busy={agent.busy} initialGame={dropped.game} initialError={dropped.error} />}</AnimatePresence>
    {pageDrop.dragging && <div className="extract-page-drop" aria-hidden><div><Icon name="folder" />{x('pageDropHint')}</div></div>}
    <AnimatePresence>{dialog === 'environment' && <EnvironmentDialog key="environment" initial={agent.environment} refresh={agent.refreshEnvironment} close={() => setDialog(null)} saved={agent.setEnvironment} projectDir={session?.game_dir ?? ''} />}</AnimatePresence>
  </div>;
}
