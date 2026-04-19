import {
  type CSSProperties,
  type ReactNode,
  type SelectHTMLAttributes,
  Children,
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';

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
  children: ReactNode;
};

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
  ...rest
}: CustomSelectProps) {
  const options = extractOptions(children);
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
    if (!open) return;
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
  }, [open]);

  // Scroll highlighted item into view
  useEffect(() => {
    if (!open || highlightIdx < 0 || !listRef.current) return;
    const el = listRef.current.querySelector(
      `[data-idx="${highlightIdx}"]`,
    ) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [open, highlightIdx]);

  useEffect(() => {
    if (!open) {
      setPanelStyle(null);
      return;
    }

    const updatePanelStyle = () => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const gap = 6;
      const estimatedPanelHeight = 280;
      const openUpward = window.innerHeight - rect.bottom < estimatedPanelHeight && rect.top > window.innerHeight - rect.bottom;
      const width = rect.width;
      const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));

      setPanelStyle({
        position: 'fixed',
        left,
        right: 'auto',
        width,
        top: openUpward ? undefined : rect.bottom + gap,
        bottom: openUpward ? window.innerHeight - rect.top + gap : undefined,
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
  }, [open]);

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
          if (!open) {
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
          if (!open) {
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

  const rootClass = [
    'custom-select',
    compact ? 'custom-select--compact' : '',
    open ? 'custom-select--open' : '',
    disabled ? 'custom-select--disabled' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const panel = open && options.length > 0 && panelStyle
    ? createPortal(
        <div className={rootClass}>
          <div
            ref={listRef}
            className="custom-select__panel"
            style={panelStyle}
            role="listbox"
            id={`custom-select-listbox-${instanceId}`}
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
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div ref={containerRef} className={rootClass}>
      <button
        type="button"
        className="custom-select__trigger"
        disabled={!!disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={rest['aria-labelledby'] as string | undefined}
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
