import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from '../../components/Icon';
import { useFolderContextMenu } from '../../components/FolderContextMenu';
import { basenamePath } from '../../lib/paths';
import { readProjectStorage, writeProjectStorage } from '../../lib/projectStorage';
import { useSessionTime } from '../../lib/sessionTime';
import { useUnseenAgentLights } from '../../lib/agentActivity';
import { previewSessions } from '../../lib/sessionPreview';
import { applySavedOrder } from '../../lib/dragReorder';
import { useDragReorder, useSavedOrder } from '../../components/useDragReorder';
import { t } from '../../i18n';
import { statusLabel, x } from './labels';
import { isRunning, type Session } from './types';

const COLLAPSED_KEY = 'galtransl-extraction-collapsed-folders';
const FOLDER_ORDER_KEY = 'galtransl-extraction-folder-order';

export function gameFolderKey(directory: string): string {
  const path = directory.replace(/\\/g, '/').replace(/\/+$/, '') || '/';
  return /^[a-z]:/i.test(path) || path.startsWith('//') ? path.toLowerCase() : path;
}

/** The session listed first under an open game folder, if that folder is in the list. */
export function firstSessionInFolder(sessions: Session[], directory: string): Session | undefined {
  const key = gameFolderKey(directory);
  return sessions.find(session => gameFolderKey(session.game_dir) === key);
}

