import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../../components/Icon';
import { t, useUiLanguage } from '../../../i18n';

export function MessageCopyButton({ text }: { text: string }) {
  useUiLanguage();
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle');
  const request = useRef(0);
  useEffect(() => {
    setState('idle');
    // 切换正文或卸载后，旧的剪贴板 Promise 不能覆盖新消息的反馈。
    return () => { request.current += 1; };
  }, [text]);
  useEffect(() => {
    if (state !== 'copied') return;
    const timer = window.setTimeout(() => setState('idle'), 2000);
    return () => window.clearTimeout(timer);
  }, [state]);
  const copy = async () => {
    const id = ++request.current;
    setState('copying');
    try {
      await navigator.clipboard.writeText(text);
      if (id === request.current) setState('copied');
    } catch {
      if (id === request.current) setState('failed');
    }
  };
  const label = state === 'copied'
    ? t('agent:messageActions.copied')
    : state === 'failed'
      ? t('agent:messageActions.copyFailed')
      : t('agent:messageActions.copy');
  return (
    <>
      <button type="button" className="agent-message-copy" onClick={() => void copy()}
        disabled={!text || state === 'copying'} aria-label={label} title={label}>
        <Icon name={state === 'copied' ? 'check' : 'copy'} />
      </button>
      <span className={`agent-message-copy__status${state === 'failed' ? ' is-error' : ''}`} role="status">
        {state === 'copied' || state === 'failed' ? label : ''}
      </span>
    </>
  );
}
