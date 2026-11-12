import { useEffect, useId, useRef, useState } from 'react';
import { Icon } from '../../components/Icon';
import { usePageActive } from '../../components/PageActivity';

export type QuickCommand = { key: string; label: string; prompt: string };

/* 输入框工具栏上的「快捷指令」chip：悬停或点击展开，选一条只把提示词填进输入框，不直接发送。
   翻译 Agent 页和提取 Agent 页共用，两边只是指令清单和填入方式不同。 */
export function QuickCommands({ label, commands, onPick }: {
  label: string; commands: readonly QuickCommand[]; onPick: (prompt: string) => void;
}) {
  const active = usePageActive();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => { if (!active) setOpen(false); }, [active]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const showMenu = () => {
    setOpen(true);
    requestAnimationFrame(() => root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus());
  };
  return <div className="agent-profile-picker agent-quick-commands" ref={root}
    onMouseEnter={() => setOpen(true)} onMouseLeave={() => {
      if (!root.current?.contains(document.activeElement)) setOpen(false);
    }} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }} onKeyDown={event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); return;
      }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      if (!open) { showMenu(); return; }
      const items = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || []);
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }}>
    <button type="button" ref={trigger} className={`agent-composer__chip${open ? ' is-open' : ''}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} onClick={showMenu}>
      <span className="agent-composer__chip-icon"><Icon name="sparkle" /></span>
      <span className="agent-composer__chip-label">{label}</span>
    </button>
    {open && <div className="agent-profile-menu" id={id} role="menu" aria-label={label}>
      {commands.map(command => <button type="button" className="agent-profile-menu__item" key={command.key} role="menuitem" onClick={() => {
        setOpen(false); onPick(command.prompt);
      }}>{command.label}</button>)}
    </div>}
  </div>;
}
