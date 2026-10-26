import { useEffect, useState } from 'react';
import { usePageActive } from '../components/PageActivity';
import { t as translate, useUiLanguage } from '../i18n';

/** 相对会话时间；超过一周显示日期，跨年时带年份。 */
export function formatSessionTime(ts: number): string {
  if (!ts) return '';
  const then = new Date(ts * 1000);
  const time = then.getTime();
  if (Number.isNaN(time)) return '';
  const diffMs = Date.now() - time;
  if (diffMs < 60_000) return translate('agent:agentSessionSidebar.formatSessionTime_message_text');
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return translate('agent:agentSessionSidebar.formatSessionTime_message_minutes', { minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return translate('agent:agentSessionSidebar.formatSessionTime_message_hours', { hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return translate('agent:agentSessionSidebar.formatSessionTime_message_textVariant2', { days });
  const showYear = then.getFullYear() !== new Date().getFullYear();
  const md = `${then.getMonth() + 1}/${then.getDate()}`;
  return showYear ? `${then.getFullYear()}/${md}` : md;
}

/** 页面可见时每分钟刷新，切回页面或切换语言时立即重算。 */
export function useSessionTime() {
  useUiLanguage();
  const active = usePageActive();
  const [, setTimeTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = () => {
      setTimeTick(n => n + 1);
      timer = setTimeout(refresh, 60_000);
    };
    refresh();
    return () => clearTimeout(timer);
  }, [active]);
  return formatSessionTime;
}