export function SessionFolders({ sessions, selected, activeSession, onSelect, onCreateInFolder, onDeleteSession, onCloseFolder, runningDirectories = [], busy }: {
  sessions: Session[]; selected: string; activeSession?: Session; onSelect: (id: string) => void;
  onCreateInFolder: (directory: string) => void; onDeleteSession: (session: Session) => void;
  onCloseFolder: (directory: string) => void; runningDirectories?: string[]; busy: boolean;
}) {
  const formatSessionTime = useSessionTime();
  const unseen = useUnseenAgentLights('extraction');
  const id = useId();
  const lastSelected = useRef(selected);
  const folderMenu = useFolderContextMenu();
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    try {
      const saved = JSON.parse(readProjectStorage(COLLAPSED_KEY) || '[]');
      return new Set(Array.isArray(saved) ? saved.filter((key): key is string => typeof key === 'string') : []);
    } catch { return new Set(); }
  });
  useEffect(() => { writeProjectStorage(COLLAPSED_KEY, JSON.stringify([...collapsed])); }, [collapsed]);
  // Folders whose "show more" is open; local only, like the translation sidebar.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const folderOrder = useSavedOrder(FOLDER_ORDER_KEY);
  const groups = new Map<string, { directory: string; sessions: Session[] }>();
  for (const session of sessions) {
    const key = gameFolderKey(session.game_dir);
    if (!groups.has(key)) groups.set(key, { directory: session.game_dir, sessions: [] });
    groups.get(key)!.sessions.push(session);
  }
  // Folder order: the user's dragged order, with folders they have not placed yet on top.
  const orderedKeys = applySavedOrder([...groups.keys()], folderOrder.order);
  const folderDrag = useDragReorder({
    keys: orderedKeys,
    onReorder: (key, toIndex) => folderOrder.reorder(orderedKeys, key, toIndex),
    handle: '.agent-sessions__group-toggle',
  });
  // Reveal a newly selected/created session even when its folder was folded.
  const selectedDirectory = (activeSession?.id === selected ? activeSession : sessions.find(session => session.id === selected))?.game_dir;
  useEffect(() => {
    if (lastSelected.current === selected || (selected && !selectedDirectory)) return;
    lastSelected.current = selected;
    if (!selectedDirectory) return;
    const key = gameFolderKey(selectedDirectory);
    setCollapsed(previous => {
      if (!previous.has(key)) return previous;
      const next = new Set(previous); next.delete(key); return next;
    });
  }, [selected, selectedDirectory]);
  return <>{orderedKeys.map((key, index) => {
    const group = groups.get(key)!;
    const folded = collapsed.has(key);
    const active = group.sessions.some(session => session.id === selected);
    const hasRunning = runningDirectories.some(directory => gameFolderKey(directory) === key)
      || group.sessions.some(session => isRunning(session.id === activeSession?.id ? activeSession : session));
    const listId = `${id}-${index}`;
    const preview = previewSessions(group.sessions, expanded.has(key), session =>
      session.id === selected || isRunning(session.id === activeSession?.id ? activeSession : session));
    return <section key={key} className={`agent-sessions__group${folded ? ' is-collapsed' : ''}${active ? ' is-active' : ''}${folderDrag.drag?.key === key ? ' is-dragging' : ''}`}
      ref={folderDrag.itemRef(key)} style={folderDrag.itemStyle(index)} onPointerDown={event => folderDrag.onPointerDown(event, key)}>
      <div className="agent-sessions__group-head" onContextMenu={event => folderMenu.onContextMenu(event, group.directory)}>
        <button type="button" className="agent-sessions__group-toggle" title={group.directory}
          aria-expanded={!folded} aria-controls={listId} onClick={() => setCollapsed(previous => {
            const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next;
          })}>
          <span className="agent-sessions__group-icon" aria-hidden><Icon name={folded ? 'folder' : 'folder-open'} /></span>
          <span className="agent-sessions__group-name">{basenamePath(group.directory) || group.directory}</span>
          <span className="agent-sessions__group-count">{group.sessions.length}</span>
        </button>
        <button type="button" className="agent-sessions__group-new" disabled={busy}
          title={`${x('newSessionInFolder')} (${group.directory})`}
          aria-label={`${x('newSessionInFolder')} (${group.directory})`}
          onClick={() => onCreateInFolder(group.directory)}><Icon name="plus" size={12} /></button>
        <button type="button" className="agent-sessions__group-close" disabled={busy || hasRunning}
          title={x(hasRunning ? 'stopBeforeClose' : 'closeFolder')}
          aria-label={`${x('closeFolder')} (${group.directory})`}
          onClick={() => onCloseFolder(group.directory)}><Icon name="close" /></button>
      </div>
      <div id={listId} className="agent-sessions__group-collapse" aria-hidden={folded}
        {...(folded ? { inert: '' } : {})}>
        <div className="agent-sessions__group-collapse-inner">
          <div className="agent-sessions__group-list">
            {preview.visible.map(item => {
              const currentSession = item.id === activeSession?.id ? activeSession : item;
              const status = currentSession.status;
              const itemRunning = isRunning({ ...item, status });
              const updatedAt = Math.max(item.updated_at || 0, currentSession.updated_at || 0) || item.created_at;
              // Blue while working; green/orange once finished until the session is opened.
              const light = itemRunning ? 'running' : selected === item.id ? '' : unseen[item.id] || '';
              return <div key={item.id} className={`agent-session-item${selected === item.id ? ' is-active' : ''}`}>
                <button type="button" className="agent-session-item__main" title={item.title}
                  aria-current={selected === item.id ? 'true' : undefined} onClick={() => onSelect(item.id)}>
                  <span className="agent-session-item__title">{item.title}</span>
                  <span className="agent-session-item__time">{formatSessionTime(updatedAt)}</span>
                </button>
                {light && <span className={`agent-session-item__light is-${light}`}
                  title={light === 'running' ? statusLabel(status) : x(light === 'failed' ? 'sessionFailedUnseen' : 'sessionFinishedUnseen')} aria-hidden />}
                <button type="button" className="agent-session-item__delete" disabled={busy || itemRunning}
                  title={x(itemRunning ? 'stopBeforeClose' : 'deleteSession')}
                  aria-label={`${x('deleteSession')} (${item.title})`}
                  onClick={() => onDeleteSession(item)}><Icon name="close" /></button>
              </div>;
            })}
            {(preview.hidden > 0 || preview.collapsible) && <button type="button" className="agent-sessions__show-more"
              onClick={() => setExpanded(previous => {
                const next = new Set(previous); if (preview.hidden > 0) next.add(key); else next.delete(key); return next;
              })}>{t(preview.hidden > 0
                ? 'agent:agentSessionSidebar.agentSessionsGroupList_button_showMore'
                : 'agent:agentSessionSidebar.agentSessionsGroupList_button_showLess')}</button>}
          </div>
        </div>
      </div>
    </section>;
  })}{folderMenu.menu}</>;
}
