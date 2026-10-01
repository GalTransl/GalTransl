import { useMemo } from 'react';
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
  const html = useMemo(() => renderMarkdown(text, { cursor }), [text, cursor]);
  return (
    <div
      className={['agent-md', className].filter(Boolean).join(' ')}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
