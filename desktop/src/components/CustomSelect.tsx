import {
  type CSSProperties,
  type ReactNode,
  type SelectHTMLAttributes,
  type Ref,
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import { usePageActive } from './PageActivity';
import { createPortal } from 'react-dom';
import { AnimatePresence, MotionConfig, UI_TRANSITION, useIsPresent, m } from './UiMotion';
import { useUiAnimations } from '../lib/useUiAnimations';

function SelectPanel({ children, panelRef, style, id, animated }: {
  children: ReactNode; panelRef: Ref<HTMLDivElement>; style: CSSProperties; id: string; animated: boolean;
}) {
  const present = useIsPresent();
  return <m.div ref={panelRef} className="custom-select__panel custom-select__panel--motion"
    style={{ ...style, transformOrigin: style.bottom === 'auto' ? 'center top' : 'center bottom', pointerEvents: present ? undefined : 'none' }}
    role={present ? 'listbox' : undefined} id={id} aria-hidden={!present || undefined}
    {...(!present ? { inert: '' } : {})}
    initial={animated ? { opacity: 0, y: -4, scale: 0.98 } : false}
    animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -3, scale: 0.985 }} transition={animated ? UI_TRANSITION : { duration: 0 }}>
    {children}
  </m.div>;
}

/* ── Option data extracted from <option> children ── */
interface OptionData {
  value: string;
  label: string;
  disabled?: boolean;
}

/* ── Props ── */
type CustomSelectProps = SelectHTMLAttributes<HTMLSelectElement> & {
  /** Compact variant (smaller height, for toolbars / inline use) */
  compact?: boolean;
  /** 设置项用：选项少于 SEGMENTED_OPTION_LIMIT 个时横向排成胶囊，一眼看全、一次点击；
   *  选项更多，或当前值不在选项里（胶囊无法显示它）时仍是下拉框。 */
  segmented?: boolean;
  children: ReactNode;
};

const SEGMENTED_OPTION_LIMIT = 4;

function SegmentedOptions({ options, value, disabled, className, onSelect, id, ariaLabel, ariaLabelledby, ariaDescribedby }: {
  options: OptionData[]; value: unknown; disabled: boolean; className: string; onSelect: (value: string) => void;
  id?: string; ariaLabel?: string; ariaLabelledby?: string; ariaDescribedby?: string;
}) {
  const groupRef = useRef<HTMLDivElement>(null);
  const move = (from: number, step: 1 | -1) => {
    for (let i = 1; i <= options.length; i++) {
      const idx = (from + step * i + options.length * i) % options.length;
      if (options[idx].disabled) continue;
      onSelect(options[idx].value);
      groupRef.current?.querySelectorAll<HTMLElement>('[role="radio"]')[idx]?.focus();
      return;
    }
  };
  return (
    <div ref={groupRef} id={id} role="radiogroup" aria-label={ariaLabel} aria-labelledby={ariaLabelledby}
      aria-describedby={ariaDescribedby} aria-disabled={disabled || undefined}
      className={['segmented-select', disabled ? 'segmented-select--disabled' : '', className].filter(Boolean).join(' ')}>
      {options.map((opt, idx) => {
        const selected = opt.value === value;
        const inert = disabled || Boolean(opt.disabled);
        return (
          // 不用 <button>：设置行多半包在 <label> 里，点到标签文字或胶囊间的空隙时
          // 浏览器会把点击转给 label 内第一个按钮，等于误选第一项。
          <div key={opt.value || `__empty_${idx}`} role="radio" aria-checked={selected} aria-disabled={inert || undefined}
            tabIndex={selected && !inert ? 0 : -1} title={opt.label}
            className={[
              'segmented-select__option',
              selected ? 'segmented-select__option--selected' : '',
              inert ? 'segmented-select__option--disabled' : '',
            ].filter(Boolean).join(' ')}
            onClick={() => { if (!inert && !selected) onSelect(opt.value); }}
            onKeyDown={(e) => {
              if (inert) return;
              if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); move(idx, 1); }
              else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); move(idx, -1); }
              else if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); if (!selected) onSelect(opt.value); }
            }}>
            {opt.label}
          </div>
        );
      })}
    </div>
  );
}

/* ── Helpers ── */

