import { useMemo } from 'react';
import { useUiLanguage } from '../i18n';
import { renderMarkdown } from '../lib/markdown';

/** 共用 Markdown 正文；工具返回中的文本不解析为缓存引用指令。 */
export function Markdown({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const language = useUiLanguage();
  const html = useMemo(() => renderMarkdown(text), [language, text]);
  return (
    <div
      className={['agent-md', className].filter(Boolean).join(' ')}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
