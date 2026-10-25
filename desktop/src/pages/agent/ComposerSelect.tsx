import { useEffect, useId, useRef, useState } from 'react';
import { Icon, type IconName } from '../../components/Icon';
import { usePageActive } from '../../components/PageActivity';

/** Compact, keyboard-accessible picker using the agent composer's existing chip/menu styles. */
export function ComposerSelect({ icon, label, value, options, onChange, disabled = false, emptyLabel, action, triggerClassName = '' }: {
  icon: IconName;
  label: string;
  value: string;
  options: { value: string; label: string; hint?: string }[];
  onChange: (value: string) => void;
  disabled?: boolean;
  triggerClassName?: string;
  emptyLabel?: string;
  action?: { label: string; onClick: () => void };
}) {
  const active = usePageActive();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => { if (!active || disabled) setOpen(false); }, [active, disabled]);
  useEffect(() => {
    if (!open) return;
    (root.current?.querySelector<HTMLButtonElement>('[role="menuitemradio"][aria-checked="true"]')
      || root.current?.querySelector<HTMLButtonElement>('[role="menuitemradio"], [role="menuitem"]'))?.focus();
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  return <div className="agent-profile-picker" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }} onKeyDown={event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      if (!open) { setOpen(true); return; }
      const items = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"], [role="menuitem"]') || []);
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  }}>
    <button type="button" ref={trigger} className={`agent-composer__chip${triggerClassName ? ` ${triggerClassName}` : ''}${open ? ' is-open' : ''}`} disabled={disabled}
      aria-label={label} title={`${label} · ${options.find(option => option.value === value)?.label || emptyLabel || ''}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen(v => !v)}>
      <span className="agent-composer__chip-icon"><Icon name={icon} /></span>
      <span className="agent-composer__chip-label">{options.find(option => option.value === value)?.label || emptyLabel}</span>
    </button>
    {open && <div className="agent-profile-menu" id={id} role="menu" aria-label={label}>
      {!options.length && <div className="agent-profile-menu__empty">{emptyLabel}</div>}
      {options.map(option => <button type="button" key={option.value} className={`agent-profile-menu__item${option.hint ? ' agent-profile-menu__item--stacked' : ''}`} role="menuitemradio"
        aria-checked={option.value === value} onClick={() => { onChange(option.value); close(); }}>
        <span className="agent-profile-menu__label">{option.label}{option.hint && <span className="agent-profile-menu__hint">{option.hint}</span>}</span>
        {option.value === value && <span className="agent-profile-menu__check" aria-hidden><Icon name="check" /></span>}
      </button>)}
      {action && <><div className="agent-profile-menu__sep" /><button type="button" role="menuitem"
        className="agent-profile-menu__item agent-profile-menu__item--action" onClick={() => { close(); action.onClick(); }}>{action.label}<span className="agent-profile-menu__chev" aria-hidden>›</span></button></>}
    </div>}
  </div>;
}
