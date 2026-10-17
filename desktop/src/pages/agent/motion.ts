import type { AgentEvent } from '../../lib/api';

type Cue = { selector: string; kind: 'arrive' | 'fade' | 'pop' };
const BUDGET = 6;

/** Short, live-event motions. No timers, token animations or persistent layers. */
export function createAgentMotion(viewport: HTMLElement, content: HTMLElement) {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pending = new Map<string, Cue>();
  const animations = new Set<Animation>();
  const streams = new Set<string>();
  let active = true;
  let disposed = false;
  const enabled = () => active && !disposed && !document.hidden && !reduced.matches;
  const clear = () => {
    pending.clear();
    for (const animation of animations) {
      animation.onfinish = animation.oncancel = null;
      animation.cancel();
    }
    animations.clear();
  };
  const visibilityChanged = () => { if (!enabled()) clear(); };
  document.addEventListener('visibilitychange', visibilityChanged);
  reduced.addEventListener('change', visibilityChanged);

  return {
    observe(event: AgentEvent) {
      let cue: Cue | undefined;
      if (event.type === 'user_message') {
        streams.clear();
        cue = { selector: `[data-agent-user-step="${event.step}"] .agent-bubble`, kind: 'arrive' };
      } else if (event.type === 'tool_call' || event.type === 'tool_result') {
        if (event.type === 'tool_call') streams.clear();
        if (event.id) cue = {
          selector: `[data-agent-tool-id="${CSS.escape(event.id)}"] .agent-tool__state-dot`, kind: 'pop',
        };
      } else if (event.type === 'content_delta' || event.type === 'reasoning_delta') {
        // Alternating reasoning/content and *_end do not restart a row's entrance.
        if (!event.delta?.trim() || streams.has(event.type)) return;
        streams.add(event.type);
        cue = event.type === 'content_delta'
          ? { selector: '.agent-content.is-streaming .agent-content__text > :first-child', kind: 'fade' }
          : { selector: '.agent-reasoning.is-streaming .agent-reasoning__icon', kind: 'pop' };
      } else if (event.type === 'finish') {
        streams.clear();
        if (event.summary?.trim()) cue = {
          selector: `[data-agent-final-step="${event.step}"] .agent-final > :first-child`, kind: 'fade',
        };
      } else if (event.type === 'llm_retry_start' || event.type === 'error' || event.type === 'stopped') {
        streams.clear();
      }
      if (!cue || !enabled()) return;
      // A burst of parallel tools cannot create an unbounded animation queue.
      pending.delete(cue.selector);
      pending.set(cue.selector, cue);
      if (pending.size > BUDGET) pending.delete(pending.keys().next().value!);
    },
    flush(beforeMeasure?: () => void) {
      if (!enabled()) { clear(); return; }
      if (!pending.size) return;
      beforeMeasure?.();
      const bounds = viewport.getBoundingClientRect();
      for (const { selector, kind } of pending.values()) {
        const matches = content.querySelectorAll<HTMLElement>(selector);
        const target = matches[matches.length - 1];
        if (!target || typeof target.animate !== 'function' || target.closest('[aria-hidden="true"]')) continue;
        const rect = target.getBoundingClientRect();
        if (!rect.height || rect.bottom <= bounds.top || rect.top >= bounds.bottom) continue;
        if (animations.size >= BUDGET) break;
        // Animate only a small dot/bubble/first paragraph. Keep the final reply's
        // anchor and long markdown surfaces stationary for scroll measurements.
        const frames = kind === 'pop'
          ? [{ transform: 'scale(0.65)', opacity: 0.45 }, { transform: 'scale(1.15)', opacity: 1, offset: 0.6 }, { transform: 'scale(1)', opacity: 1 }]
          : kind === 'arrive'
            ? [{ transform: 'translateY(6px)', opacity: 0 }, { transform: 'translateY(0)', opacity: 1 }]
            : [{ opacity: 0.35 }, { opacity: 1 }];
        const animation = target.animate(frames, {
          duration: kind === 'fade' ? 220 : 280, easing: 'cubic-bezier(0, 0, 0.2, 1)', fill: 'none',
        });
        animation.id = `agent-${kind}`;
        animations.add(animation);
        animation.onfinish = animation.oncancel = () => {
          animations.delete(animation);
          animation.onfinish = animation.oncancel = null;
          animation.cancel();
        };
      }
      pending.clear();
    },
    setActive(value: boolean) { active = value; if (!active) clear(); },
    reset() { clear(); streams.clear(); },
    dispose() {
      disposed = true;
      clear();
      document.removeEventListener('visibilitychange', visibilityChanged);
      reduced.removeEventListener('change', visibilityChanged);
    },
  };
}
