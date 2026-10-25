import { useCallback, useEffect, useRef, useState } from 'react';
import { usePageActive, useRetainPage } from '../../components/PageActivity';
import { extractionApi, extractionUrl } from './api';
import { reduceEvents, type Row } from './transcript';
import { isRunning, type AgentEvent, type Environment, type SessionList, type Snapshot } from './types';

type View = Snapshot & { rows: Row[]; motionEvents: AgentEvent[]; usage?: AgentEvent; contextUsage?: AgentEvent; compacting?: boolean };
const fromSnapshot = (snapshot: Snapshot, liveAfter?: number): View => {
  const events = [...snapshot.events, ...snapshot.live_events];
  const latest = [...events].reverse();
  return { ...snapshot, rows: reduceEvents([], events),
    motionEvents: liveAfter === undefined ? [] : events.filter(event => (event.seq || 0) > liveAfter),
    usage: latest.find(event => event.type === 'usage'),
    contextUsage: latest.find(event => event.type === 'context_usage'),
    compacting: isRunning(snapshot.session) && latest.find(event => event.type === 'compaction')?.active };
};
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
const remembered = () => { try { return localStorage.getItem('galtransl-extraction-session') || ''; } catch { return ''; } };

export function useExtraction() {
  const active = usePageActive();
  const [sessions, setSessions] = useState<SessionList>({ sessions: [], total: 0, next_offset: null });
  const [environment, setEnvironment] = useState<Environment | null>(null);
  const [selected, setSelected] = useState(remembered);
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const current = useRef(selected); current.current = selected;
  const currentView = useRef(view); currentView.current = view;
  // A submitted message is already in the next snapshot before the SSE opens.
  const sentAfter = useRef<{ sessionId: string; cursor: number } | null>(null);
  const request = useRef(false);
  const retries = useRef(0);
  useRetainPage(isRunning(view?.session));
  const refreshSessions = useCallback(async (offset = 0) => {
    const result = await extractionApi<SessionList>('sessions', { offset });
    setSessions(previous => offset ? { ...result, sessions: [...previous.sessions.slice(0, offset), ...result.sessions] } : result);
  }, []);
  const refreshEnvironment = useCallback(async () => {
    const result = await extractionApi<Environment>('environment'); setEnvironment(result); return result;
  }, []);
  useEffect(() => {
    if (active) {
      void refreshSessions().catch(e => setError(errorText(e)));
      void refreshEnvironment().catch(e => setError(errorText(e)));
    }
  }, [active, refreshSessions, refreshEnvironment]);
  const hasRunningSessions = Boolean(sessions.running_game_dirs?.length || sessions.sessions.some(isRunning));
  useEffect(() => {
    if (!active || !hasRunningSessions) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { await refreshSessions(); } catch { /* Keep the last list until the next check. */ }
      if (!disposed) timer = setTimeout(refresh, 2000);
    };
    timer = setTimeout(refresh, 2000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [active, hasRunningSessions, refreshSessions]);
  useEffect(() => {
    try { localStorage.setItem('galtransl-extraction-session', selected); } catch { /* cache only */ }
  }, [selected]);
  useEffect(() => { setView(null); setError(''); retries.current = 0; }, [selected]);
  useEffect(() => {
    if (!active || !selected) return;
    let disposed = false;
    let source: EventSource | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let buffer: AgentEvent[] = [];
    const abort = new AbortController();
    const flush = () => {
      const batch = buffer; buffer = []; timer = undefined;
      if (disposed || !batch.length) return;
      setView(previous => {
        if (!previous || previous.session.id !== selected) return previous;
        const next = { ...previous, session: { ...previous.session }, rows: reduceEvents(previous.rows, batch), motionEvents: batch };
        for (const event of batch) {
          next.cursor = Math.max(next.cursor, event.seq || 0);
          if (event.type === 'state') next.session.status = event.status || 'idle';
          if (event.type === 'permission_mode' && event.permission_mode) next.session.permission_mode = event.permission_mode;
          if (event.type === 'permission_request' || event.type === 'ask_request') next.pending = event;
          if (event.type === 'request_answered') next.pending = null;
          if (event.type === 'usage') next.usage = event;
          if (event.type === 'context_usage') next.contextUsage = event;
          if (event.type === 'compaction') next.compacting = event.active;
          if (event.type === 'state' && (!isRunning(next.session) || event.status === 'stopping')) {
            next.pending = null;
            next.compacting = false;
          }
        }
        return next;
      });
    };
    void extractionApi<Snapshot>('session', { session_id: selected }, undefined, abort.signal).then(snapshot => {
      if (disposed) return;
      const sent = sentAfter.current;
      sentAfter.current = null;
      setView(fromSnapshot(snapshot, sent?.sessionId === selected ? sent.cursor : undefined));
      if (!isRunning(snapshot.session)) return;
      source = new EventSource(extractionUrl('stream', { session_id: selected, after: snapshot.cursor }));
      source.onmessage = message => {
        if (disposed) return;
        let event: AgentEvent & { snapshot?: Snapshot };
        try { event = JSON.parse(message.data); } catch { setError('Invalid extraction stream'); source?.close(); return; }
        retries.current = 0;
        if (event.type === 'reset' && event.snapshot) {
          flush(); setView(fromSnapshot(event.snapshot));
        } else if (event.type === 'close') {
          flush(); source?.close();
          void refreshSessions().catch(e => setError(errorText(e)));
          setRevision(v => v + 1);
        } else {
          buffer.push(event);
          if (!timer) timer = setTimeout(flush, 80);
        }
      };
      source.onerror = () => {
        source?.close(); flush();
        if (!disposed && retries.current++ < 5) reconnect = setTimeout(() => setRevision(v => v + 1), Math.min(1000 * retries.current, 5000));
        else if (!disposed) setError('Stream disconnected. Reload the session to reconnect.');
      };
    }).catch(error => { if (!disposed) setError(errorText(error)); });
    return () => { disposed = true; abort.abort(); source?.close(); clearTimeout(timer); clearTimeout(reconnect); };
  }, [selected, revision, active, refreshSessions]);

  const mutate = useCallback(async <T,>(action: string, body: Record<string, unknown>): Promise<T | undefined> => {
    if (request.current) return;
    request.current = true; setBusy(true); setError('');
    const id = current.current;
    const cursor = currentView.current?.session.id === id ? currentView.current.cursor : 0;
    try {
      const result = await extractionApi<T>(action, {}, { session_id: id, ...body });
      if (id === current.current) {
        if (action === 'message') sentAfter.current = { sessionId: id, cursor };
        if (action === 'permission-mode' && typeof body.permission_mode === 'string') {
          setView(previous => previous?.session.id === id
            ? { ...previous, session: { ...previous.session, permission_mode: body.permission_mode as string } } : previous);
        }
        if (action !== 'delete' && action !== 'close-folder') setRevision(v => v + 1);
      }
      await refreshSessions().catch(error => setError(errorText(error)));
      return result;
    } catch (error) { if (id === current.current) setError(errorText(error)); return undefined; }
    finally { request.current = false; setBusy(false); }
  }, [refreshSessions]);
  const loadOlder = async () => {
    if (!view?.before_seq || busy) return;
    const id = selected; setBusy(true);
    try {
      const older = await extractionApi<Snapshot>('session', { session_id: id, before_seq: view.before_seq });
      if (current.current !== id) return;
      setView(previous => {
        if (!previous) return previous;
        const previousById = new Map(previous.rows.map(row => [row.id, row]));
        const rows = reduceEvents([], older.events).map(row => previousById.get(row.id) || row);
        const ids = new Set(rows.map(row => row.id));
        return { ...previous, rows: [...rows, ...previous.rows.filter(row => !ids.has(row.id))].slice(0, 1000),
          has_more: older.has_more, before_seq: older.before_seq };
      });
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  };
  return { sessions, environment, selected, select: setSelected, view, error, setError, busy,
    mutate, loadOlder, refreshSessions, refreshEnvironment, setEnvironment,
    reload: () => { retries.current = 0; setError(''); setRevision(v => v + 1); } };
}
