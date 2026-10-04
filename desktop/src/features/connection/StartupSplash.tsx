import { t as translate, useUiLanguage } from "../../i18n";
import { useEffect, useRef, useState } from 'react';
import logoUrl from '../../assets/logo.png';
import { Button } from '../../components/Button';
import { Icon } from '../../components/Icon';
import type { ConnectionPhase } from '../../lib/api';

type StartupSplashProps = {
  phase: ConnectionPhase;
  message: string;
  /** 当前进行到第几步（1 起，见 ConnectionContext 的 connectionStep） */
  step: number;
  /** 淡出中（由 BootstrapGate 控制） */
  leaving: boolean;
  onRetry: () => void;
  onSkip: () => void;
};

/** 与 lib 侧 ConnectionContext 的 connectionStep 一一对应 */
const STEPS = [
  { index: 1, get label() { return translate("common:startupSplash.label_label_translation"); } },
  { index: 2, get label() { return translate("common:startupSplash.label_label_translationVersion"); } },
  { index: 3, get label() { return translate("common:startupSplash.label_label_interface"); } },
];

const ELAPSED_VISIBLE_AFTER_MS = 1200;

/**
 * 覆盖全屏的启动加载界面。
 *
 * 纯展示组件：什么时候显示、什么时候淡出由 BootstrapGate 决定，这里只按
 * phase / step / message 把「正在做什么、做到哪了」讲清楚；连不上时给出
 * 重试与跳过两条路，避免界面卡死在这张图上。
 */
export function StartupSplash({ phase, message, step, leaving, onRetry, onSkip }: StartupSplashProps) {
  const uiLanguage = useUiLanguage();
  const [elapsedMs, setElapsedMs] = useState(0);
  const startedAtRef = useRef(Date.now());
  const offline = phase === 'offline';

  useEffect(() => {
    if (phase !== 'connecting') {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAtRef.current);
    }, 200);
    return () => window.clearInterval(timer);
  }, [phase]);

  const showElapsed = !offline && elapsedMs >= ELAPSED_VISIBLE_AFTER_MS;

  return (
    <div
      className={`startup-splash${leaving ? ' startup-splash--leaving' : ''}`}
      role="status"
      aria-live="polite"
      aria-busy={!offline}
    >
      <div className="startup-splash__glow startup-splash__glow--primary" aria-hidden="true" />
      <div className="startup-splash__glow startup-splash__glow--accent" aria-hidden="true" />

      <div className="startup-splash__card">
        <div className="startup-splash__brand">
          <span className="startup-splash__logo-wrap">
            <span className="startup-splash__logo-halo" aria-hidden="true" />
            <img src={logoUrl} alt="" className="startup-splash__logo" />
          </span>
          <h1 className="startup-splash__title">{translate("common:startupSplash.startupSplashBrand_message_galTransl")}</h1>
          <p className="startup-splash__subtitle">{translate("common:startupSplash.startupSplashBrand_message_translateYourFavoriteGalgame")}</p>
        </div>

        {offline ? (
          <div className="startup-splash__error">
            <div className="startup-splash__error-head">
              <Icon name="warning" size={18} />
              <span>{translate("common:startupSplash.startupSplashErrorHead_message_translationNot")}</span>
            </div>
            <p className="startup-splash__error-message">{message}</p>
            <div className="startup-splash__actions">
              <Button type="button" onClick={onRetry}>{translate("common:startupSplash.startupSplashActions_message_retryConnection")}</Button>
              <Button type="button" variant="secondary" onClick={onSkip}>{translate("common:startupSplash.startupSplashActions_message_text")}</Button>
            </div>
          </div>
        ) : (
          <div className="startup-splash__progress">
            <div className="startup-splash__progress-row">
              <Spinner />
              <div className="startup-splash__progress-text">
                <p className="startup-splash__message">{message}</p>
                {showElapsed ? (
                  <p className="startup-splash__elapsed">{translate("common:startupSplash.startupSplashProgressText_message_doneWaitSeconds", { value: (elapsedMs / 1000).toFixed(1) })}</p>
                ) : null}
              </div>
            </div>

            <ol className="startup-splash__steps">
              {STEPS.map((item) => {
                const state = step > item.index ? 'done' : step === item.index ? 'active' : 'pending';
                return (
                  <li key={item.index} className={`startup-splash__step startup-splash__step--${state}`}>
                    <span className="startup-splash__step-marker" aria-hidden="true">
                      {state === 'done' ? <Icon name="check" size={12} /> : null}
                    </span>
                    <span className="startup-splash__step-label">{item.label}</span>
                  </li>
                );
              })}
            </ol>

            <div className="startup-splash__bar" aria-hidden="true">
              <div className="startup-splash__bar-fill" />
            </div>
          </div>
        )}

        <p className="startup-splash__footnote">{translate("common:startupSplash.startupSplashCard_message_translationSeconds")}</p>
      </div>
    </div>
  );
}

function Spinner() {
  useUiLanguage();
  return (
    <svg className="startup-splash__spinner" viewBox="0 0 40 40" width="34" height="34" fill="none" aria-hidden="true">
      <circle cx="20" cy="20" r="16.5" stroke="var(--color-line-strong)" strokeWidth="3" />
      <circle
        cx="20"
        cy="20"
        r="16.5"
        stroke="var(--color-primary)"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray="26 100"
      />
    </svg>
  );
}
