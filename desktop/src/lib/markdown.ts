/* 轻量 Markdown 渲染器（供 Agent 对话气泡使用）。
   覆盖 LLM 常见输出：标题 / 粗斜体 / 行内代码 / 围栏代码块 / 无序有序列表 /
   引用 / 段落。所有文本先 HTML 转义再做替换，不产生注入面；链接降级为
   纯文本 + 原始 URL（桌面端 WebView 不外跳）。 */

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 行内元素：行内代码、图片/链接降级、粗体、斜体。输入须已转义。 */
function renderInline(escaped: string): string {
  // 行内代码优先（内部不再解析其它标记）
  let out = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');
  // 图片 ![alt](url) -> 「图片」alt
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '「图片：$1」');
  // 链接 [text](url) -> text（url）——不生成可点击链接
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '$1（$2）');
  // 加粗 + 斜体
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return out;
}

// 表格行：| a | b |，允许省略首尾竖线
const TABLE_ROW_STRICT = /^\s*\|.*\|\s*$/;
const TABLE_ROW_ANY = /^\s*(?:\|)/.source; // 供日志，不使用
// 宽松判定：含未转义竖线且不像其它块级语法（列表/引用/标题/水平线由前序分支处理）
function looksLikeTableRow(line: string): boolean {
  if (TABLE_ROW_STRICT.test(line)) return true;
  // 无首尾竖线：行内要有至少 2 个未转义单元格分隔
  if (/^\s*\|/.test(line)) return true;
  const unescaped = line.replace(/\\\|/g, '\u0000');
  return unescaped.includes('|') && unescaped.split('|').length >= 2;
}
// 分隔行：| --- | :--: | --: |（允许省略首尾竖线）
function isTableSplit(line: string): boolean {
  const t = line.trim();
  if (!/-/.test(t)) return false;
  // 只由 | : - 空格构成
  return /^[\s|:-]+$/.test(t) && t.includes('-');
}

/** 把 | a | b | 拆成单元格数组（trim 每个 cell，保留转义竖线）。 */
function splitTableRow(line: string): string[] {
  let inner = line.trim();
  if (inner.startsWith('|')) inner = inner.slice(1);
  if (inner.endsWith('|') && !inner.endsWith('\\|')) inner = inner.slice(0, -1);
  // 单元格内的 \| 转义竖线先占位，避免被误当分隔符
  const cells = inner.replace(/\\\|/g, '\u0000').split('|');
  return cells.map((c) => c.trim().replace(/\u0000/g, '|'));
}
function renderTable(header: string, splitLine: string, bodyRows: string[]): string {
  const headerCells = splitTableRow(header).map((c) => renderInline(escapeHtml(c)));
  const aligns = splitTableRow(splitLine).map((c) => {
    const l = c.startsWith(':');
    const r = c.endsWith(':');
    if (l && r) return 'center';
    if (r) return 'right';
    return '';
  });
  const align = (i: number) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : '');

  const html: string[] = ['<table>', '<thead><tr>'];
  headerCells.forEach((c, i) => {
    html.push(`<th${align(i)}>${c}</th>`);
  });
  html.push('</tr></thead>');
  if (bodyRows.length) {
    html.push('<tbody>');
    for (const row of bodyRows) {
      html.push('<tr>');
      splitTableRow(row)
        .map((c) => renderInline(escapeHtml(c)))
        .forEach((c, i) => html.push(`<td${align(i)}>${c}</td>`));
      html.push('</tr>');
    }
    html.push('</tbody>');
  }
  html.push('</table>');
  return html.join('');
}

/** 打字机光标：流式输出时贴在最后一个字符之后的细竖线。 */
const TYPING_CURSOR_HTML = '<span class="agent-typing-cursor" aria-hidden="true"></span>';

/** 可容纳行内内容的文本容器闭合标签。 */
const INLINE_TAIL_RE = /<\/(?:p|li|blockquote|h[3-6]|td|th|code)>/g;

/** 只做结构收尾、自身不含文本的节点（列表闭合标签），光标要落到更早的节点里。 */
const STRUCTURAL_TAIL_RE = /^<\/(?:ul|ol|table|thead|tbody|tr)>$/;

/**
 * 把打字机光标注入到最后一个文本块内部（取其最后一个闭合标签之前），
 * 让光标停在文字的同一行末尾，而不是被块级元素挤到下一行。
 * 若最后一块无法容纳行内内容（如水平线），才退化为独立的行内元素。
 */
function injectTypingCursor(nodes: string[]): void {
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const node = nodes[i];
    if (STRUCTURAL_TAIL_RE.test(node.trim())) continue;
    let insertAt = -1;
    INLINE_TAIL_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = INLINE_TAIL_RE.exec(node)) !== null) insertAt = match.index;
    if (insertAt >= 0) {
      nodes[i] = node.slice(0, insertAt) + TYPING_CURSOR_HTML + node.slice(insertAt);
      return;
    }
    break;
  }
  nodes.push(TYPING_CURSOR_HTML);
}

