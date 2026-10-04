import { useMemo } from 'react';
import { useUiLanguage } from '../i18n';
import { renderMarkdown } from '../lib/markdown';

/** 共用 Markdown 正文；工具返回中的文本不解析为缓存引用指令。 */
export function Markdown({
  text,
  cursor,
  className,
}: {
  text: string;
  cursor?: boolean;
  className?: string;
}) {
  const language = useUiLanguage();
  const html = useMemo(() => renderMarkdown(text, { cursor }), [language, text, cursor]);
  return (
    <div
      className={['agent-md', className].filter(Boolean).join(' ')}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