function extractOptions(children: ReactNode): OptionData[] {
  const result: OptionData[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    // Handle <optgroup> — flatten its children
    if (
      typeof child.type === 'string' &&
      (child.type === 'optgroup' || child.type === 'group')
    ) {
      extractOptions((child.props as { children?: ReactNode }).children).forEach((o) =>
        result.push(o),
      );
      return;
    }
    const props = child.props as Record<string, unknown>;
    const value = String(props.value ?? '');
    // label falls back to children text
    let label = String(props.label ?? '');
    if (!label && props.children != null) {
      const ch = props.children;
      label = Array.isArray(ch) ? ch.join('') : String(ch);
    }
    result.push({ value, label, disabled: Boolean(props.disabled) });
  });
  return result;
}

/* ── Component ── */

export function CustomSelect({
  children,
  value,
  onChange,
  disabled,
  className = '',
  compact,
  segmented,
  ...rest
}: CustomSelectProps) {
  const options = extractOptions(children);
  const active = usePageActive();
  const animated = useUiAnimations();
  const [open, setOpen] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const instanceId = useId();
  const justSelectedRef = useRef(false);
  const [panelStyle, setPanelStyle] = useState<CSSProperties | null>(null);

  const selectedOption = options.find((o) => o.value === value);
  const fullLabel = selectedOption?.label || String(value);
  const displayLabel = fullLabel.split('·')[0].trim();

  // Close on outside click
  useEffect(() => {
    if (!active || !open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        containerRef.current &&
        !containerRef.current.contains(target) &&
        !listRef.current?.contains(target)
      ) {
        setOpen(false);
        setHighlightIdx(-1);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [active, open]);

  // Scroll highlighted item into view
  useEffect(() => {
    if (!active || !open || highlightIdx < 0 || !listRef.current) return;
    const el = listRef.current.querySelector(
      `[data-idx="${highlightIdx}"]`,
    ) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [active, open, highlightIdx]);

  useEffect(() => {
    if (!active || !open) {
      setPanelStyle(null);
      return;
    }

    const updatePanelStyle = () => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const gap = 6;
      // Estimate panel height from actual option count so short lists don't
      // mistakenly flip upward when there is plenty of room below.
      const OPTION_ROW_HEIGHT = 36; // ~padding + line-height
      const PANEL_PADDING = 8;
      const MAX_PANEL_HEIGHT = 280;
      const estimatedPanelHeight = Math.min(
        MAX_PANEL_HEIGHT,
        Math.max(OPTION_ROW_HEIGHT, options.length * OPTION_ROW_HEIGHT + PANEL_PADDING),
      );
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      // Only flip upward when the panel truly doesn't fit below AND above has more room.
      const openUpward = spaceBelow < estimatedPanelHeight && spaceAbove > spaceBelow;
      const width = rect.width;
      const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));

      setPanelStyle({
        position: 'fixed',
        left,
        right: 'auto',
        width,
        top: openUpward ? 'auto' : rect.bottom + gap,
        bottom: openUpward ? window.innerHeight - rect.top + gap : 'auto',
        maxHeight: Math.min(MAX_PANEL_HEIGHT, Math.max(0, (openUpward ? spaceAbove : spaceBelow) - gap - 8)),
        zIndex: 4000,
      });
    };

    updatePanelStyle();
    window.addEventListener('resize', updatePanelStyle);
    window.addEventListener('scroll', updatePanelStyle, true);

    return () => {
      window.removeEventListener('resize', updatePanelStyle);
      window.removeEventListener('scroll', updatePanelStyle, true);
    };
  }, [active, open]);

  const notifyChange = useCallback(
    (val: string) => {
      // Synthesize an event compatible with native <select> onChange
      if (onChange) {
        const syntheticEvent = {
          target: { value: val },
          currentTarget: { value: val },
        } as React.ChangeEvent<HTMLSelectElement>;
        onChange(syntheticEvent);
      }
    },
    [onChange],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (disabled) return;
      switch (e.key) {
        case ' ':
        case 'Enter':
          e.preventDefault();
          if (!active || !open) {
            setOpen(true);
            setHighlightIdx(options.findIndex((o) => o.value === value));
          } else {
            // Select highlighted
            if (highlightIdx >= 0 && !options[highlightIdx]?.disabled) {
              notifyChange(options[highlightIdx].value);
              setOpen(false);
              setHighlightIdx(-1);
            }
          }
          break;
        case 'Escape':
          setOpen(false);
          setHighlightIdx(-1);
          break;
        case 'ArrowDown':
          e.preventDefault();
          if (!active || !open) {
            setOpen(true);
            setHighlightIdx(Math.max(0, options.findIndex((o) => o.value === value)));
          } else {
            setHighlightIdx((prev) => {
              let next = prev;
              for (let i = 0; i < options.length; i++) {
                const candidate = (prev + 1 + i) % options.length;
                if (!options[candidate]?.disabled) {
                  next = candidate;
                  break;
                }
              }
              return next;
            });
          }
          break;
        case 'ArrowUp':
          e.preventDefault();
          if (open) {
            setHighlightIdx((prev) => {
              let next = prev;
              for (let i = 0; i < options.length; i++) {
                const candidate = (prev - 1 - i + options.length) % options.length;
                if (!options[candidate]?.disabled) {
                  next = candidate;
                  break;
                }
              }
              return next;
            });
          }
          break;
        case 'Tab':
          setOpen(false);
          setHighlightIdx(-1);
          break;
      }
    },
    [disabled, open, highlightIdx, options, value, notifyChange],
  );

  if (segmented && options.length > 1 && options.length < SEGMENTED_OPTION_LIMIT && selectedOption) {
    return <SegmentedOptions options={options} value={value} disabled={!!disabled} className={className}
      onSelect={notifyChange} id={rest.id} ariaLabel={rest['aria-label']}
      ariaLabelledby={rest['aria-labelledby'] as string | undefined} ariaDescribedby={rest['aria-describedby']} />;
  }

  const rootClass = [
    'custom-select',
    compact ? 'custom-select--compact' : '',
    open ? 'custom-select--open' : '',
    disabled ? 'custom-select--disabled' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const panel = active && options.length > 0 && panelStyle
    ? createPortal(
        <div className={rootClass}>
          <MotionConfig reducedMotion="never">
          <AnimatePresence>
          {open && <SelectPanel
            key="options"
            panelRef={listRef}
            style={panelStyle}
            id={`custom-select-listbox-${instanceId}`}
            animated={animated}
          >
            {options.map((opt, idx) => (
              <div
                key={opt.value || `__empty_${idx}`}
                className={[
                  'custom-select__option',
                  opt.value === value ? 'custom-select__option--selected' : '',
                  idx === highlightIdx ? 'custom-select__option--highlighted' : '',
                  opt.disabled ? 'custom-select__option--disabled' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                role="option"
                aria-selected={opt.value === value}
                data-idx={idx}
                onClick={(e) => {
                  if (opt.disabled) return;
                  e.preventDefault();
                  e.stopPropagation();
                  justSelectedRef.current = true;
                  setTimeout(() => { justSelectedRef.current = false; }, 0);
                  notifyChange(opt.value);
                  setOpen(false);
                  setHighlightIdx(-1);
                }}
                onMouseEnter={() => setHighlightIdx(idx)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                }}
              >
                {opt.label}
              </div>
            ))}
          </SelectPanel>}
          </AnimatePresence>
          </MotionConfig>
        </div>,
        document.body,
      )
    : null;

  return (
    <div ref={containerRef} className={rootClass}>
      <button
        id={rest.id}
        type="button"
        className="custom-select__trigger"
        disabled={!!disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={rest['aria-labelledby'] as string | undefined}
        aria-label={rest['aria-label']}
        aria-describedby={rest['aria-describedby']}
        onClick={() => {
          if (disabled) return;
          // If label just forwarded a click after selecting an option, ignore
          if (justSelectedRef.current) {
            justSelectedRef.current = false;
            return;
          }
          setOpen((prev) => !prev);
          setHighlightIdx(options.findIndex((o) => o.value === value));
        }}
        onKeyDown={handleKeyDown}
      >
        <span className="custom-select__value">{displayLabel}</span>
        <span className="custom-select__arrow" aria-hidden="true">
          <svg
            width="12"
            height="8"
            viewBox="0 0 12 8"
            fill="none"
          >
            <path
              d="M1.5 1.5L6 6l4.5-4.5"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
      </button>
      {panel}
    </div>
  );
}
