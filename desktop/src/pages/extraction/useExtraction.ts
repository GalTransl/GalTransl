import { useCallback, useEffect, useRef, useState } from 'react';
import { usePageActive, useRetainPage } from '../../components/PageActivity';
import { extractionApi, extractionUrl } from './api';
import { prependHistory, reduceEvents, type Row } from './transcript';
import { isRunning, type AgentEvent, type Environment, type SessionList, type SkillStatus, type Snapshot } from './types';
import { message, t, useMessageState } from '../../i18n';
import { notifyNeedsAttention } from '../../lib/desktopNotify';
import { askNotifyBody, toolMeta } from '../agent/toolMeta';
import { STREAM_MAX_RECONNECTS, streamReconnectDelay } from '../agent/streamReconnect';

type View = Snapshot & { rows: Row[]; motionEvents: AgentEvent[]; usage?: AgentEvent; contextUsage?: AgentEvent; compacting?: boolean };
const fromSnapshot = (snapshot: Snapshot, liveAfter?: number, previous?: View | null): View => {
  const events = [...snapshot.events, ...snapshot.live_events].sort((left, right) => (left.seq || 0) - (right.seq || 0));
  const latest = [...events].reverse();
  const retainHistory = previous?.session.id === snapshot.session.id && snapshot.has_more
    && (previous.before_seq === null || (snapshot.before_seq !== null && previous.before_seq < snapshot.before_seq));
  const rows = reduceEvents([], events);
  return { ...snapshot, rows: retainHistory ? prependHistory(rows, previous.rows) : rows,
    ...(retainHistory ? { has_more: previous.has_more, before_seq: previous.before_seq, activity_prefix: previous.activity_prefix } : {}),
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
  const [reconnecting, setReconnecting] = useMessageState<string>('');
  const [busy, setBusy] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [revision, setRevision] = useState(0);
  const current = useRef(selected); current.current = selected;
  const currentView = useRef(view); currentView.current = view;
  const currentEnvironment = useRef(environment); currentEnvironment.current = environment;
  // A submitted message is already in the next snapshot before the SSE opens.
  const sentAfter = useRef<{ sessionId: string; cursor: number } | null>(null);
  const request = useRef(false);
  const olderRequest = useRef(false);
  const retries = useRef(0);
  useRetainPage(isRunning(view?.session));
  const refreshSessions = useCallback(async (offset = 0) => {
    const result = await extractionApi<SessionList>('sessions', { offset });
    setSessions(previous => offset ? { ...result, sessions: [...previous.sessions.slice(0, offset), ...result.sessions] } : result);
  }, []);
  const refreshEnvironment = useCallback(async () => {
    const result = await extractionApi<Environment>('environment');
    const previous = currentEnvironment.current;
    const updated = previous?.settings.skill_dir === result.settings.skill_dir
      && previous.skill.current_commit === result.skill.current_commit
      && previous.skill.installed === result.skill.installed
      ? { ...result, skill: previous.skill } : result;
    setEnvironment(updated); return updated;
  }, []);
  useEffect(() => {
    if (active) {
      void refreshSessions().catch(e => setError(errorText(e)));
      void refreshEnvironment().catch(e => setError(errorText(e)));
    }
  }, [active, refreshSessions, refreshEnvironment]);
  const skillDirectory = environment?.settings.skill_dir;
  const skillCommit = environment?.skill.current_commit;
  const skillInstalled = environment?.skill.installed;
  useEffect(() => {
    if (!active || !skillInstalled || !skillDirectory) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const check = () => extractionApi<SkillStatus>('skill-check', {}, { skill_dir: skillDirectory }).then(skill => {
      if (disposed) return;
      setEnvironment(previous => previous?.settings.skill_dir === skillDirectory && previous.skill.current_commit === skillCommit
        ? { ...previous, skill } : previous);
    }).catch(() => { /* Keep the last known status when GitHub is unavailable. */ }).finally(() => {
      if (!disposed) timer = setTimeout(check, 60 * 60 * 1000);
    });
    void check();
    return () => { disposed = true; clearTimeout(timer); };
  }, [active, skillDirectory, skillCommit, skillInstalled]);
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
  useEffect(() => { setView(null); setError(''); setReconnecting(''); retries.current = 0; }, [selected]);
  const pendingRequest = view?.pending;
  useEffect(() => {
    if (!pendingRequest) return;
    const permission = pendingRequest.type === 'permission_request';
    const label = toolMeta(pendingRequest.toolName, pendingRequest.arguments).action;
    const body = permission ? t('agent:agentPage.agentPage_notifyNeedsAttention_confirmVariant2', { label })
      : askNotifyBody({ arguments: { questions: pendingRequest.questions || [{ question: pendingRequest.question }] } });
    void notifyNeedsAttention(`extraction:${selected}:${pendingRequest.requestId}`,
      t(permission ? 'agent:agentPage.agentPage_notifyNeedsAttention_agentWaitApprove' : 'agent:agentPage.agentPage_notifyNeedsAttention_agentAnswer'), body);
  }, [selected, pendingRequest?.requestId]);
  useEffect(() => {
    if (!selected || (!active && !isRunning(currentView.current?.session))) return;
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
          if (event.type === 'queue') next.queued = event.queued || [];
          if (event.type === 'state' && (!isRunning(next.session) || event.status === 'stopping')) {
            next.pending = null;
            next.compacting = false;
          }
        }
        return next;
      });
    };
    const connected = () => {
      retries.current = 0;
      setReconnecting('');
    };
    const retry = () => {
      if (disposed) return;
      const attempt = retries.current;
      if (attempt >= STREAM_MAX_RECONNECTS) {
        setReconnecting(message('agent:agentPage.onGiveUp_normalizeError_agent'));
        return;
      }
      retries.current = attempt + 1;
      setReconnecting(message('agent:agentPage.onReconnecting_setError_agentPending', { attempt: attempt + 1, max: STREAM_MAX_RECONNECTS }));
      reconnect = setTimeout(() => setRevision(value => value + 1), streamReconnectDelay(attempt));
    };
    void extractionApi<Snapshot>('session', { session_id: selected }, undefined, abort.signal).then(snapshot => {
      if (disposed) return;
      const sent = sentAfter.current;
      sentAfter.current = null;
      setView(previous => fromSnapshot(snapshot, sent?.sessionId === selected ? sent.cursor : undefined, previous));
      if (!isRunning(snapshot.session)) { connected(); return; }
      source = new EventSource(extractionUrl('stream', { session_id: selected, after: snapshot.cursor }));
      source.onmessage = incoming => {
        if (disposed) return;
        let event: AgentEvent & { snapshot?: Snapshot };
        try { event = JSON.parse(incoming.data); } catch { source?.close(); flush(); retry(); return; }
        connected();
        if (event.type === 'reset' && event.snapshot) {
          flush();
          const snapshot = event.snapshot;
          setView(previous => fromSnapshot(snapshot, undefined, previous));
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
        retry();
      };
    }).catch(() => retry());
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
  const loadOlder = useCallback(async () => {
    const view = currentView.current;
    if (!view?.has_more || !view.before_seq || olderRequest.current) return;
    const id = view.session.id;
    olderRequest.current = true; setLoadingOlder(true);
    try {
      const older = await extractionApi<Snapshot>('session', { session_id: id, before_seq: view.before_seq });
      if (current.current !== id) return;
      setView(previous => {
        if (!previous || previous.session.id !== id || previous.before_seq !== view.before_seq) return previous;
        return { ...previous, rows: prependHistory(previous.rows, reduceEvents([], older.events)),
          has_more: older.has_more, before_seq: older.before_seq, activity_prefix: older.activity_prefix };
      });
    } catch (error) { if (current.current === id) setError(errorText(error)); }
    finally { olderRequest.current = false; setLoadingOlder(false); }
  }, []);
  return { sessions, environment, selected, select: setSelected, view, error: reconnecting || error, setError, busy, loadingOlder,
    mutate, loadOlder, refreshSessions, refreshEnvironment, setEnvironment,
    reload: () => { retries.current = 0; setError(''); setReconnecting(''); setRevision(v => v + 1); } };
}
