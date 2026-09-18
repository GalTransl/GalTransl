import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useConnection } from './ConnectionContext';
import { StartupSplash } from './StartupSplash';

/** 淡出时长，需与 styles/components/startup-splash.css 里的过渡时长保持一致 */
const FADE_OUT_MS = 420;
/** 最短展示时长：后端秒连时也让 splash 完整出现一次，避免界面闪一下 */
const MIN_VISIBLE_MS = 700;

/**
 * 主界面之外的启动闸门。
 *
 * 后端没连上前不挂载主界面（否则首页会在后端还没起来时就去拉任务列表，
 * 立刻弹一个"无法连接到后端"的错误），而是显示一张覆盖全屏的加载界面；
 * 连上之后再让主界面挂载，并让加载界面淡出。
 *
 * 后端最终失败时也不把人困在加载界面上：可以重试，也可以跳过直接进主界面。
 */
export function BootstrapGate({ children }: { children: ReactNode }) {
  const { connectionPhase, connectionMessage, connectionStep, loadingInitialData, loadInitialData } = useConnection();
  const [skipped, setSkipped] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [splashGone, setSplashGone] = useState(false);
  const mountedAtRef = useRef(Date.now());

  const ready = connectionPhase === 'online' && !loadingInitialData;
  const showApp = ready || skipped;

  useEffect(() => {
    if (!showApp) {
      return undefined;
    }

    const elapsed = Date.now() - mountedAtRef.current;
    let fadeTimer = 0;
    const holdTimer = window.setTimeout(() => {
      setLeaving(true);
      fadeTimer = window.setTimeout(() => setSplashGone(true), FADE_OUT_MS);
    }, Math.max(0, MIN_VISIBLE_MS - elapsed));

    return () => {
      window.clearTimeout(holdTimer);
      if (fadeTimer) {
        window.clearTimeout(fadeTimer);
      }
    };
  }, [showApp]);

  return (
    <>
      {showApp ? children : null}
      {splashGone ? null : (
        <StartupSplash
          phase={connectionPhase}
          message={connectionMessage}
          step={connectionStep}
          leaving={leaving}
          onRetry={() => void loadInitialData()}
          onSkip={() => setSkipped(true)}
        />
      )}
    </>
  );
}