/** 把一段 markdown 文本渲染成 HTML 片段。
 *  options.cursor 为真时，在末尾追加打字机光标（供流式输出使用）。 */
export function renderMarkdown(markdown: string, options?: { cursor?: boolean }): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html: string[] = [];

  let inCode = false;
  let codeLines: string[] = [];
  let listType: 'ul' | 'ol' | null = null;
  let quoteLines: string[] = [];
  let paraLines: string[] = [];
  // 表格收集：header / 分隔行 / 数据行
  let tableHeader: string | null = null;
  let tableSplit: string | null = null;
  let tableBody: string[] = [];

  const closeList = () => {
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
  };
  const closeQuote = () => {
    if (quoteLines.length) {
      html.push(`<blockquote>${renderInline(escapeHtml(quoteLines.join(' ')))}</blockquote>`);
      quoteLines = [];
    }
  };
  const closePara = () => {
    if (paraLines.length) {
      html.push(`<p>${renderInline(escapeHtml(paraLines.join('\n')))}</p>`);
      paraLines = [];
    }
  };
  const closeTable = () => {
    if (tableHeader !== null && tableSplit !== null) {
      html.push(renderTable(tableHeader, tableSplit, tableBody));
    } else if (tableHeader !== null) {
      // 没等到分隔行就不是表格，按普通段落吐回去
      paraLines.push(tableHeader);
      paraLines.push(...tableBody);
    }
    tableHeader = null;
    tableSplit = null;
    tableBody = [];
  };
  const closeAll = () => {
    // 先关表格（回退时会把 header 吐回 paraLines），再关段落，
    // 否则单行"伪表格"文本会滞留在 paraLines 里丢失。
    closeTable();
    closePara();
    closeList();
    closeQuote();
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    // 围栏代码块
    if (/^\s*```/.test(line)) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        closeAll();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(raw);
      continue;
    }

    // 空行：收束当前块
    if (!line.trim()) {
      closeAll();
      continue;
    }

    // 表格：header 行之后必须紧跟分隔行，然后连续吃数据行。
    // 注意表格判定要排在列表/引用/标题之前——分隔行 "---" 形态与水平线
    // 冲突，但仅在 header 待定上下文里才会走到这里。
    const looksLikeRow = looksLikeTableRow(line);
    if (looksLikeRow) {
      if (tableHeader === null) {
        // 待定：先攒着，下一行是分隔行才成立（水平线等形态由后续分支处理，
        // 因为能进这个分支说明 header 已待定）
        closePara();
        closeList();
        closeQuote();
        tableHeader = line;
        continue;
      }
      if (tableSplit === null) {
        if (isTableSplit(line)) {
          tableSplit = line;
        } else {
          // header 后不是分隔行：回退成段落，当前行重新走普通逻辑
          closeTable();
          paraLines.push(line);
        }
        continue;
      }
      // 分隔行已见，这是数据行
      tableBody.push(line);
      continue;
    } else if (tableHeader !== null || tableSplit !== null) {
      // 表格被非表格行打断：闭合（header 无分隔行时回退为段落）
      closeTable();
      if (tableSplit === null && paraLines.length) {
        // closeTable 已把 header 吐回 paraLines；当前行作为普通文本续上
        paraLines.push(line);
        continue;
      }
    }

    // 标题
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      closeAll();
      const level = Math.min(heading[1].length + 2, 6); // # -> h3，最多 h6
      html.push(`<h${level}>${renderInline(escapeHtml(heading[2]))}</h${level}>`);
      continue;
    }

    // 引用（> 已被转义成 &gt;，两种形态都认）
    const quote = /^\s*(?:&gt;|>)\s?(.*)$/.exec(line);
    if (quote) {
      closePara();
      closeList();
      quoteLines.push(quote[1]);
      continue;
    }

    // 无序列表
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (ul) {
      closePara();
      closeQuote();
      if (listType !== 'ul') {
        closeList();
        html.push('<ul>');
        listType = 'ul';
      }
      html.push(`<li>${renderInline(escapeHtml(ul[1]))}</li>`);
      continue;
    }

    // 有序列表
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ol) {
      closePara();
      closeQuote();
      if (listType !== 'ol') {
        closeList();
        html.push('<ol>');
        listType = 'ol';
      }
      html.push(`<li>${renderInline(escapeHtml(ol[1]))}</li>`);
      continue;
    }

    // 水平线
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      closeAll();
      html.push('<hr />');
      continue;
    }

    // 普通段落文本
    closeList();
    closeQuote();
    paraLines.push(line);
  }

  // 收尾：未闭合的代码块/列表/段落
  if (inCode && codeLines.length) {
    html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  }
  closeAll();
  if (options?.cursor) injectTypingCursor(html);
  return html.join('\n');
}
