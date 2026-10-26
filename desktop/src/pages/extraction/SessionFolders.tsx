import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from '../../components/Icon';
import { basenamePath } from '../../lib/paths';
import { readProjectStorage, writeProjectStorage } from '../../lib/projectStorage';
import { useSessionTime } from '../../lib/sessionTime';
import { statusLabel, x } from './labels';
import { isRunning, type Session } from './types';

const COLLAPSED_KEY = 'galtransl-extraction-collapsed-folders';

export function gameFolderKey(directory: string): string {
  const path = directory.replace(/\\/g, '/').replace(/\/+$/, '') || '/';
  return /^[a-z]:/i.test(path) || path.startsWith('//') ? path.toLowerCase() : path;
}

export function SessionFolders({ sessions, selected, activeSession, onSelect, onCreateInFolder, onDeleteSession, onCloseFolder, runningDirectories = [], busy }: {
  sessions: Session[]; selected: string; activeSession?: Session; onSelect: (id: string) => void;
  onCreateInFolder: (directory: string) => void; onDeleteSession: (session: Session) => void;
  onCloseFolder: (directory: string) => void; runningDirectories?: string[]; busy: boolean;
}) {
  const formatSessionTime = useSessionTime();
  const id = useId();
  const lastSelected = useRef(selected);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    try {
      const saved = JSON.parse(readProjectStorage(COLLAPSED_KEY) || '[]');
      return new Set(Array.isArray(saved) ? saved.filter((key): key is string => typeof key === 'string') : []);
    } catch { return new Set(); }
  });
  useEffect(() => { writeProjectStorage(COLLAPSED_KEY, JSON.stringify([...collapsed])); }, [collapsed]);
  const groups = new Map<string, { directory: string; sessions: Session[] }>();
  for (const session of sessions) {
    const key = gameFolderKey(session.game_dir);
    if (!groups.has(key)) groups.set(key, { directory: session.game_dir, sessions: [] });
    groups.get(key)!.sessions.push(session);
  }
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
  return <>{[...groups].map(([key, group], index) => {
    const folded = collapsed.has(key);
    const active = group.sessions.some(session => session.id === selected);
    const hasRunning = runningDirectories.some(directory => gameFolderKey(directory) === key)
      || group.sessions.some(session => isRunning(session.id === activeSession?.id ? activeSession : session));
    const listId = `${id}-${index}`;
    return <section key={key} className={`agent-sessions__group${folded ? ' is-collapsed' : ''}${active ? ' is-active' : ''}`}>
      <div className="agent-sessions__group-head">
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
          onClick={() => onCreateInFolder(group.directory)}>＋</button>
        <button type="button" className="agent-sessions__group-close" disabled={busy || hasRunning}
          title={x(hasRunning ? 'stopBeforeClose' : 'closeFolder')}
          aria-label={`${x('closeFolder')} (${group.directory})`}
          onClick={() => onCloseFolder(group.directory)}><Icon name="close" /></button>
      </div>
      <div id={listId} className="agent-sessions__group-collapse" aria-hidden={folded}
        {...(folded ? { inert: '' } : {})}>
        <div className="agent-sessions__group-collapse-inner">
          <div className="agent-sessions__group-list">
            {group.sessions.map(item => {
              const currentSession = item.id === activeSession?.id ? activeSession : item;
              const status = currentSession.status;
              const itemRunning = isRunning({ ...item, status });
              const updatedAt = Math.max(item.updated_at || 0, currentSession.updated_at || 0) || item.created_at;
              return <div key={item.id} className={`agent-session-item${selected === item.id ? ' is-active' : ''}`}>
                <button type="button" className="agent-session-item__main" title={item.title}
                  aria-current={selected === item.id ? 'true' : undefined} onClick={() => onSelect(item.id)}>
                  <span className="agent-session-item__title">{item.title}</span>
                  <span className="agent-session-item__time">{formatSessionTime(updatedAt)}</span>
                </button>
                {itemRunning && <span className="agent-session-item__light is-running" title={statusLabel(status)} aria-hidden />}
                <button type="button" className="agent-session-item__delete" disabled={busy || itemRunning}
                  title={x(itemRunning ? 'stopBeforeClose' : 'deleteSession')}
                  aria-label={`${x('deleteSession')} (${item.title})`}
                  onClick={() => onDeleteSession(item)}><Icon name="close" /></button>
              </div>;
            })}
          </div>
        </div>
      </div>
    </section>;
  })}</>;
}
