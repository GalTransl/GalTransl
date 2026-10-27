import { memo, useMemo } from 'react';
import { useUiLanguage } from '../i18n';
import { renderMarkdownBlocks } from '../lib/markdown';

/** 文本 -> HTML。块级缓存随之生效（流式输出只重解析最后一块）；
 *  语言进依赖是因为图片降级文案要跟着界面语言走。 */
export function useMarkdownHtml(text: string): string {
  const language = useUiLanguage();
  return useMemo(() => renderMarkdownBlocks(text, language), [language, text]);
}

/** 共用 Markdown 正文；工具返回中的文本不解析为缓存引用指令。 */
export const Markdown = memo(function Markdown({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const html = useMarkdownHtml(text);
  return (
    <div
      className={['agent-md', className].filter(Boolean).join(' ')}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
});
