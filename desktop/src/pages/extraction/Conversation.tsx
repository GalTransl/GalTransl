import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/Icon';
import { usePageActive } from '../../components/PageActivity';
import { t, useUiLanguage } from '../../i18n';
import { AgentGroupView } from '../agent/rows/AgentGroupView';
import { followTail } from '../agent/followTail';
import { readingMemory } from '../agent/readingMemory';
import { createExtractionTimelineBuilder } from './presentation';
import { x, statusLabel } from './labels';
import type { Row } from './transcript';
import type { AgentEvent, Snapshot } from './types';
import { AskUserCard } from '../agent/rows/AskUserCard';
import { createAgentMotion } from '../agent/motion';
import { extractionMotionEvent } from './motion';
import { toolMeta, toolReason } from '../agent/toolMeta';

const NO_MOTION_EVENTS: AgentEvent[] = [];

export function Conversation({ rows, sessionId, running, status, pending, older, loadOlder, beforeSeq, loadingOlder = false, activityPrefix, motionEvents = NO_MOTION_EVENTS }: {
  rows: Row[]; sessionId: string; running: boolean; status: string; pending: AgentEvent | null; older: boolean; loadOlder: () => void;
  beforeSeq?: number | null; loadingOlder?: boolean; activityPrefix?: Snapshot['activity_prefix'];
  motionEvents?: AgentEvent[];
}) {
  useUiLanguage();
  const active = usePageActive();
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const tail = useRef<ReturnType<typeof followTail> | null>(null);
  const motion = useRef<ReturnType<typeof createAgentMotion> | null>(null);
  const previous = useRef({ sessionId, running });
  const historyPosition = useRef<{ sessionId: string; rows: Row[]; element?: HTMLElement; position: number; height: number } | null>(null);
  const attemptedPage = useRef<number | null | undefined>(null);
  const [atBottom, setAtBottom] = useState(true);
  const buildTimeline = useMemo(createExtractionTimelineBuilder, [sessionId]);
  const groups = useMemo(() => buildTimeline(rows, running, pending, activityPrefix), [buildTimeline, rows, running, pending, activityPrefix]);
  useLayoutEffect(() => {
    if (!scroller.current || !content.current) return;
    const key = `extraction::${sessionId}`;
    const controller = followTail(scroller.current, content.current, setAtBottom, {
      initial: readingMemory.read(key), active,
      onPosition: position => readingMemory.save(key, position),
    });
    tail.current = controller;
    const effects = createAgentMotion(scroller.current, content.current);
    effects.setActive(active);
    motion.current = effects;
    historyPosition.current = null;
    attemptedPage.current = null;
    return () => {
      controller.dispose(); effects.dispose();
      if (tail.current === controller) tail.current = null;
      if (motion.current === effects) motion.current = null;
    };
  }, [sessionId]);
  useLayoutEffect(() => { tail.current?.setActive(active); motion.current?.setActive(active); }, [active]);
  useLayoutEffect(() => {
    const position = historyPosition.current;
    const viewport = scroller.current;
    if (!position || !viewport || loadingOlder) return;
    historyPosition.current = null;
    if (position.sessionId !== sessionId || position.rows === rows || tail.current?.isFollowing()) return;
    const delta = position.element?.isConnected
      ? position.element.getBoundingClientRect().top + viewport.scrollTop - position.position
      : viewport.scrollHeight - position.height;
    tail.current?.restoreReading(viewport.scrollTop + delta);
  }, [rows, loadingOlder, sessionId]);
  useLayoutEffect(() => {
    tail.current?.follow();
    if (previous.current.sessionId === sessionId && previous.current.running && !running) {
      const answers = content.current?.querySelectorAll<HTMLElement>('[data-agent-final-step]');
      const last = answers?.[answers.length - 1];
      if (last) tail.current?.revealFinal(last);
    }
    previous.current = { sessionId, running };
  }, [groups, sessionId, running]);
  useLayoutEffect(() => {
    if (!motionEvents.length) { motion.current?.reset(); return; }
    for (const event of motionEvents) {
      const cue = extractionMotionEvent(event);
      if (cue) motion.current?.observe(cue);
    }
    motion.current?.flush(() => tail.current?.follow());
  }, [motionEvents, sessionId]);
  useLayoutEffect(() => {
    const viewport = scroller.current;
    if (!viewport || !active || !older || loadingOlder) return;
    const load = (retry: boolean) => {
      if (viewport.scrollTop > 160 || tail.current?.isFollowing() || historyPosition.current || (!retry && attemptedPage.current === beforeSeq)) return;
      attemptedPage.current = beforeSeq;
      const element = Array.from(content.current?.querySelectorAll<HTMLElement>('[data-agent-history-anchor], [data-agent-tool-id], [data-agent-user-step], [data-agent-final-step]') ?? [])
        .find(element => {
          const rect = element.getBoundingClientRect();
          return rect.height > 0 && rect.bottom > viewport.getBoundingClientRect().top;
        });
      historyPosition.current = { sessionId, rows, element,
        position: (element?.getBoundingClientRect().top ?? 0) + viewport.scrollTop, height: viewport.scrollHeight };
      loadOlder();
    };
    const onScroll = () => load(true);
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY >= 0 || viewport.scrollTop > 160) return;
      tail.current?.restoreReading(viewport.scrollTop);
      load(true);
    };
    viewport.addEventListener('scroll', onScroll, { passive: true });
    viewport.addEventListener('wheel', onWheel, { passive: true });
    load(false);
    return () => {
      viewport.removeEventListener('scroll', onScroll);
      viewport.removeEventListener('wheel', onWheel);
    };
  }, [active, older, beforeSeq, loadingOlder, loadOlder, rows, sessionId]);
  return <div className="agent-console__thread" ref={scroller} data-page-scroll-managed tabIndex={0} aria-label={x('conversation')}>
    <div className="agent-thread" ref={content}>
      {older && <div className="extract-older" role="status">{x(loadingOlder ? 'loadingOlder' : 'scrollOlder')}</div>}
      {groups.map((group, index) => group.type === 'notice' ? <div key={group.id} className="agent-compact-note">{group.text}</div>
        : <AgentGroupView key={group.id} group={group} isLive={running && index === groups.length - 1} projectDir="" persistKey={`extraction::${sessionId}`} imageScope={{ sessionId, extraction: true }} autoExpandTools={false} newProjectWizardRefs />)}
      {running && <div className="agent-working" role="status"><span className="agent-working__dots" aria-hidden><span /><span /><span /></span><span className="agent-working__label">{statusLabel(status)}</span></div>}
      {['stopped', 'interrupted'].includes(status) && <div className="agent-stopped"><span className="agent-stopped__text">{statusLabel(status)}</span></div>}
    </div>
    {!atBottom && <div className="agent-thread__jump"><button type="button" className="agent-jump-bottom" aria-label={x('jumpLatest')} title={x('jumpLatest')} onClick={() => tail.current?.jump()}><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg></button></div>}
  </div>;
}
export function PendingRequest({ pending, answer, busy }: { pending: AgentEvent; answer: (body: Record<string, unknown>) => void; busy: boolean }) {
  useUiLanguage();
  const [text, setText] = useState('');
  const permission = pending.type === 'permission_request';
  const reason = toolReason(pending.arguments);
  if (!permission && pending.questions?.length) return <AskUserCard
    item={{ kind: 'tool', step: 0, id: pending.toolCallId || '', name: 'ask_user', arguments: { questions: pending.questions } }}
    submitting={busy} error={null} onSubmit={answers => answer({ request_id: pending.requestId, answers })} />;
  return <section className={`agent-perm extract-pending${permission ? '' : ' extract-pending--question'}`} aria-live="polite" aria-label={x(permission ? 'permissionTitle' : 'questionTitle')}>
    <header className="agent-perm__head"><span className="agent-perm__icon"><Icon name={permission ? 'shield' : 'help'} /></span><strong className="agent-perm__title">{x(permission ? 'permissionTitle' : 'questionTitle')}</strong></header>
    {permission ? <><div className="extract-pending__body"><p className="extract-muted">{x('commandWarning')}</p>
      {reason && <p className="agent-perm__reason">{t('agent:permissionCard.agentPerm_message_text', { reason })}</p>}
      <details><summary>{toolMeta(pending.toolName, pending.arguments).action}</summary><pre>{JSON.stringify(pending.arguments, null, 2)}</pre></details></div><div className="extract-actions">
      {(['deny', 'allow-turn', 'allow-once'] as const).map(decision => <button type="button" key={decision} disabled={busy} className={`agent-perm__btn${decision === 'allow-once' ? ' is-primary' : ''}`} onClick={() => answer({ request_id: pending.requestId, decision })}>{x(decision === 'deny' ? 'deny' : decision === 'allow-turn' ? 'allowTurn' : 'allowOnce')}</button>)}
    </div></> : <form onSubmit={event => { event.preventDefault(); answer({ request_id: pending.requestId, text }); }}><div className="extract-pending__body"><p className="agent-ask__question">{pending.question}</p><textarea aria-label={x('answer')} value={text} onChange={event => setText(event.target.value)} placeholder={x('answer')} maxLength={10000} /></div><div className="extract-actions"><button className="agent-ask__btn is-primary" disabled={!text.trim() || busy}>{x('submit')}</button></div></form>}
  </section>;
}
