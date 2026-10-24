import { useId, useState } from 'react';
import { t, useUiLanguage } from '../../../i18n';
import { Icon } from '../../../components/Icon';
import type { ActivityItem } from '../timeline';
import type { ActivityEntry } from '../readGroups';
import { asArgs, toolMeta } from '../toolMeta';
import { manualOpenState } from '../uiState';
import { AnimatedDisclosure } from './AnimatedDisclosure';
import { ToolRow, type ToolPhase } from './ToolRow';

export function ReadToolGroup({ entry, items, phases, persistKey }: {
  entry: ActivityEntry;
  items: ActivityItem[];
  phases: (ToolPhase | undefined)[];
  persistKey: string;
}) {
  useUiLanguage();
  const id = useId();
  const stateKey = `${persistKey}::reads-${entry.key}`;
  const [open, setOpen] = useState(() => manualOpenState.get(stateKey) ?? false);
  const incomplete = entry.indices.some(index => phases[index] === 'stale');
  const queued = entry.indices.every(index => phases[index] === 'queued');
  const label = t(queued ? 'agent:readGroup.queued' : entry.live ? 'agent:readGroup.running' : incomplete ? 'agent:readGroup.incomplete' : 'agent:readGroup.done', { count: entry.indices.length });
  const summary = [...new Set(entry.indices.map(index => {
    const item = items[index];
    const meta = toolMeta(item.name);
    return meta.summary(asArgs(item.arguments)) || meta.action;
  }))].join('；');
  return (
    <div className={`agent-read-group${open ? ' is-open' : ''}`}>
      <button type="button" className="agent-tool__header agent-read-group__header"
        aria-expanded={open} aria-controls={id}
        onClick={() => { manualOpenState.set(stateKey, !open); setOpen(!open); }}>
        <span className="agent-tool__icon"><Icon name="search" /></span>
        <span className={`agent-tool__name${entry.live && !queued ? ' is-running' : ''}`}>{label}</span>
        <span className="agent-tool__summary" title={summary}>{summary}</span>
        <span className="agent-read-group__caret">›</span>
      </button>
      <AnimatedDisclosure id={id} open={open} className="agent-read-group__collapse">
        {() => <div className="agent-read-group__inner">
          {entry.indices.map(index => <ToolRow key={items[index].id || items[index].step}
            item={items[index]} phase={phases[index]} persistKey={persistKey} />)}
        </div>}
      </AnimatedDisclosure>
    </div>
  );
}
