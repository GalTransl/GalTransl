/**
 * GalTransl Desktop — Motion Design Tokens & Utilities
 *
 * Single source of truth for animation durations, easings, and motion helpers.
 * CSS animations should reference these values via CSS custom properties
 * defined in tokens.css; JS-driven timers should import from here.
 */

import { useEffect, useState } from 'react';
import { DISABLE_ANIMATIONS_CHANGE_EVENT, getDisableAnimationsPreference } from './api';

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

/**
 * 桌面端动画不跟随系统的「减弱动态效果」，但跟随用户在设置里手动关闭的
 * 「关闭所有动画效果」开关——打开后所有 JS 动效（发射动画、平滑滚动等）
 * 都按减弱动态效果处理，减少 GPU 占用。
 */
export function prefersReducedMotion(): boolean {
  return getDisableAnimationsPreference();
}

/**
 * React hook-shaped helper：跟随设置里「关闭所有动画效果」的即时切换，
 * 缓存页面（切设置后不重新挂载）也能拿到最新值。
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion);
  useEffect(() => {
    const sync = () => setReduced(prefersReducedMotion());
    window.addEventListener(DISABLE_ANIMATIONS_CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener(DISABLE_ANIMATIONS_CHANGE_EVENT, sync);
    };
  }, []);
  return reduced;
}
