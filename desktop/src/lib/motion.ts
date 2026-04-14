/**
 * GalTransl Desktop — Motion Design Tokens & Utilities
 *
 * Single source of truth for animation durations, easings, and motion helpers.
 * CSS animations should reference these values via CSS custom properties
 * defined in tokens.css; JS-driven timers should import from here.
 */

// ── Durations ──────────────────────────────────────────
export const DUR = {
  /** Micro feedback (hover, press) */
  micro: 120,
  /** Fast transition (sidebar collapse, tab switch) */
  fast: 200,
  /** Standard enter/exit (page transition, card appear) */
  standard: 300,
  /** Emphasized motion (hero progress, launch charge) */
  emphasized: 500,
  /** Celebration / complex sequences */
  celebration: 800,
} as const;

// ── Easings ────────────────────────────────────────────
export const EASE = {
  /** Default ease for most UI transitions */
  default: 'cubic-bezier(0.25, 0.1, 0.25, 1)',
  /** Deceleration (entering elements) */
  decel: 'cubic-bezier(0, 0, 0.2, 1)',
  /** Acceleration (exiting elements) */
  accel: 'cubic-bezier(0.4, 0, 1, 1)',
  /** Sharp — material-style standard */
  sharp: 'cubic-bezier(0.4, 0, 0.6, 1)',
  /** Spring overshoot (celebrations, badge pop) */
  spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)',
  /** Smooth bar fill */
  barFill: 'cubic-bezier(0.22, 1, 0.36, 1)',
} as const;

// ── Translate-page motion timing (JS-driven) ───────────
export const LAUNCH = {
  chargeMs: DUR.emphasized,
  blastMs: 600,
  particleCount: 12,
  particleDistanceMin: 30,
  particleDistanceMax: 80,
  rippleMs: 600,
  particleMs: 700,
} as const;

export const STRIP_BOOT = {
  scanMs: 500,
  glowMs: 800,
  totalMs: 1200,
} as const;

export const BAR_SURGE = {
  ms: 800,
} as const;

export const COMPLETE = {
  celebrateMs: 1200,
  barGlowMs: 800,
  badgePopMs: 600,
} as const;

/** How long a fresh success row stays highlighted */
export const FRESH_HIGHLIGHT_MS = 2200;

// ── reduced-motion helper ──────────────────────────────

const QUERY = '(prefers-reduced-motion: reduce)';

/** Check if the user prefers reduced motion (sync, non-reactive) */
export function prefersReducedMotion(): boolean {
  return window.matchMedia(QUERY).matches;
}

/**
 * React hook — subscribes to the prefers-reduced-motion media query.
 * Returns `true` when the user wants minimal motion.
 *
 * Use this to conditionally skip particle bursts, ripples, etc.
 * CSS-side animations should use the @media (prefers-reduced-motion) block.
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = React.useState(prefersReducedMotion);

  React.useEffect(() => {
    const mql = window.matchMedia(QUERY);
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, []);

  return reduced;
}

// Re-export React for the hook's internal usage
import React from 'react';
