import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/Icon';
import { usePageActive } from '../../components/PageActivity';
import { useUiLanguage } from '../../i18n';
import { AgentGroupView } from '../agent/rows/AgentGroupView';
import { followTail } from '../agent/followTail';
import { readingMemory } from '../agent/readingMemory';
import { createExtractionTimelineBuilder } from './presentation';
import { x, statusLabel } from './labels';
import type { Row } from './transcript';
import type { AgentEvent } from './types';
import { AskUserCard } from '../agent/rows/AskUserCard';

export function Conversation({ rows, sessionId, running, status, pending, older, loadOlder }: {
  rows: Row[]; sessionId: string; running: boolean; status: string; pending: AgentEvent | null; older: boolean; loadOlder: () => void;
}) {
  useUiLanguage();
  const active = usePageActive();
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const tail = useRef<ReturnType<typeof followTail> | null>(null);
  const previous = useRef({ sessionId, running });
  const [atBottom, setAtBottom] = useState(true);
  const buildTimeline = useMemo(createExtractionTimelineBuilder, [sessionId]);
  const groups = useMemo(() => buildTimeline(rows, running, pending), [buildTimeline, rows, running, pending]);
  useLayoutEffect(() => {
    if (!scroller.current || !content.current) return;
    const key = `extraction::${sessionId}`;
    const controller = followTail(scroller.current, content.current, setAtBottom, {
      initial: readingMemory.read(key), active,
      onPosition: position => readingMemory.save(key, position),
    });
    tail.current = controller;
    return () => { controller.dispose(); if (tail.current === controller) tail.current = null; };
  }, [sessionId]);
  useEffect(() => tail.current?.setActive(active), [active]);
  useLayoutEffect(() => {
    tail.current?.follow();
    if (previous.current.sessionId === sessionId && previous.current.running && !running) {
      const answers = content.current?.querySelectorAll<HTMLElement>('[data-agent-final-step]');
      const last = answers?.[answers.length - 1];
      if (last) tail.current?.revealFinal(last);
    }
    previous.current = { sessionId, running };
  }, [groups, sessionId, running]);
  return <div className="agent-console__thread" ref={scroller} data-page-scroll-managed tabIndex={0} aria-label={x('conversation')}>
    <div className="agent-thread" ref={content}>
      {older && <button type="button" className="extract-text-button extract-older" onClick={loadOlder}>{x('older')}</button>}
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
  if (!permission && pending.questions?.length) return <AskUserCard
    item={{ kind: 'tool', step: 0, id: pending.toolCallId || '', name: 'ask_user', arguments: { questions: pending.questions } }}
    submitting={busy} error={null} onSubmit={answers => answer({ request_id: pending.requestId, answers })} />;
  return <section className={`agent-perm extract-pending${permission ? '' : ' extract-pending--question'}`} aria-live="polite" aria-label={x(permission ? 'permissionTitle' : 'questionTitle')}>
    <header className="agent-perm__head"><span className="agent-perm__icon"><Icon name={permission ? 'shield' : 'help'} /></span><strong className="agent-perm__title">{x(permission ? 'permissionTitle' : 'questionTitle')}</strong></header>
    {permission ? <><div className="extract-pending__body"><p className="extract-muted">{x('commandWarning')}</p><details><summary>{pending.toolName}</summary><pre>{JSON.stringify(pending.arguments, null, 2)}</pre></details></div><div className="extract-actions">
      {(['deny', 'allow-turn', 'allow-once'] as const).map(decision => <button type="button" key={decision} disabled={busy} className={`agent-perm__btn${decision === 'allow-once' ? ' is-primary' : ''}`} onClick={() => answer({ request_id: pending.requestId, decision })}>{x(decision === 'deny' ? 'deny' : decision === 'allow-turn' ? 'allowTurn' : 'allowOnce')}</button>)}
    </div></> : <form onSubmit={event => { event.preventDefault(); answer({ request_id: pending.requestId, text }); }}><div className="extract-pending__body"><p className="agent-ask__question">{pending.question}</p><textarea aria-label={x('answer')} value={text} onChange={event => setText(event.target.value)} placeholder={x('answer')} maxLength={10000} /></div><div className="extract-actions"><button className="agent-ask__btn is-primary" disabled={!text.trim() || busy}>{x('submit')}</button></div></form>}
  </section>;
}
